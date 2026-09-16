"""旧分类斜杠路径迁移和已持久化策略修复测试。"""

import importlib
from collections.abc import Mapping
from typing import Any, Protocol, cast

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.engine import Connection

from app.application.classification.legacy import migrate_legacy_category_config
from app.domain.classification.validation import ClassificationPolicyValidator

_MIGRATION = "database.versions.e7f3a9c1d5b2_3_0_29"


class _MigrationModule(Protocol):
    """声明本测试调用的 Alembic 迁移模块接口。"""

    revision: str
    down_revision: str
    op: Any

    def upgrade(self) -> None:
        """执行升级迁移。"""


def _bind_migration(
    monkeypatch: pytest.MonkeyPatch,
    connection: Connection,
) -> _MigrationModule:
    """把策略路径迁移绑定到隔离 SQLite 连接。"""
    migration = cast(_MigrationModule, importlib.import_module(_MIGRATION))
    monkeypatch.setattr(
        migration,
        "op",
        Operations(MigrationContext.configure(connection)),
    )
    return migration


def _policy_state_payload() -> dict[str, object]:
    """构造包含活动和历史 legacy 斜杠路径的最小策略状态。"""
    legacy_category = {
        "id": "legacy.movie.0123456789abcdef",
        "media_type": "电影",
        "name": "电影/日韩电影",
        "path": ["电影/日韩电影"],
        "enabled": True,
    }
    unchanged_category = {
        "id": "movie.custom",
        "media_type": "电影",
        "name": "用户分类/保留原样",
        "path": ["用户分类/保留原样"],
        "enabled": True,
    }
    snapshot = {
        "schema_version": 2,
        "revision": 2,
        "categories": [legacy_category, unchanged_category],
        "rules": [],
        "fallbacks": {},
        "field_aliases": {},
    }
    history = {
        **snapshot,
        "revision": 1,
        "categories": [legacy_category],
    }
    return {"active": snapshot, "history": [history]}


def _create_system_config_table(connection: Connection) -> None:
    """创建迁移所需的最小系统配置表。"""
    metadata = sa.MetaData()
    sa.Table(
        "systemconfig",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String()),
        sa.Column("value", sa.JSON()),
    )
    metadata.create_all(connection)


def _policy_value(connection: Connection) -> Mapping[str, object]:
    """读取系统配置表中的分类策略 JSON。"""
    table = sa.table(
        "systemconfig",
        sa.column("key", sa.String()),
        sa.column("value", sa.JSON()),
    )
    value = connection.execute(sa.select(table.c.value).where(table.c.key == "MediaClassificationPolicy")).scalar_one()
    return cast(Mapping[str, object], value)


def test_legacy_migration_splits_slash_names_into_safe_path_segments() -> None:
    """新迁移应保留原分类名和稳定身份，仅把斜杠恢复为目录层级。"""
    result = migrate_legacy_category_config(
        {
            "movie": {
                "电影/日韩电影": {"genre_ids": "16"},
                "兜底": None,
            },
            "tv": {},
        }
    )

    category = next(item for item in result.policy.categories if item.name == "电影/日韩电影")
    assert category.path == ["电影", "日韩电影"]
    assert result.valid
    assert not result.issues
    assert ClassificationPolicyValidator.validate(
        result.policy,
        result.extra_fields,
    ).valid


def test_persisted_policy_migration_repairs_active_and_history_without_touching_other_categories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """数据库迁移应修复活动和历史版本，并保留非 legacy 分类原值。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        _create_system_config_table(connection)
        table = sa.table(
            "systemconfig",
            sa.column("key", sa.String()),
            sa.column("value", sa.JSON()),
        )
        original = _policy_state_payload()
        connection.execute(
            table.insert().values(
                key="MediaClassificationPolicy",
                value=original,
            )
        )
        migration = _bind_migration(monkeypatch, connection)

        migration.upgrade()
        repaired = _policy_value(connection)
        active = cast(Mapping[str, object], repaired["active"])
        history = cast(list[Mapping[str, object]], repaired["history"])
        active_categories = cast(list[Mapping[str, object]], active["categories"])
        history_categories = cast(list[Mapping[str, object]], history[0]["categories"])

        assert active_categories[0]["path"] == ["电影", "日韩电影"]
        assert history_categories[0]["path"] == ["电影", "日韩电影"]
        assert active_categories[1]["path"] == ["用户分类/保留原样"]
        assert active["revision"] == 2
        assert history[0]["revision"] == 1

        migration.upgrade()
        assert _policy_value(connection) == repaired


def test_persisted_policy_migration_revision_chain() -> None:
    """策略路径修复迁移应接在 3.0.28 检查点迁移之后。"""
    migration = cast(_MigrationModule, importlib.import_module(_MIGRATION))

    assert migration.revision == "e7f3a9c1d5b2"
    assert migration.down_revision == "d8f2b6a4c1e7"
