"""durable transfer 分类 checkpoint 3.0.28 数据迁移测试。"""

import importlib
from copy import deepcopy
from typing import Any, Protocol, cast

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.engine import Connection

_MIGRATION = "database.versions.d8f2b6a4c1e7_3_0_28"
_SNAPSHOT_FIELDS = {
    "category_id",
    "library_category",
    "classification_rule_id",
    "classification_policy_revision",
    "classification_source",
}


class _MigrationModule(Protocol):
    """声明测试调用的 3.0.28 Alembic 迁移接口。"""

    revision: str
    down_revision: str
    op: Any

    def upgrade(self) -> None:
        """执行分类 checkpoint 升级。"""

    def downgrade(self) -> None:
        """执行分类 checkpoint 降级。"""


def _bind_migration(
    monkeypatch: pytest.MonkeyPatch,
    connection: Connection,
) -> _MigrationModule:
    """把 3.0.28 迁移绑定到当前 SQLite 事务。"""
    migration = cast(_MigrationModule, importlib.import_module(_MIGRATION))
    monkeypatch.setattr(
        migration,
        "op",
        Operations(MigrationContext.configure(connection)),
    )
    return migration


def _create_tables(connection: Connection) -> tuple[sa.Table, sa.Table]:
    """创建迁移需要的最小 pending 和执行步骤表。"""
    metadata = sa.MetaData()
    pending = sa.Table(
        "transferpending",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.String(64), nullable=False, unique=True),
        sa.Column("checkpoint_version", sa.Integer(), nullable=True),
        sa.Column("checkpoint_payload", sa.JSON(), nullable=True),
        sa.Column("execution_state", sa.String(32), nullable=False),
        sa.Column("execution_payload", sa.JSON(), nullable=True),
    )
    steps = sa.Table(
        "transferexecutionstep",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.String(64), nullable=False),
    )
    metadata.create_all(connection)
    return pending, steps


def _insert_pending(
    connection: Connection,
    pending: sa.Table,
    *,
    row_id: int,
    task_id: str,
    payload: dict[str, Any],
    checkpoint_version: int = 1,
    execution_state: str = "not_started",
    execution_payload: object = None,
) -> None:
    """写入一条可精确控制执行证据的测试任务。"""
    connection.execute(
        pending.insert().values(
            id=row_id,
            task_id=task_id,
            checkpoint_version=checkpoint_version,
            checkpoint_payload=payload,
            execution_state=execution_state,
            execution_payload=execution_payload,
        )
    )


def _read_pending(
    connection: Connection,
    pending: sa.Table,
    task_id: str,
) -> dict[str, Any]:
    """读取一条任务的版本、计划和执行状态。"""
    return dict(
        connection.execute(
            sa.select(pending).where(pending.c.task_id == task_id)
        ).mappings().one()
    )


def _v1_payload(media_payload: object) -> dict[str, Any]:
    """构造仅保留迁移关注字段的 v1 检查点。"""
    return {
        "schema_version": 1,
        "planning_input": {"schema_version": 1},
        "resolved_mediainfo": media_payload,
        "provider_invocation": None,
        "items": [],
    }


def _v2_payload(snapshot: dict[str, object]) -> dict[str, Any]:
    """构造带顶层五字段分类快照的 v2 检查点。"""
    return {
        "schema_version": 2,
        "planning_input": {"schema_version": 1},
        "classification_snapshot": snapshot,
        "items": [],
    }


def test_migration_revision_chain() -> None:
    """3.0.28 必须直接衔接分类持久化 3.0.27。"""
    migration = cast(_MigrationModule, importlib.import_module(_MIGRATION))

    assert migration.revision == "d8f2b6a4c1e7"
    assert migration.down_revision == "c9a4d7e2f1b6"


def test_sqlite_upgrade_is_idempotent_and_supports_reupgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """有效 v1 应冻结最终 effective，重复升级和降级后再升级结果一致。"""
    engine = sa.create_engine("sqlite://")
    media_payload = {
        "classification": {
            "effective": {
                "category_id": "movie.drama",
                "category_path": ["电影", "剧情"],
                "rule_id": "rule.drama",
                "source": "automatic",
            },
            "policy_revision": 7,
        },
        "library_category": "不应覆盖/最终结果",
        "metadata_category": "严禁进入 checkpoint",
    }
    expected_snapshot = {
        "category_id": "movie.drama",
        "library_category": "电影/剧情",
        "classification_rule_id": "rule.drama",
        "classification_policy_revision": 7,
        "classification_source": "automatic",
    }
    with engine.begin() as connection:
        pending, _steps = _create_tables(connection)
        _insert_pending(
            connection,
            pending,
            row_id=1,
            task_id="effective",
            payload=_v1_payload(media_payload),
        )
        migration = _bind_migration(monkeypatch, connection)

        migration.upgrade()
        migration.upgrade()

        upgraded = _read_pending(connection, pending, "effective")
        assert upgraded["checkpoint_version"] == 2
        assert upgraded["checkpoint_payload"]["schema_version"] == 2
        assert upgraded["checkpoint_payload"]["classification_snapshot"] == expected_snapshot
        assert set(upgraded["checkpoint_payload"]["classification_snapshot"]) == _SNAPSHOT_FIELDS

        migration.downgrade()
        downgraded = _read_pending(connection, pending, "effective")
        assert downgraded["checkpoint_version"] == 1
        assert downgraded["checkpoint_payload"]["schema_version"] == 1
        assert "classification_snapshot" not in downgraded["checkpoint_payload"]

        migration.upgrade()
        reupgraded = _read_pending(connection, pending, "effective")
        assert reupgraded["checkpoint_version"] == 2
        assert reupgraded["checkpoint_payload"]["classification_snapshot"] == expected_snapshot


@pytest.mark.parametrize("path_field", ("library_category", "category"))
def test_sqlite_upgrade_uses_provider_legacy_path_and_ignores_metadata(
    monkeypatch: pytest.MonkeyPatch,
    path_field: str,
) -> None:
    """provider v1 两种旧路径都生成 legacy 快照，不能提升描述分类。"""
    engine = sa.create_engine("sqlite://")
    payload = _v1_payload(None)
    payload["provider_invocation"] = {
        "mediainfo": {
            path_field: "音乐/现场",
            "metadata_category": "Album / Live",
        }
    }
    with engine.begin() as connection:
        pending, _steps = _create_tables(connection)
        _insert_pending(
            connection,
            pending,
            row_id=1,
            task_id="provider-legacy",
            payload=payload,
        )
        migration = _bind_migration(monkeypatch, connection)

        migration.upgrade()

        snapshot = _read_pending(
            connection, pending, "provider-legacy"
        )["checkpoint_payload"]["classification_snapshot"]
        assert snapshot == {
            "category_id": None,
            "library_category": "音乐/现场",
            "classification_rule_id": None,
            "classification_policy_revision": None,
            "classification_source": "legacy",
        }
        assert "Album / Live" not in snapshot.values()


def test_sqlite_upgrade_never_rewrites_v1_with_execution_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """运行态、execution payload 和步骤任一存在时都必须保留原 v1 身份。"""
    engine = sa.create_engine("sqlite://")
    original = _v1_payload({"library_category": "电影/旧计划"})
    with engine.begin() as connection:
        pending, steps = _create_tables(connection)
        _insert_pending(
            connection,
            pending,
            row_id=1,
            task_id="running",
            payload=deepcopy(original),
            execution_state="running",
        )
        _insert_pending(
            connection,
            pending,
            row_id=2,
            task_id="execution-payload",
            payload=deepcopy(original),
            execution_payload={},
        )
        _insert_pending(
            connection,
            pending,
            row_id=3,
            task_id="step",
            payload=deepcopy(original),
        )
        connection.execute(steps.insert().values(id=1, task_id="step"))
        migration = _bind_migration(monkeypatch, connection)

        migration.upgrade()

        for task_id in ("running", "execution-payload", "step"):
            row = _read_pending(connection, pending, task_id)
            assert row["checkpoint_version"] == 1
            assert row["checkpoint_payload"] == original


@pytest.mark.parametrize(
    "unsafe_path",
    ("../逃逸", "/绝对路径", "音乐\\逃逸"),
)
def test_sqlite_upgrade_keeps_v1_when_classification_path_is_unsafe(
    monkeypatch: pytest.MonkeyPatch,
    unsafe_path: str,
) -> None:
    """无执行证据的脏路径也不得被迁移规范化成不同的 v2 计划。"""
    engine = sa.create_engine("sqlite://")
    original = _v1_payload({"library_category": unsafe_path})
    with engine.begin() as connection:
        pending, _steps = _create_tables(connection)
        _insert_pending(
            connection,
            pending,
            row_id=1,
            task_id="unsafe-path",
            payload=deepcopy(original),
        )
        migration = _bind_migration(monkeypatch, connection)

        migration.upgrade()

        row = _read_pending(connection, pending, "unsafe-path")
        assert row["checkpoint_version"] == 1
        assert row["checkpoint_payload"] == original


def test_sqlite_downgrade_rejects_execution_evidence_without_partial_rewrite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """任一 v2 已有步骤时必须整体拒绝降级，并保持其他安全行不变。"""
    engine = sa.create_engine("sqlite://")
    snapshot = {
        "category_id": "movie.drama",
        "library_category": "电影/剧情",
        "classification_rule_id": "rule.drama",
        "classification_policy_revision": 7,
        "classification_source": "automatic",
    }
    payload = _v2_payload(snapshot)
    with engine.begin() as connection:
        pending, steps = _create_tables(connection)
        _insert_pending(
            connection,
            pending,
            row_id=1,
            task_id="safe",
            payload=deepcopy(payload),
            checkpoint_version=2,
        )
        _insert_pending(
            connection,
            pending,
            row_id=2,
            task_id="executed",
            payload=deepcopy(payload),
            checkpoint_version=2,
        )
        connection.execute(steps.insert().values(id=1, task_id="executed"))
        migration = _bind_migration(monkeypatch, connection)

        with pytest.raises(
            RuntimeError,
            match="存在执行证据的 v2 整理检查点，拒绝降级",
        ):
            migration.downgrade()

        for task_id in ("safe", "executed"):
            row = _read_pending(connection, pending, task_id)
            assert row["checkpoint_version"] == 2
            assert row["checkpoint_payload"] == payload
