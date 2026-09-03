"""分类持久化字段 Alembic 迁移测试。"""

import importlib
from types import SimpleNamespace
from typing import Any, Protocol, cast

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.engine import Connection

from app.application.history import DownloadHistoryWrite, TransferHistoryWrite
from app.application.server.share import ServerSharingService
from app.application.subscription.contract import (
    SubscriptionHistoryPatch,
    SubscriptionHistorySnapshot,
    SubscriptionPatch,
    SubscriptionSnapshot,
)
from app.db.adapters.history.download import _project_history as project_download_history
from app.db.adapters.history.transfer import project_transfer_history
from app.db.adapters.subscription import _project_history as project_subscription_history
from app.db.adapters.subscription import _project_subscription
from app.db.models.downloadhistory import DownloadHistory
from app.db.models.subscribe import Subscribe
from app.db.models.subscribehistory import SubscribeHistory
from app.db.models.transferhistory import TransferHistory
from app.schemas.history import (
    DownloadHistory as DownloadHistorySchema,
)
from app.schemas.history import (
    TransferHistory as TransferHistorySchema,
)
from app.schemas.query import DownloadHistorySnapshot as DownloadHistoryQuery
from app.schemas.query import SubscriptionHistorySnapshot as SubscriptionHistoryQuery
from app.schemas.query import SubscriptionSnapshot as SubscriptionQuery
from app.schemas.query import TransferHistorySnapshot as TransferHistoryQuery
from app.schemas.subscribe import Subscribe as SubscribeSchema
from app.schemas.subscribe import SubscribeShare

_MIGRATION = "database.versions.c9a4d7e2f1b6_3_0_27"
_HISTORY_FIELDS = {
    "media_category_id",
    "classification_rule_id",
    "classification_policy_revision",
    "classification_source",
}
_EXPECTED_FIELDS = {
    "subscribe": {"media_category_id"},
    "subscribehistory": _HISTORY_FIELDS,
    "downloadhistory": _HISTORY_FIELDS,
    "transferhistory": _HISTORY_FIELDS,
}
_LEGACY_PATH_FIELDS = {
    "subscribe": "media_category",
    "subscribehistory": "media_category",
    "downloadhistory": "media_category",
    "transferhistory": "category",
}


class _MigrationModule(Protocol):
    """声明测试使用的分类字段迁移接口。"""

    revision: str
    down_revision: str
    op: Any

    def upgrade(self) -> None:
        """执行升级迁移。"""

    def downgrade(self) -> None:
        """执行降级迁移。"""


def _bind_migration(
    monkeypatch: pytest.MonkeyPatch,
    connection: Connection,
) -> _MigrationModule:
    """把分类字段迁移绑定到隔离 SQLite 连接。"""
    migration = cast(_MigrationModule, importlib.import_module(_MIGRATION))
    monkeypatch.setattr(
        migration,
        "op",
        Operations(MigrationContext.configure(connection)),
    )
    return migration


def _create_legacy_tables(connection: Connection) -> None:
    """创建仅含兼容路径字段和旧记录的四张历史版本表。"""
    metadata = sa.MetaData()
    for table_name, path_field in _LEGACY_PATH_FIELDS.items():
        table = sa.Table(
            table_name,
            metadata,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(path_field, sa.String(), nullable=True),
        )
        connection.execute(sa.schema.CreateTable(table))
        connection.execute(table.insert().values(id=1, **{path_field: f"legacy/{table_name}"}))


def _columns(connection: Connection, table_name: str) -> dict[str, dict[str, Any]]:
    """按字段名返回指定表的反射结构。"""
    return {str(column["name"]): column for column in sa.inspect(connection).get_columns(table_name)}


def _legacy_row(connection: Connection, table_name: str) -> dict[str, Any]:
    """返回迁移前写入的单条兼容路径记录。"""
    table = sa.Table(table_name, sa.MetaData(), autoload_with=connection)
    return dict(connection.execute(sa.select(table)).mappings().one())


def test_classification_persistence_migration_round_trip_is_replay_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """迁移应可重复升降级，并始终保留旧路径且不伪造分类事实。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        _create_legacy_tables(connection)
        migration = _bind_migration(monkeypatch, connection)

        migration.upgrade()
        migration.upgrade()

        for table_name, expected_fields in _EXPECTED_FIELDS.items():
            columns = _columns(connection, table_name)
            assert expected_fields.issubset(columns)
            assert all(columns[field]["nullable"] for field in expected_fields)
            row = _legacy_row(connection, table_name)
            assert row[_LEGACY_PATH_FIELDS[table_name]] == f"legacy/{table_name}"
            assert all(row[field] is None for field in expected_fields)

        migration.downgrade()
        migration.downgrade()

        for table_name, removed_fields in _EXPECTED_FIELDS.items():
            columns = _columns(connection, table_name)
            assert removed_fields.isdisjoint(columns)
            row = _legacy_row(connection, table_name)
            assert row[_LEGACY_PATH_FIELDS[table_name]] == f"legacy/{table_name}"

        migration.upgrade()

        for table_name, expected_fields in _EXPECTED_FIELDS.items():
            row = _legacy_row(connection, table_name)
            assert all(row[field] is None for field in expected_fields)


def test_classification_persistence_models_match_nullable_schema() -> None:
    """ORM 应声明活动稳定引用和三类历史的完整可空分类快照。"""
    models = {
        "subscribe": Subscribe,
        "subscribehistory": SubscribeHistory,
        "downloadhistory": DownloadHistory,
        "transferhistory": TransferHistory,
    }

    for table_name, expected_fields in _EXPECTED_FIELDS.items():
        columns = models[table_name].__table__.c
        assert expected_fields.issubset(columns.keys())
        assert all(columns[field].nullable for field in expected_fields)


def test_classification_persistence_application_contracts_are_complete() -> None:
    """活动订阅只接收稳定引用，历史与写 DTO 保留完整分类事实。"""
    active_patch = SubscriptionPatch(
        {
            "media_category_id": "movie.drama",
            "media_category": "电影/剧情",
        }
    )
    assert active_patch.to_payload() == {
        "media_category_id": "movie.drama",
        "media_category": "电影/剧情",
    }
    with pytest.raises(ValueError, match="未知字段"):
        SubscriptionPatch({"classification_rule_id": "rule.drama"})

    history_values = {
        "name": "示例电影",
        "media_category_id": "movie.drama",
        "media_category": "电影/剧情",
        "classification_rule_id": "rule.drama",
        "classification_policy_revision": 7,
        "classification_source": "automatic",
    }
    history_patch = SubscriptionHistoryPatch.from_subscription(history_values)
    assert history_patch.to_payload() == history_values

    active_snapshot = SubscriptionSnapshot(
        id=1,
        name="示例电影",
        media_category_id="movie.drama",
        media_category="电影/剧情",
    )
    history_snapshot = SubscriptionHistorySnapshot(id=1, **history_values)
    assert active_snapshot.media_category_id == "movie.drama"
    assert history_snapshot.classification_policy_revision == 7

    download_payload = DownloadHistoryWrite(
        path="/downloads/example",
        type="电影",
        title="示例电影",
        media_category_id="movie.drama",
        media_category="电影/剧情",
        classification_rule_id="rule.drama",
        classification_policy_revision=7,
        classification_source="automatic",
    ).to_payload()
    transfer_payload = TransferHistoryWrite(
        src="/downloads/example.mkv",
        media_category_id="movie.drama",
        category="电影/剧情",
        classification_rule_id="rule.drama",
        classification_policy_revision=7,
        classification_source="automatic",
    ).to_payload()
    assert _HISTORY_FIELDS.issubset(download_payload)
    assert _HISTORY_FIELDS.issubset(transfer_payload)


def test_classification_persistence_adapters_project_all_fields() -> None:
    """四个 ORM 投影边界应原样复制稳定引用和历史分类事实。"""
    classification_values = {
        "media_category_id": "movie.drama",
        "classification_rule_id": "rule.drama",
        "classification_policy_revision": 7,
        "classification_source": "automatic",
    }
    active = _project_subscription(
        Subscribe(
            id=1,
            name="示例电影",
            state="N",
            media_category_id="movie.drama",
            media_category="电影/剧情",
        )
    )
    subscription_history = project_subscription_history(
        SubscribeHistory(
            id=1,
            name="示例电影",
            media_category="电影/剧情",
            **classification_values,
        )
    )
    download_history = project_download_history(
        SimpleNamespace(
            id=1,
            path="/downloads/example",
            type="电影",
            title="示例电影",
            media_category="电影/剧情",
            **classification_values,
        )
    )
    transfer_history = project_transfer_history(
        SimpleNamespace(
            id=1,
            category="电影/剧情",
            **classification_values,
        )
    )

    assert active.media_category_id == "movie.drama"
    for snapshot in (subscription_history, download_history, transfer_history):
        assert snapshot.media_category_id == "movie.drama"
        assert snapshot.classification_rule_id == "rule.drama"
        assert snapshot.classification_policy_revision == 7
        assert snapshot.classification_source == "automatic"


def test_classification_persistence_public_schemas_are_complete() -> None:
    """REST、分享和插件查询模型应公开各自需要的分类字段。"""
    history_schemas = (
        SubscribeSchema,
        DownloadHistorySchema,
        TransferHistorySchema,
        SubscriptionHistoryQuery,
        DownloadHistoryQuery,
        TransferHistoryQuery,
    )
    for schema in history_schemas:
        assert _HISTORY_FIELDS.issubset(schema.model_fields)

    assert "media_category_id" in SubscribeSchema.model_fields
    assert "media_category_id" in SubscribeShare.model_fields
    assert "media_category_id" in SubscriptionQuery.model_fields
    assert "media_category_id" in ServerSharingService.SUBSCRIBE_FIELDS
    assert (_HISTORY_FIELDS - {"media_category_id"}).issubset(SubscribeSchema.PUBLIC_WRITE_EXCLUDED_FIELDS)


def test_classification_persistence_migration_revision_chain() -> None:
    """迁移版本应接在系统配置唯一性 3.0.26 revision 之后。"""
    migration = cast(_MigrationModule, importlib.import_module(_MIGRATION))

    assert migration.revision == "c9a4d7e2f1b6"
    assert migration.down_revision == "b6e1f8a3c9d2"
