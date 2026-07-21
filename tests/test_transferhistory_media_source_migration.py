import importlib
from types import SimpleNamespace
from unittest.mock import Mock

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.core.meta import MetaBase
from app.db.transferhistory_oper import TransferHistoryOper
from app.schemas import FileItem


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

    oper.add_fail(
        fileitem=FileItem(
            storage="local",
            path="/downloads/Frieren.mkv",
            type="file",
        ),
        mode="copy",
        meta=meta,
    )

    call = oper.add_force.call_args
    assert call.kwargs["media_source"] == "anilist"
    assert call.kwargs["media_id"] == "154587"
