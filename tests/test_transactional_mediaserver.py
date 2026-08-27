"""媒体服务器本地缓存短事务适配器的回归测试。"""

import json
from dataclasses import FrozenInstanceError
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.application.mediaserver import MediaServerQueryService, MediaServerSyncItem
from app.db.adapters.mediaserver import TransactionalMediaServerRepository
from app.db.base import Base
from app.db.models.mediaserver import MediaServerItem
from app.schemas.mediaserver import MediaServerItem as MediaServerItemSchema
from app.schemas.types import MediaSource


@pytest.fixture
def session_factory(tmp_path):
    """创建隔离的媒体服务器缓存数据库与会话工厂。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'mediaserver-adapter.db'}")
    factory = sessionmaker(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield factory
    engine.dispose()


def _sync_item(*, title: str, sync_time: str) -> MediaServerSyncItem:
    """构造一个可重复 upsert 的冻结媒体库条目。"""
    return MediaServerSyncItem(
        server="plex",
        library="movies",
        item_id="item-1",
        item_type="电影",
        title=title,
        original_title=None,
        year="2026",
        media_source=MediaSource.TMDB,
        media_id="1001",
        path=f"/media/{title}.mkv",
        seasoninfo=((1, (1, 2)),),
        note_json=None,
        lst_mod_date=sync_time,
    )


def test_upsert_commits_insert_and_update_without_external_session(
    session_factory,
    monkeypatch,
) -> None:
    """每次 upsert 应在单一短 Session 中完成查询、写入和提交。"""
    from app.db import base as db_base

    monkeypatch.setattr(
        db_base,
        "run_sync_transaction",
        lambda _operation: (_ for _ in ()).throw(
            AssertionError("显式 Session 不应创建兼容事务")
        ),
    )
    repository = TransactionalMediaServerRepository(session_factory)

    assert repository.upsert(
        _sync_item(title="旧标题", sync_time="2026-08-28 10:00:00")
    )
    assert not repository.upsert(
        _sync_item(title="新标题", sync_time="2026-08-28 11:00:00")
    )

    with session_factory() as session:
        items = session.query(MediaServerItem).all()

    assert len(items) == 1
    assert items[0].title == "新标题"
    assert items[0].path == "/media/新标题.mkv"
    assert items[0].seasoninfo == {"1": [1, 2]}


def test_get_item_id_returns_scalar_after_short_session_closes(session_factory) -> None:
    """查询端口只返回标量 ID，不把 ORM 条目带出短 Session。"""
    repository = TransactionalMediaServerRepository(session_factory)
    repository.upsert(
        _sync_item(title="查询标题", sync_time="2026-08-28 10:00:00")
    )

    item_id = repository.get_item_id(
        media_source=MediaSource.TMDB,
        media_id="1001",
        mtype="电影",
        season=1,
    )

    assert item_id == "item-1"


def test_sync_item_deeply_detaches_mutable_remote_values() -> None:
    """冻结 DTO 必须规范化季集并断开与远端 note 的可变引用。"""
    note = {"nested": ["original"]}
    source = MediaServerItemSchema(
        server="plex",
        library="shows",
        item_id="show-1",
        item_type="Series",
        title="剧集",
        note=note,
    )

    snapshot = MediaServerSyncItem.from_item(
        source,
        item_type="电视剧",
        seasoninfo={1: None, 2: [1, 2]},
        sync_time="2026-08-28 10:00:00",
    )
    note["nested"].append("changed")

    assert snapshot.seasoninfo == ((1, ()), (2, (1, 2)))
    assert json.loads(snapshot.note_json or "null") == {"nested": ["original"]}
    with pytest.raises(FrozenInstanceError):
        snapshot.note_json = None


def test_upsert_rolls_back_and_preserves_original_error() -> None:
    """单条写入异常时必须回滚短事务并继续抛出原始异常。"""
    session = MagicMock()
    session.__enter__.return_value = session
    session.__exit__.return_value = False
    repository = TransactionalMediaServerRepository(MagicMock(return_value=session))

    with patch(
        "app.db.adapters.mediaserver.MediaServerOper.upsert",
        side_effect=ValueError("invalid item"),
    ), pytest.raises(ValueError, match="invalid item"):
        repository.upsert(
            _sync_item(title="失败标题", sync_time="2026-08-28 10:00:00")
        )

    session.rollback.assert_called_once_with()
    session.commit.assert_not_called()


def test_upsert_rolls_back_commit_failure() -> None:
    """提交阶段失败也必须回滚短事务并传播原始异常。"""
    session = MagicMock()
    session.__enter__.return_value = session
    session.__exit__.return_value = False
    session.commit.side_effect = RuntimeError("commit failed")
    repository = TransactionalMediaServerRepository(MagicMock(return_value=session))

    with patch(
        "app.db.adapters.mediaserver.MediaServerOper.upsert",
        return_value=True,
    ), pytest.raises(RuntimeError, match="commit failed"):
        repository.upsert(
            _sync_item(title="提交失败", sync_time="2026-08-28 10:00:00")
        )

    session.commit.assert_called_once_with()
    session.rollback.assert_called_once_with()


@pytest.mark.asyncio
async def test_query_service_consumes_scalar_repository_result() -> None:
    """Application 查询服务只消费标量 ID，不读取 ORM 属性。"""
    repository = MagicMock()
    repository.async_get_item_id = AsyncMock(return_value="item-1")
    service = MediaServerQueryService(repository)

    result = await service.find_item_id(
        media_source=MediaSource.TMDB,
        media_id="1001",
        mtype="电影",
    )

    assert result == "item-1"
    repository.async_get_item_id.assert_awaited_once_with(
        title=None,
        year=None,
        mtype="电影",
        media_source=MediaSource.TMDB,
        media_id="1001",
        season=None,
    )
