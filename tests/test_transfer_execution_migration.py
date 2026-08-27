"""整理执行证据 3.0.16 Alembic 迁移的保守升级与可逆性测试。"""

import importlib
from datetime import datetime, timezone
from io import StringIO
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.orm import sessionmaker

from app.application.transfer.execution import (
    TransferExecutionCommand,
    TransferExecutionConflictError,
    TransferExecutionState,
    TransferManualReviewDecision,
    TransferManualReviewQuery,
    TransferStepIntent,
    TransferStepResult,
)
from app.db.adapters.transfer.execution import (
    TransactionalTransferExecutionRepository,
)
from app.db.models.transferpending import TransferPending

MIGRATION = "database.versions.e5c7a9b1d3f6_3_0_16"
LEGACY_DIAGNOSTIC = "升级检测到既有执行迹象，需人工确认后再处理"


def _bind_migration(monkeypatch, connection):
    """把 3.0.16 迁移绑定到隔离 SQLite 连接。"""
    migration = importlib.import_module(MIGRATION)
    monkeypatch.setattr(
        migration,
        "op",
        Operations(MigrationContext.configure(connection)),
    )
    return migration


def _create_legacy_tables(connection) -> tuple[sa.Table, sa.Table]:
    """创建具备 3.0.15 租约字段的最小 pending/history 表。"""
    metadata = sa.MetaData()
    pending = sa.Table(
        "transferpending",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.String(64), nullable=False),
        sa.Column("storage", sa.String(), nullable=False),
        sa.Column("src_path", sa.String(), nullable=False),
        sa.Column("created_at", sa.String()),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("updated_at", sa.String(40)),
        sa.Column("last_error", sa.Text()),
        sa.Column("input_version", sa.Integer(), nullable=False),
        sa.Column("planning_input", sa.JSON(), nullable=False),
        sa.Column("input_fingerprint", sa.String(64), nullable=False),
        sa.Column("checkpoint_version", sa.Integer()),
        sa.Column("checkpoint_payload", sa.JSON()),
        sa.Column("planned_at", sa.String(40)),
        sa.Column("lease_owner", sa.String(128)),
        sa.Column("lease_token", sa.String(64)),
        sa.Column("lease_expires_at", sa.String(40)),
        sa.Column("heartbeat_at", sa.String(40)),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.UniqueConstraint("task_id", name="uq_transferpending_task_id"),
    )
    history = sa.Table(
        "transferhistory",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("src", sa.String()),
        sa.Column("src_storage", sa.String(), nullable=False),
    )
    metadata.create_all(connection)
    return pending, history


def _insert_legacy_rows(connection, pending: sa.Table) -> None:
    """写入安全未开始和多种执行结果未知的旧任务。"""
    base = {
        "storage": "local",
        "created_at": "2026-08-27 10:00:00",
        "updated_at": "2026-08-27 10:00:00",
        "last_error": None,
        "input_version": 1,
        "planning_input": {"schema_version": 1},
        "input_fingerprint": "0" * 64,
        "checkpoint_version": None,
        "checkpoint_payload": None,
        "planned_at": None,
        "lease_owner": None,
        "lease_token": None,
        "lease_expires_at": None,
        "heartbeat_at": None,
        "attempt_count": 0,
    }
    rows = [
        {**base, "id": 1, "task_id": "safe", "src_path": "/safe", "state": "accepted"},
        {
            **base,
            "id": 2,
            "task_id": "planned",
            "src_path": "/planned",
            "state": "planned",
            "checkpoint_version": 1,
            "checkpoint_payload": {"schema_version": 1},
        },
        {
            **base,
            "id": 3,
            "task_id": "attempted",
            "src_path": "/attempted",
            "state": "accepted",
            "attempt_count": 1,
        },
        {
            **base,
            "id": 4,
            "task_id": "provider",
            "src_path": "/provider",
            "state": "provider_pending",
            "checkpoint_version": 1,
            "checkpoint_payload": {"provider": True},
        },
    ]
    connection.execute(pending.insert(), rows)


def _assert_execution_tables_match_models(connection) -> None:
    """断言步骤与 append-only 回执表的字段、约束和索引精确匹配 ORM。"""
    inspector = sa.inspect(connection)
    step_columns = {
        column["name"]: column["nullable"]
        for column in inspector.get_columns("transferexecutionstep")
    }
    assert step_columns == {
        "id": False,
        "task_id": False,
        "operation_id": False,
        "checkpoint_fingerprint": False,
        "ordinal": False,
        "phase": False,
        "kind": False,
        "state": False,
        "attempt_token": True,
        "attempt_count": False,
        "intent_version": False,
        "intent_payload": False,
        "result_version": True,
        "result_payload": True,
        "last_error": True,
        "prepared_at": False,
        "started_at": True,
        "completed_at": True,
        "updated_at": False,
    }
    assert {
        column["name"]: str(column["type"])
        for column in inspector.get_columns("transferexecutionstep")
    } == {
        "id": "INTEGER",
        "task_id": "VARCHAR(64)",
        "operation_id": "VARCHAR(64)",
        "checkpoint_fingerprint": "VARCHAR(64)",
        "ordinal": "INTEGER",
        "phase": "VARCHAR(32)",
        "kind": "VARCHAR(32)",
        "state": "VARCHAR(32)",
        "attempt_token": "VARCHAR(64)",
        "attempt_count": "INTEGER",
        "intent_version": "INTEGER",
        "intent_payload": "JSON",
        "result_version": "INTEGER",
        "result_payload": "JSON",
        "last_error": "TEXT",
        "prepared_at": "VARCHAR(40)",
        "started_at": "VARCHAR(40)",
        "completed_at": "VARCHAR(40)",
        "updated_at": "VARCHAR(40)",
    }
    assert inspector.get_pk_constraint("transferexecutionstep")[
        "constrained_columns"
    ] == ["id"]
    assert [
        (
            item["constrained_columns"],
            item["referred_table"],
            item["referred_columns"],
            item["options"].get("ondelete"),
        )
        for item in inspector.get_foreign_keys("transferexecutionstep")
    ] == [(["task_id"], "transferpending", ["task_id"], "CASCADE")]
    step_uniques = {
        item["name"]: item["column_names"]
        for item in inspector.get_unique_constraints("transferexecutionstep")
    }
    assert step_uniques == {
        "uq_transferexecutionstep_operation_id": ["operation_id"],
        "uq_transferexecutionstep_task_ordinal": ["task_id", "ordinal"],
    }
    step_indexes = {
        item["name"]: item["column_names"]
        for item in inspector.get_indexes("transferexecutionstep")
    }
    assert step_indexes == {
        "ix_transferexecutionstep_task_state_ordinal": [
            "task_id",
            "state",
            "ordinal",
        ],
    }

    receipt_columns = {
        column["name"]: column["nullable"]
        for column in inspector.get_columns("transfersettlementreceipt")
    }
    assert receipt_columns == {
        "id": False,
        "task_id": False,
        "history_id": False,
        "settlement_revision": False,
        "outcome": False,
        "execution_fingerprint": False,
        "lease_token": False,
        "history_status": False,
        "src": True,
        "src_storage": True,
        "pending_deleted": False,
        "error": True,
        "created_at": False,
        "updated_at": False,
    }
    assert {
        column["name"]: str(column["type"])
        for column in inspector.get_columns("transfersettlementreceipt")
    } == {
        "id": "INTEGER",
        "task_id": "VARCHAR(64)",
        "history_id": "INTEGER",
        "settlement_revision": "INTEGER",
        "outcome": "VARCHAR(16)",
        "execution_fingerprint": "VARCHAR(64)",
        "lease_token": "VARCHAR(64)",
        "history_status": "BOOLEAN",
        "src": "VARCHAR",
        "src_storage": "VARCHAR",
        "pending_deleted": "BOOLEAN",
        "error": "TEXT",
        "created_at": "VARCHAR(40)",
        "updated_at": "VARCHAR(40)",
    }
    assert inspector.get_pk_constraint("transfersettlementreceipt")[
        "constrained_columns"
    ] == ["id"]
    assert inspector.get_foreign_keys("transfersettlementreceipt") == []
    receipt_uniques = {
        item["name"]: item["column_names"]
        for item in inspector.get_unique_constraints("transfersettlementreceipt")
    }
    assert receipt_uniques == {
        "uq_transfersettlementreceipt_task_revision": [
            "task_id",
            "settlement_revision",
        ],
    }
    receipt_indexes = {
        item["name"]: item["column_names"]
        for item in inspector.get_indexes("transfersettlementreceipt")
    }
    assert receipt_indexes == {
        "ix_transfersettlementreceipt_history_id": ["history_id"],
        "ix_transfersettlementreceipt_task_revision": [
            "task_id",
            "settlement_revision",
        ],
    }


def test_upgrade_is_conservative_and_repairs_interrupted_indexes(monkeypatch):
    """旧执行迹象必须隔离，重复升级应补齐索引且不覆盖保守状态。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        pending, _ = _create_legacy_tables(connection)
        _insert_legacy_rows(connection, pending)
        migration = _bind_migration(monkeypatch, connection)
        migration.upgrade()
        connection.execute(sa.text(
            "DROP INDEX ix_transferexecutionstep_task_state_ordinal"
        ))
        connection.execute(sa.text(
            "DROP INDEX ix_transfersettlementreceipt_history_id"
        ))
        connection.execute(sa.text(
            "DROP INDEX ix_transfersettlementreceipt_task_revision"
        ))
        migration.upgrade()
        states = dict(connection.execute(sa.text(
            "SELECT task_id, execution_state FROM transferpending ORDER BY id"
        )).all())
        assert states == {
            "safe": "not_started",
            "planned": "manual_review",
            "attempted": "manual_review",
            "provider": "manual_review",
        }
        inspector = sa.inspect(connection)
        assert {
            "transferexecutionstep",
            "transfersettlementreceipt",
        }.issubset(inspector.get_table_names())
        execution_due_index = next(
            index
            for index in inspector.get_indexes("transferpending")
            if index["name"] == "ix_transferpending_execution_due"
        )
        assert execution_due_index["column_names"] == [
            "execution_state",
            "retry_due_at",
            "state",
            "created_at",
            "id",
        ]
        assert "ix_transferexecutionstep_task_state_ordinal" in {
            index["name"] for index in inspector.get_indexes("transferexecutionstep")
        }
        assert "ux_transferhistory_transfer_task_id" in {
            index["name"] for index in inspector.get_indexes("transferhistory")
        }
        _assert_execution_tables_match_models(connection)
    engine.dispose()


def test_downgrade_marks_step_evidence_then_reupgrade_keeps_manual_review(
        monkeypatch,
) -> None:
    """降级不得丢失执行不确定性，再升级也不能把该任务自动重放。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        pending, _ = _create_legacy_tables(connection)
        _insert_legacy_rows(connection, pending)
        migration = _bind_migration(monkeypatch, connection)
        migration.upgrade()
        connection.execute(sa.text(
            "INSERT INTO transferexecutionstep ("
            "task_id, operation_id, checkpoint_fingerprint, ordinal, phase, kind, "
            "state, attempt_token, attempt_count, intent_version, intent_payload, "
            "prepared_at, updated_at"
            ") VALUES ("
            "'safe', 'operation', 'plan', 0, 'transfer', 'copy', "
            "'prepared', NULL, 0, 1, '{}', "
            "'2026-08-27 10:00:00', '2026-08-27 10:00:00'"
            ")"
        ))
        connection.execute(sa.text(
            "UPDATE transferpending SET last_error = '原始失败细节' "
            "WHERE task_id = 'safe'"
        ))
        migration.downgrade()
        assert connection.execute(sa.text(
            "SELECT state FROM transferpending WHERE task_id = 'safe'"
        )).scalar_one() == "accepted"
        assert connection.execute(sa.text(
            "SELECT last_error FROM transferpending WHERE task_id = 'safe'"
        )).scalar_one() == f"原始失败细节\n{LEGACY_DIAGNOSTIC}"
        inspector = sa.inspect(connection)
        assert "transferexecutionstep" not in inspector.get_table_names()
        assert "transfersettlementreceipt" not in inspector.get_table_names()
        assert "execution_state" not in {
            column["name"] for column in inspector.get_columns("transferpending")
        }
        migration.upgrade()
        assert connection.execute(sa.text(
            "SELECT execution_state FROM transferpending WHERE task_id = 'safe'"
        )).scalar_one() == "manual_review"
    engine.dispose()


def test_upgrade_without_pending_table_is_a_safe_noop(monkeypatch):
    """全新数据库尚未执行前置迁移时本版本应安全等待迁移链建表。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        migration = _bind_migration(monkeypatch, connection)
        migration.upgrade()
        assert sa.inspect(connection).get_table_names() == []
    engine.dispose()


def test_execution_table_ddl_compiles_for_postgresql(monkeypatch) -> None:
    """步骤与回执建表 DDL 必须在生产 PostgreSQL 方言下可编译。"""
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": output},
    )
    migration = importlib.import_module(MIGRATION)
    monkeypatch.setattr(migration, "op", Operations(context))
    migration._create_step_table()
    migration._create_receipt_table()
    ddl = output.getvalue()
    assert "CREATE TABLE transferexecutionstep" in ddl
    assert "FOREIGN KEY(task_id) REFERENCES transferpending (task_id) ON DELETE CASCADE" in ddl
    assert "CREATE TABLE transfersettlementreceipt" in ddl
    assert "CONSTRAINT uq_transfersettlementreceipt_task_revision UNIQUE" in ddl


def test_repair_indexes_ignores_postgresql_unique_backing_index(monkeypatch) -> None:
    """PG 唯一约束后端索引应留给约束治理，普通意外索引仍须删除。"""
    migration = importlib.import_module(MIGRATION)
    table_name = "transfersettlementreceipt"
    indexes = [
        {
            "name": "uq_transfersettlementreceipt_task_revision",
            "column_names": ["task_id", "settlement_revision"],
            "unique": True,
            "duplicates_constraint": "uq_transfersettlementreceipt_task_revision",
        },
        {
            "name": "ix_transfersettlementreceipt_task_revision",
            "column_names": ["task_id", "settlement_revision"],
            "unique": False,
        },
        {
            "name": "ix_transfersettlementreceipt_unexpected",
            "column_names": ["outcome"],
            "unique": False,
        },
    ]
    dropped = []
    created = []

    def drop_index(index_name: str, *, table_name: str) -> None:
        """记录删除并模拟 PostgreSQL 反射结果随 DDL 更新。"""
        dropped.append((index_name, table_name))
        indexes[:] = [item for item in indexes if item["name"] != index_name]

    inspector = SimpleNamespace(
        get_table_names=lambda: [table_name],
        get_indexes=lambda inspected_table: list(indexes),
    )
    monkeypatch.setattr(migration.sa, "inspect", lambda bind: inspector)
    monkeypatch.setattr(
        migration,
        "op",
        SimpleNamespace(
            get_bind=lambda: object(),
            drop_index=drop_index,
            create_index=lambda *args, **kwargs: created.append((args, kwargs)),
        ),
    )

    migration._repair_indexes(
        table_name=table_name,
        expected={
            "ix_transfersettlementreceipt_task_revision": (
                "task_id",
                "settlement_revision",
            ),
        },
    )

    assert dropped == [
        ("ix_transfersettlementreceipt_unexpected", table_name),
    ]
    assert created == []
    assert any(
        item["name"] == "uq_transfersettlementreceipt_task_revision"
        for item in indexes
    )


def test_interrupted_column_upgrade_preserves_existing_manual_state(monkeypatch):
    """字段阶段中断后重跑应补齐 schema 且保留已写入的人工复核状态。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        pending, _ = _create_legacy_tables(connection)
        _insert_legacy_rows(connection, pending)
        connection.execute(sa.text(
            "ALTER TABLE transferpending ADD COLUMN execution_state VARCHAR(32)"
        ))
        connection.execute(sa.text(
            "UPDATE transferpending SET execution_state = 'manual_review' "
            "WHERE task_id = 'safe'"
        ))
        connection.execute(sa.text(
            "ALTER TABLE transferhistory ADD COLUMN transfer_task_id VARCHAR(64)"
        ))
        migration = _bind_migration(monkeypatch, connection)
        migration.upgrade()
        assert connection.execute(sa.text(
            "SELECT execution_state FROM transferpending WHERE task_id = 'safe'"
        )).scalar_one() == "manual_review"
        pending_columns = {
            column["name"]
            for column in sa.inspect(connection).get_columns("transferpending")
        }
        assert {
            "execution_fingerprint",
            "retry_generation",
            "retry_requested_by",
            "settlement_revision",
        }.issubset(pending_columns)
        assert "transfer_settlement_revision" in {
            column["name"]
            for column in sa.inspect(connection).get_columns("transferhistory")
        }
    engine.dispose()


def test_upgrade_recreates_empty_partial_execution_tables(monkeypatch) -> None:
    """中断升级留下的空残表应无损重建为完整步骤与回执 schema。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        pending, _ = _create_legacy_tables(connection)
        _insert_legacy_rows(connection, pending)
        connection.execute(sa.text(
            "CREATE TABLE transferexecutionstep ("
            "id INTEGER PRIMARY KEY, task_id VARCHAR(64) NOT NULL)"
        ))
        connection.execute(sa.text(
            "CREATE TABLE transfersettlementreceipt ("
            "id INTEGER PRIMARY KEY, task_id VARCHAR(64) NOT NULL)"
        ))
        migration = _bind_migration(monkeypatch, connection)
        migration.upgrade()
        _assert_execution_tables_match_models(connection)
    engine.dispose()


def test_upgrade_replaces_old_single_task_receipt_unique(monkeypatch) -> None:
    """中断版本的 task 单列唯一约束必须移除，保留数据后允许追加新 revision。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        pending, _ = _create_legacy_tables(connection)
        _insert_legacy_rows(connection, pending)
        migration = _bind_migration(monkeypatch, connection)
        migration.upgrade()
        connection.execute(sa.text(
            "INSERT INTO transfersettlementreceipt ("
            "task_id, history_id, settlement_revision, outcome, "
            "execution_fingerprint, lease_token, history_status, src, src_storage, "
            "pending_deleted, error, created_at, updated_at"
            ") VALUES ("
            "'task-a', 1, 1, 'succeeded', 'fingerprint-1', 'lease-1', 1, "
            "'/src', 'local', 1, NULL, "
            "'2026-08-27 10:00:00', '2026-08-27 10:00:00'"
            ")"
        ))
        with migration.op.batch_alter_table("transfersettlementreceipt") as batch_op:
            batch_op.drop_constraint(
                "uq_transfersettlementreceipt_task_revision",
                type_="unique",
            )
            batch_op.create_unique_constraint(
                "uq_transfersettlementreceipt_task_id",
                ["task_id"],
            )
        migration.upgrade()
        connection.execute(sa.text(
            "INSERT INTO transfersettlementreceipt ("
            "task_id, history_id, settlement_revision, outcome, "
            "execution_fingerprint, lease_token, history_status, src, src_storage, "
            "pending_deleted, error, created_at, updated_at"
            ") VALUES ("
            "'task-a', 2, 2, 'failed', 'fingerprint-2', 'lease-2', 0, "
            "'/src', 'local', 1, 'failed', "
            "'2026-08-27 11:00:00', '2026-08-27 11:00:00'"
            ")"
        ))
        assert connection.execute(sa.text(
            "SELECT settlement_revision FROM transfersettlementreceipt "
            "WHERE task_id = 'task-a' ORDER BY settlement_revision"
        )).scalars().all() == [1, 2]
        _assert_execution_tables_match_models(connection)
    engine.dispose()


@pytest.mark.parametrize(
    ("table_name", "create_sql", "insert_sql"),
    (
        (
            "transferexecutionstep",
            "CREATE TABLE transferexecutionstep ("
            "id INTEGER PRIMARY KEY, task_id VARCHAR(64) NOT NULL)",
            "INSERT INTO transferexecutionstep (id, task_id) VALUES (1, 'planned')",
        ),
        (
            "transfersettlementreceipt",
            "CREATE TABLE transfersettlementreceipt ("
            "id INTEGER PRIMARY KEY, task_id VARCHAR(64) NOT NULL)",
            "INSERT INTO transfersettlementreceipt (id, task_id) "
            "VALUES (1, 'settled')",
        ),
    ),
)
def test_upgrade_rejects_nonempty_partial_execution_table(
        monkeypatch,
        table_name: str,
        create_sql: str,
        insert_sql: str,
) -> None:
    """含数据残表不能猜测修复，且必须报告明确迁移冲突而非缺列 SQL。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        pending, _ = _create_legacy_tables(connection)
        _insert_legacy_rows(connection, pending)
        connection.execute(sa.text(create_sql))
        connection.execute(sa.text(insert_sql))
        migration = _bind_migration(monkeypatch, connection)
        with pytest.raises(
                RuntimeError,
                match=rf"含数据的不完整迁移表 {table_name}.*缺少字段",
        ):
            migration.upgrade()
    engine.dispose()


def test_upgrade_adds_synthetic_review_when_nonmanual_step_already_exists(
        monkeypatch,
) -> None:
    """已有普通步骤不代表可人工判定，迁移仍须补 synthetic 并稳定回填时间。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        pending, _ = _create_legacy_tables(connection)
        _insert_legacy_rows(connection, pending)
        connection.execute(sa.text(
            "UPDATE transferpending SET updated_at = NULL WHERE task_id = 'planned'"
        ))
        migration = _bind_migration(monkeypatch, connection)
        migration._add_pending_columns()
        migration._backfill_pending()
        migration._create_step_table()
        connection.execute(sa.text(
            "INSERT INTO transferexecutionstep ("
            "task_id, operation_id, checkpoint_fingerprint, ordinal, phase, kind, "
            "state, attempt_token, attempt_count, intent_version, intent_payload, "
            "prepared_at, updated_at"
            ") VALUES ("
            "'planned', 'existing-operation', 'existing-plan', 0, 'transfer', 'copy', "
            "'prepared', NULL, 0, 1, '{}', "
            "'2026-08-27 09:00:00', '2026-08-27 09:00:00'"
            ")"
        ))
        migration.upgrade()
        migration.upgrade()
        rows = connection.execute(sa.text(
            "SELECT kind, state, prepared_at FROM transferexecutionstep "
            "WHERE task_id = 'planned' ORDER BY ordinal"
        )).all()
        assert rows == [
            ("copy", "prepared", "2026-08-27 09:00:00"),
            ("legacy_execution_review", "manual_review", "2026-08-27 10:00:00"),
        ]
    engine.dispose()


def test_migrated_legacy_reviews_are_discoverable_resolvable_and_retryable(
        monkeypatch,
) -> None:
    """迁移遗留任务应可分页判定，并在判定后准备真实首步骤。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        pending, _ = _create_legacy_tables(connection)
        _insert_legacy_rows(connection, pending)
        migration = _bind_migration(monkeypatch, connection)
        migration.upgrade()

    factory = sessionmaker(bind=engine, expire_on_commit=False)
    repository = TransactionalTransferExecutionRepository(
        factory,
        local_clock=lambda: datetime(2026, 8, 27, 11, 0, 0),
        lease_clock=lambda: datetime(
            2026,
            8,
            27,
            3,
            0,
            0,
            tzinfo=timezone.utc,
        ),
    )
    query = TransferManualReviewQuery(repository)
    command = TransferExecutionCommand(repository)

    first_page = query.list(page=1, page_size=2)
    second_page = query.list(page=2, page_size=2)
    assert first_page.total == 3
    assert len(first_page.items) == 2
    assert len(second_page.items) == 1
    reviews = {
        item.task_id: item
        for item in (*first_page.items, *second_page.items)
    }
    assert set(reviews) == {"planned", "attempted", "provider"}
    assert all(
        item.step.kind == "legacy_execution_review"
        for item in reviews.values()
    )
    assert query.get(task_id="planned") == reviews["planned"]

    with pytest.raises(
            TransferExecutionConflictError,
            match="没有足够证据证明外部操作已发生",
    ):
        command.resolve_manual_review(
            task_id="planned",
            operation_id=reviews["planned"].step.operation_id,
            decision=TransferManualReviewDecision.APPLIED,
            actor="admin",
            reason="无法仅凭旧状态确认外部结果",
            result=TransferStepResult(payload={"confirmed": True}),
        )
    assert query.get(task_id="planned").state is TransferExecutionState.MANUAL_REVIEW

    resolved = []
    for task_id in ("planned", "attempted"):
        resolved.append(command.resolve_manual_review(
            task_id=task_id,
            operation_id=reviews[task_id].step.operation_id,
            decision=TransferManualReviewDecision.NOT_APPLIED,
            actor="admin",
            reason="已回滚或确认旧步骤未发生",
            result=TransferStepResult(payload={"confirmed": False}),
        ))
    assert all(
        item.state is TransferExecutionState.RETRY_WAIT
        for item in resolved
    )
    retry_page = query.list(
        state=TransferExecutionState.RETRY_WAIT,
        page=1,
        page_size=10,
    )
    assert {item.task_id for item in retry_page.items} == {"planned", "attempted"}
    assert query.get(task_id="planned").step.evidence == {"confirmed": False}
    assert query.get(task_id="attempted").step.evidence == {"confirmed": False}

    with factory() as session:
        rows = list(session.scalars(
            sa.select(TransferPending).where(
                TransferPending.task_id.in_(("planned", "attempted"))
            )
        ).all())
        for row in rows:
            row.lease_owner = "worker"
            row.lease_token = f"lease-{row.task_id}"
            row.lease_expires_at = "2099-01-01 00:00:00.000000"
        session.commit()

    for task_id in ("planned", "attempted"):
        snapshot = repository.get_snapshot(task_id=task_id)
        assert snapshot is not None
        assert snapshot.steps == ()
        prepared = command.prepare(
            task_id=task_id,
            lease_token=f"lease-{task_id}",
            intent=TransferStepIntent.create(
                task_id=task_id,
                checkpoint_fingerprint="f" * 64,
                ordinal=0,
                phase="transfer",
                kind="copy",
                payload={"source": task_id},
            ),
        )
        assert prepared.ordinal == 0
    engine.dispose()
