"""
验证通知独立海报经过渲染、持久化、查询和事件投影，且不改变渠道图片。
"""

import asyncio
import importlib
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.api.endpoints.message import get_notification_message
from app.application.messaging.message import MessageQueryService, MessageTemplateHelper
from app.chain.base import ChainBase
from app.db.oper.message import MessageOper
from app.db.session import AsyncSessionFactory
from app.domain.context import MediaInfo, MusicInfo
from app.schemas.message import Message


@pytest.mark.parametrize("has_poster", [True, False])
def test_render_preserves_backdrop_and_projects_only_real_poster(has_poster: bool) -> None:
    """
    即使消息已经有正文也应提取独立海报，缺失时不得从背景图回退。
    """
    media = MediaInfo(
        title="测试影片",
        backdrop_path="https://images.example.com/backdrop.jpg",
        poster_path="https://images.example.com/poster.jpg" if has_poster else None,
    )
    message = Message(title="已完成整理", image=media.get_message_image())

    rendered = MessageTemplateHelper.render(message, mediainfo=media)

    assert rendered is message
    assert message.image == "https://images.example.com/backdrop.jpg"
    assert message.poster == ("https://images.example.com/poster.jpg" if has_poster else None)
    event = ChainBase._build_notice_message_data(message)
    assert event["poster"] == message.poster
    assert event["image"] == message.image


def test_render_keeps_explicit_poster_and_supports_music_cover() -> None:
    """
    显式海报优先，音乐媒体使用已有封面，无媒体上下文不猜测图片类型。
    """
    music = MusicInfo(title="测试专辑", cover_url="https://images.example.com/album.jpg")
    message = Message(title="入库完成", poster="https://images.example.com/custom.jpg")
    assert MessageTemplateHelper.render(message, mediainfo=music).poster == message.poster
    assert MessageTemplateHelper.render(Message(title="专辑通知"), mediainfo=music).poster == music.cover_url
    assert MessageTemplateHelper.render(Message(title="普通通知", image=music.cover_url)).poster is None


@pytest.mark.parametrize("async_write", [False, True])
def test_notification_history_preserves_poster_and_legacy_null(async_write: bool) -> None:
    """
    同步和异步写入均应通过真实通知查询返回独立海报，同时兼容旧消息。
    """
    title = f"poster-contract-{uuid4().hex}"
    payload = {
        "title": title,
        "image": "https://images.example.com/backdrop.jpg",
        "poster": "https://images.example.com/poster.jpg",
    }

    async def write_and_query() -> None:
        """
        仅使用测试临时数据库保存消息，并经 API schema 读取结果。
        """
        async with AsyncSessionFactory() as db:
            if async_write:
                await MessageOper(db).async_add(**payload)
                await MessageOper(db).async_add(title=f"{title}-legacy", image=payload["image"])
                await db.commit()
            else:
                MessageOper().add(**payload)
                MessageOper().add(title=f"{title}-legacy", image=payload["image"])
            messages = await get_notification_message(service=MessageQueryService(MessageOper(db)))
            current = next(item for item in messages if item.title == title)
            legacy = next(item for item in messages if item.title == f"{title}-legacy")
            assert current.image == payload["image"]
            assert current.poster == payload["poster"]
            assert legacy.image == payload["image"]
            assert legacy.poster is None

    asyncio.run(write_and_query())


@pytest.mark.parametrize("already_created", [False, True])
def test_message_poster_migration_preserves_existing_images(monkeypatch, already_created: bool) -> None:
    """
    迁移支持旧表和新 ORM 已建表，重复执行及回滚不会改写历史背景图。
    """
    migration = importlib.import_module("database.versions.e4a8c7b29016_3_1_9")
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    columns = [sa.Column("id", sa.Integer(), primary_key=True), sa.Column("image", sa.String())]
    if already_created:
        columns.append(sa.Column("poster", sa.String(), nullable=True))
    messages = sa.Table("message", metadata, *columns)
    with engine.begin() as connection:
        metadata.create_all(connection)
        connection.execute(messages.insert(), {"id": 1, "image": "legacy-backdrop.jpg"})
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        migration.upgrade()
        migrated = sa.Table("message", sa.MetaData(), autoload_with=connection)
        row = connection.execute(sa.select(migrated)).mappings().one()
        assert row["image"] == "legacy-backdrop.jpg"
        assert row["poster"] is None
        connection.execute(migrated.insert(), {"id": 2, "image": "backdrop.jpg", "poster": "poster.jpg"})
        assert connection.execute(sa.select(migrated.c.poster).where(migrated.c.id == 2)).scalar_one() == "poster.jpg"
        migration.downgrade()
        migration.downgrade()
        restored = sa.Table("message", sa.MetaData(), autoload_with=connection)
        assert "poster" not in restored.c
        assert connection.execute(sa.select(restored.c.image).order_by(restored.c.id)).scalars().all() == [
            "legacy-backdrop.jpg", "backdrop.jpg",
        ]
    engine.dispose()
