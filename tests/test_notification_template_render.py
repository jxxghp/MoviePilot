#!/usr/bin/env python
# -*- coding:utf-8 -*-
"""
通知模板渲染回归测试。

V3 起通知模板不再硬编码在程序中，默认模板由数据库升级迁移写入
``SystemConfigKey.NotificationTemplates``。本测试验证：

1. V3 迁移会把默认模板一次性写入数据库配置，并覆盖用户旧设置；
2. 带双引号条件的 Jinja 模板（如 ``{% if type == "音乐" %}``）可正常渲染，
   避免 JSON 序列化转义引号导致音乐下载/入库通知内容为空的历史问题；
3. 模板完全由数据库配置驱动，配置缺失时不渲染、消息保持原样。
"""
from pathlib import Path

import importlib.util

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.domain.context import MUSIC_ENTITY_ALBUM, MusicInfo
from app.domain.meta.metamusic import MetaMusic
from app.db.oper.systemconfig import SystemConfigOper
from app.application.messaging.message import MessageTemplateHelper, TemplateContextBuilder, TemplateHelper
from app.schemas.message import Message
from app.schemas.types import ContentType, SystemConfigKey

MUSIC_ORGANIZE_TEMPLATE = """
{
    'title': '{{ title_year }}{% if track_number %} #{{ track_number }}{% endif %}'
            '{% if season_episode %} {{ season_episode }}{% endif %} 已入库',
    'text': '类型：{{ type }}{% if category %}，类别：{{ category }}{% endif %}'
            '{% if type == "音乐" and artist %}\\n艺术家：{{ artist }}{% endif %}'
            '{% if type == "音乐" and album %}\\n专辑：{{ album }}{% endif %}'
            '{% if type == "音乐" and audio_specs %}\\n音质：{{ audio_specs }}{% endif %}'
            '{% if resource_term %}，质量：{{ resource_term }}{% endif %}'
            '，共{{ file_count }}个文件，大小：{{ total_size }}'
}"""

MUSIC_CONTEXT = {
    "type": "音乐",
    "title_year": "晴天 (2003)",
    "track_number": 3,
    "artist": "周杰伦",
    "album": "叶惠美",
    "audio_specs": "FLAC · 24-bit · 96 kHz",
    "resource_term": "无损",
    "file_count": 12,
    "total_size": "1.2 GB",
}


@pytest.fixture()
def notification_templates() -> SystemConfigOper:
    """备份并还原通知模板配置，避免测试污染其它用例。"""
    config_oper = SystemConfigOper()
    original = config_oper.get(SystemConfigKey.NotificationTemplates)
    yield config_oper
    config_oper.set(SystemConfigKey.NotificationTemplates, original)


def test_album_batch_context_uses_album_title_for_notification() -> None:
    """
    专辑实体批量入库的通知上下文应以专辑名作为标题，
    不得展示批次中某个单曲的曲名和曲序。
    """
    meta = MetaMusic(
        title="晴天",
        artists=["周杰伦"],
        album="叶惠美",
        track_number=3,
        year=2003,
    )
    mediainfo = MusicInfo(
        music_type=MUSIC_ENTITY_ALBUM,
        title="晴天",
        album="叶惠美",
        artists=["周杰伦"],
        year=2003,
    )

    context = TemplateContextBuilder().build(
        meta=meta, mediainfo=mediainfo, aggregate_music_album=True
    )

    assert context["title"] == "叶惠美"
    assert context["title_year"] == "叶惠美 (2003)"
    assert context.get("track_number") is None

    rendered = TemplateHelper().render(
        template_content=MUSIC_ORGANIZE_TEMPLATE,
        **dict(context, file_count=11, total_size="1.2 GB"),
    )
    assert isinstance(rendered, dict)
    assert rendered["title"] == "叶惠美 (2003) 已入库"
    assert "艺术家：周杰伦" in rendered["text"]
    assert "#3" not in rendered["title"]


def test_album_batch_context_keeps_track_identity_for_rename() -> None:
    """
    重命名等逐文件场景（未开启整专聚合）应继续使用每个文件的曲名和曲序，
    不能把专辑名写成所有目标文件名。
    """
    meta = MetaMusic(
        title="晴天",
        artists=["周杰伦"],
        album="叶惠美",
        track_number=3,
        year=2003,
    )
    mediainfo = MusicInfo(
        music_type=MUSIC_ENTITY_ALBUM,
        title="晴天",
        album="叶惠美",
        artists=["周杰伦"],
        year=2003,
    )

    context = TemplateContextBuilder().build(meta=meta, mediainfo=mediainfo)

    assert context["title"] == "晴天"
    assert context["track"] == "03"


def test_single_track_context_keeps_track_title_and_number() -> None:
    """
    单曲下载/整理的通知上下文应继续使用曲目标题和曲序，
    不受专辑场景调整的影响。
    """
    context = MUSIC_CONTEXT.copy()

    rendered = TemplateHelper().render(
        template_content=MUSIC_ORGANIZE_TEMPLATE, **context
    )

    assert isinstance(rendered, dict)
    assert rendered["title"] == "晴天 (2003) #3 已入库"


def test_literal_template_with_quoted_condition_renders_music() -> None:
    """
    带 ``{% if type == "音乐" %}`` 双引号条件的模板应能正常渲染，
    音乐字段（艺术家/专辑/音质）必须出现在渲染结果中。
    """
    rendered = TemplateHelper().render(
        template_content=MUSIC_ORGANIZE_TEMPLATE, **MUSIC_CONTEXT
    )

    assert isinstance(rendered, dict)
    assert rendered["title"] == "晴天 (2003) #3 已入库"
    assert "艺术家：周杰伦" in rendered["text"]
    assert "专辑：叶惠美" in rendered["text"]
    assert "音质：FLAC · 24-bit · 96 kHz" in rendered["text"]


def test_literal_template_skips_music_fields_for_video() -> None:
    """
    影视场景下模板中的音乐专属字段不应出现，影视字段正常渲染。
    """
    rendered = TemplateHelper().render(
        template_content=MUSIC_ORGANIZE_TEMPLATE,
        type="电视剧",
        title_year="测试剧集 (2025)",
        season_episode="S01E05",
        category="国产剧",
        resource_term="1080p",
        file_count=1,
        total_size="5 GB",
    )

    assert isinstance(rendered, dict)
    assert rendered["title"] == "测试剧集 (2025) S01E05 已入库"
    assert "艺术家" not in rendered["text"]
    assert "专辑" not in rendered["text"]
    assert "类别：国产剧" in rendered["text"]


def test_message_renders_from_db_config(notification_templates: SystemConfigOper) -> None:
    """
    MessageTemplateHelper 应使用数据库配置的模板渲染消息，
    而不再依赖程序内硬编码模板。
    """
    notification_templates.set(
        SystemConfigKey.NotificationTemplates,
        {"organizeSuccess": MUSIC_ORGANIZE_TEMPLATE},
    )
    message = Message(ctype=ContentType.OrganizeSuccess)

    MessageTemplateHelper.render(message, **MUSIC_CONTEXT)

    assert message.title == "晴天 (2003) #3 已入库"
    assert "艺术家：周杰伦" in message.text


def test_message_without_template_config_stays_unchanged(
        notification_templates: SystemConfigOper,
) -> None:
    """
    数据库中没有模板配置时消息应保持原样，不应渲染也不应报错。
    """
    notification_templates.set(SystemConfigKey.NotificationTemplates, None)
    message = Message(ctype=ContentType.OrganizeSuccess)

    MessageTemplateHelper.render(message, **MUSIC_CONTEXT)

    assert message.title is None
    assert message.text is None


def test_v3_migration_overwrites_templates_once(monkeypatch) -> None:
    """
    V3 大版本迁移应无条件覆盖用户旧通知模板配置，并写入全部 4 类模板。
    """
    migration_path = (
            Path(__file__).resolve().parent.parent
            / "database" / "versions" / "4dadad1d161a_3_0_0.py"
    )
    spec = importlib.util.spec_from_file_location("v3_migration", migration_path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    systemconfig = sa.Table(
        "systemconfig",
        metadata,
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column("value", sa.JSON()),
    )
    with engine.begin() as connection:
        metadata.create_all(connection)
        connection.execute(
            systemconfig.insert().values(
                key=SystemConfigKey.NotificationTemplates.value,
                value={"organizeSuccess": "用户自定义模板"},
            )
        )
        context = MigrationContext.configure(connection)
        monkeypatch.setattr(migration, "op", Operations(context))

        migration.upgrade()
        templates = connection.execute(
            sa.select(systemconfig.c.value).where(
                systemconfig.c.key == SystemConfigKey.NotificationTemplates.value
            )
        ).scalar_one()

    assert set(templates.keys()) == {
        "organizeSuccess", "downloadAdded", "subscribeAdded", "subscribeComplete",
    }
    assert templates["organizeSuccess"] != "用户自定义模板"
    assert "音乐" in templates["organizeSuccess"]
    assert "音乐" in templates["downloadAdded"]
