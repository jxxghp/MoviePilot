import importlib
from types import SimpleNamespace
from unittest.mock import Mock

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.application.history import add_transfer_fail, add_transfer_success
from app.domain.context import MUSIC_ENTITY_ALBUM, MusicInfo
from app.domain.meta.metabase import MetaBase
from app.domain.meta.metamusic import MetaMusic
from app.db.oper.transferhistory import TransferHistoryOper
from app.schemas.file import FileItem
from app.schemas.transfer import TransferInfo


def test_transferhistory_migration_backfills_existing_source_ids(monkeypatch) -> None:
    """迁移应把存量TMDB和豆瓣字段回填到统一来源字段。"""
    migration = importlib.import_module(
        "database.versions.e6a1c4b8d2f0_2_2_13"
    )
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    transfer_history = sa.Table(
        "transferhistory",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tmdbid", sa.Integer()),
        sa.Column("doubanid", sa.String()),
    )

    with engine.begin() as connection:
        metadata.create_all(connection)
        connection.execute(
            transfer_history.insert(),
            [
                {"id": 1, "tmdbid": 123, "doubanid": "ignored"},
                {"id": 2, "tmdbid": None, "doubanid": "456"},
                {"id": 3, "tmdbid": None, "doubanid": None},
            ],
        )
        context = MigrationContext.configure(connection)
        monkeypatch.setattr(migration, "op", Operations(context))

        migration.upgrade()

        migrated = sa.Table(
            "transferhistory",
            sa.MetaData(),
            autoload_with=connection,
        )
        rows = connection.execute(
            sa.select(migrated).order_by(migrated.c.id)
        ).mappings().all()

    assert rows[0]["media_source"] == "themoviedb"
    assert rows[0]["media_id"] == "123"
    assert rows[1]["media_source"] == "douban"
    assert rows[1]["media_id"] == "456"
    assert rows[2]["media_source"] is None
    assert rows[2]["media_id"] is None


def test_failed_transfer_history_preserves_explicit_media_source() -> None:
    """识别失败记录也应保存文件名中显式指定的数据源ID。"""
    oper = object.__new__(TransferHistoryOper)
    oper.add_force = Mock(return_value=SimpleNamespace(id=1))
    meta = MetaBase("Frieren")
    meta.cn_name = "Frieren"
    meta.media_source = "anilist"
    meta.media_id = "154587"

    add_transfer_fail(
        fileitem=FileItem(
            storage="local",
            path="/downloads/Frieren.mkv",
            type="file",
        ),
        mode="copy",
        meta=meta,
        transfer_history_oper=oper,
    )

    call = oper.add_force.call_args
    assert call.kwargs["media_source"] == "anilist"
    assert call.kwargs["media_id"] == "154587"


def test_failed_music_history_preserves_media_type_and_entity_namespace() -> None:
    """未识别音乐的失败记录也应保留音乐类型和单曲实体命名空间。"""
    oper = object.__new__(TransferHistoryOper)
    oper.add_force = Mock(return_value=SimpleNamespace(id=1))
    meta = MetaMusic(
        title="晴天",
        artists=["周杰伦"],
        album="叶惠美",
        media_source="musicbrainz",
        media_id="recording-1",
    )

    add_transfer_fail(
        fileitem=FileItem(
            storage="local",
            path="/downloads/周杰伦 - 晴天.flac",
            type="file",
        ),
        mode="copy",
        meta=meta,
        transfer_history_oper=oper,
    )

    call = oper.add_force.call_args
    assert call.kwargs["type"] == "音乐"
    assert call.kwargs["music_type"] == "recording"


def test_transferhistory_music_migration_is_idempotent(monkeypatch) -> None:
    """整理历史音乐字段迁移应支持重复执行。"""
    migration = importlib.import_module(
        "database.versions.d4f6a8c2e1b7_2_2_17"
    )
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    sa.Table(
        "transferhistory",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
    )

    with engine.begin() as connection:
        metadata.create_all(connection)
        context = MigrationContext.configure(connection)
        monkeypatch.setattr(migration, "op", Operations(context))

        migration.upgrade()
        migration.upgrade()

        columns = {
            column["name"]
            for column in sa.inspect(connection).get_columns("transferhistory")
        }

    assert {"music_type", "total_tracks"}.issubset(columns)


def test_music_audio_quality_migration_is_idempotent(monkeypatch) -> None:
    """音乐音质字段迁移应可重复执行且不覆盖自定义通知模板。"""
    migration = importlib.import_module(
        "database.versions.e8b1c4d7a2f9_2_2_18"
    )
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    for table_name in ("subscribe", "subscribehistory", "transferhistory"):
        sa.Table(
            table_name,
            metadata,
            sa.Column("id", sa.Integer(), primary_key=True),
        )
    systemconfig = sa.Table(
        "systemconfig",
        metadata,
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column("value", sa.JSON()),
    )
    custom_templates = {
        "organizeSuccess": "custom organize template",
        "downloadAdded": "custom download template",
    }

    with engine.begin() as connection:
        metadata.create_all(connection)
        connection.execute(
            systemconfig.insert().values(
                key="NotificationTemplates",
                value=custom_templates,
            )
        )
        context = MigrationContext.configure(connection)
        monkeypatch.setattr(migration, "op", Operations(context))

        migration.upgrade()
        migration.upgrade()

        inspector = sa.inspect(connection)
        subscribe_columns = {
            column["name"] for column in inspector.get_columns("subscribe")
        }
        history_columns = {
            column["name"]
            for column in inspector.get_columns("subscribehistory")
        }
        transfer_columns = {
            column["name"]
            for column in inspector.get_columns("transferhistory")
        }
        stored_templates = connection.execute(
            sa.select(systemconfig.c.value).where(
                systemconfig.c.key == "NotificationTemplates"
            )
        ).scalar_one()

    assert {
        "audio_quality",
        "audio_format",
        "min_bitrate",
        "min_bit_depth",
        "min_sample_rate",
        "current_audio_format",
        "current_bitrate",
        "current_bit_depth",
        "current_sample_rate",
    }.issubset(subscribe_columns)
    assert {"current_priority", "current_audio_format"}.issubset(history_columns)
    assert {
        "audio_format",
        "audio_lossless",
        "bit_depth",
        "sample_rate",
        "bitrate",
    }.issubset(transfer_columns)
    assert stored_templates == custom_templates


def test_transfer_history_preserves_album_entity_context() -> None:
    """整理成功记录应保存整专实体和预期曲目数供 Agent 重试。"""
    oper = object.__new__(TransferHistoryOper)
    oper.add_force = Mock(return_value=SimpleNamespace(id=1))
    media = MusicInfo(
        media_source="musicbrainz",
        media_id="release-group-1",
        music_type=MUSIC_ENTITY_ALBUM,
        title="叶惠美",
        album="叶惠美",
        artists=["周杰伦"],
        total_tracks=11,
    )
    meta = MetaMusic(title="叶惠美", artists=["周杰伦"], total_tracks=11)
    meta.apply_audio_quality("FLAC Lossless 24bit 96kHz 2304kbps")

    add_transfer_success(
        fileitem=FileItem(
            storage="local",
            path="/downloads/叶惠美/01.flac",
            type="file",
        ),
        mode="copy",
        meta=meta,
        mediainfo=media,
        transferinfo=TransferInfo(
            target_item=FileItem(
                storage="local",
                path="/music/周杰伦/叶惠美/01.flac",
                type="file",
            ),
        ),
        transfer_history_oper=oper,
    )

    call = oper.add_force.call_args
    assert call.kwargs["music_type"] == "album"
    assert call.kwargs["total_tracks"] == 11
    assert call.kwargs["audio_format"] == "FLAC"
    assert call.kwargs["audio_lossless"] is True
    assert call.kwargs["bit_depth"] == 24
    assert call.kwargs["sample_rate"] == 96_000
    assert call.kwargs["bitrate"] == 2_304_000
