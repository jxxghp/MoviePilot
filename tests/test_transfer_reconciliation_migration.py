"""整理状态 3.0.17 数据收口与完整迁移链测试。"""

import hashlib
import importlib
import json
import os
import uuid
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.orm import sessionmaker

from app.application.transfer.execution import (
    TransferExecutionCommand,
    TransferExecutionState,
    TransferManualReviewDecision,
    TransferManualReviewQuery,
    TransferStepResult,
)
from app.application.transfer.workflow import (
    TransferPlanCheckpoint,
    TransferPlanItem,
    TransferPlanningInput,
    TransferProviderInvocationSnapshot,
    TransferProviderReference,
)
from app.db.adapters.transfer.admission import (
    TransactionalTransferAdmissionRepository,
)
from app.db.adapters.transfer.execution import (
    TransactionalTransferExecutionRepository,
)

try:
    import psycopg2 as postgres_driver
    from psycopg2 import sql

    POSTGRESQL_DIALECT = "postgresql+psycopg2"
except ModuleNotFoundError:
    import psycopg as postgres_driver
    from psycopg import sql

    POSTGRESQL_DIALECT = "postgresql+psycopg"

INITIAL_MIGRATION = "database.versions.e3d9f4b7c806_3_0_4"
ADMISSION_MIGRATION = "database.versions.b1e7d3f5a9c2_3_0_13"
PLANNING_MIGRATION = "database.versions.c2f8a4d6e1b3_3_0_14"
LEASE_MIGRATION = "database.versions.d3a9e5f7b2c4_3_0_15"
EXECUTION_MIGRATION = "database.versions.e5c7a9b1d3f6_3_0_16"
RECONCILIATION_MIGRATION = "database.versions.f6d8b0c2e4a7_3_0_17"


def _bind_migration(monkeypatch, connection, module_name: str):
    """把指定整理迁移绑定到隔离数据库连接。"""
    migration = importlib.import_module(module_name)
    monkeypatch.setattr(
        migration,
        "op",
        Operations(MigrationContext.configure(connection)),
    )
    return migration


def _canonical_fingerprint(payload: dict[str, object]) -> str:
    """按运行时规范计算测试检查点指纹。"""
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _create_3_0_15_tables(connection) -> sa.Table:
    """创建执行迁移前的 pending 与最小 history 表。"""
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
        sa.Column("updated_at", sa.String(40), nullable=False),
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
    sa.Table(
        "transferhistory",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("src", sa.String()),
        sa.Column("src_storage", sa.String(), nullable=False),
        sa.Column("status", sa.Boolean()),
    )
    metadata.create_all(connection)
    return pending


def _seed_execution_rows(connection, pending: sa.Table) -> None:
    """写入待收口的非法状态以及应保持不变的合法状态。"""
    base = {
        "storage": "local",
        "created_at": "2026-08-27 10:00:00",
        "updated_at": "2026-08-27 10:00:00",
        "last_error": None,
        "state": "accepted",
        "input_version": 1,
        "planning_input": {
            "schema_version": 1,
            "source_fileitem": {"storage": "local", "path": "/source"},
        },
        "input_fingerprint": "input",
        "checkpoint_version": None,
        "checkpoint_payload": None,
        "planned_at": None,
        "lease_owner": None,
        "lease_token": None,
        "lease_expires_at": None,
        "heartbeat_at": None,
        "attempt_count": 0,
    }
    task_ids = (
        "unknown",
        "settling-missing",
        "failed-missing",
        "retry-missing-due",
        "partial-checkpoint",
        "completed",
        "manual-lease",
        "settling-valid",
        "failed-valid",
        "accepted-checkpoint",
        "rejection-checkpoint",
        "invalid-outcome",
        "invalid-overwrite",
    )
    connection.execute(pending.insert(), [
        {
            **base,
            "id": index,
            "task_id": task_id,
            "src_path": f"/{task_id}",
        }
        for index, task_id in enumerate(task_ids, start=1)
    ])


def _execution_checkpoint() -> tuple[dict[str, object], str]:
    """构造合法且可由运行时恢复的零副作用执行检查点。"""
    payload = {
        "schema_version": 1,
        "payload": {"outcome": "succeeded", "preview": True},
        "operation_ids": [],
        "skip_reason": "preview",
    }
    return payload, _canonical_fingerprint(payload)


def test_upgrade_reconciles_invalid_execution_combinations(monkeypatch) -> None:
    """非法执行组合必须留证转人工态，合法结算与失败终态不得被破坏。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        pending = _create_3_0_15_tables(connection)
        _seed_execution_rows(connection, pending)
        execution = _bind_migration(monkeypatch, connection, EXECUTION_MIGRATION)
        execution.upgrade()
        payload, fingerprint = _execution_checkpoint()
        connection.execute(sa.text(
            "UPDATE transferpending SET execution_state = 'future' "
            "WHERE task_id = 'unknown'"
        ))
        connection.execute(sa.text(
            "UPDATE transferpending SET execution_state = 'settling' "
            "WHERE task_id = 'settling-missing'"
        ))
        connection.execute(sa.text(
            "UPDATE transferpending SET execution_state = 'failed' "
            "WHERE task_id = 'failed-missing'"
        ))
        connection.execute(sa.text(
            "UPDATE transferpending SET execution_state = 'retry_wait', "
            "retry_due_at = NULL WHERE task_id = 'retry-missing-due'"
        ))
        connection.execute(sa.text(
            "UPDATE transferpending SET execution_state = 'running', "
            "execution_version = 1 WHERE task_id = 'partial-checkpoint'"
        ))
        connection.execute(sa.text(
            "UPDATE transferpending SET execution_state = 'completed' "
            "WHERE task_id = 'completed'"
        ))
        connection.execute(sa.text(
            "UPDATE transferpending SET execution_state = 'manual_review', "
            "lease_owner = 'old-worker', lease_token = 'old-lease', "
            "lease_expires_at = '2099-01-01 00:00:00.000000', "
            "heartbeat_at = '2026-08-27 10:00:00.000000' "
            "WHERE task_id = 'manual-lease'"
        ))
        current = sa.table(
            "transferpending",
            sa.column("task_id", sa.String(64)),
            sa.column("execution_state", sa.String(32)),
            sa.column("state", sa.String(32)),
            sa.column("input_version", sa.Integer()),
            sa.column("planning_input", sa.JSON()),
            sa.column("input_fingerprint", sa.String(64)),
            sa.column("checkpoint_version", sa.Integer()),
            sa.column("checkpoint_payload", sa.JSON()),
            sa.column("planned_at", sa.String(40)),
            sa.column("execution_version", sa.Integer()),
            sa.column("execution_payload", sa.JSON()),
            sa.column("execution_fingerprint", sa.String(64)),
            sa.column("settlement_revision", sa.Integer()),
            sa.column("terminal_history_id", sa.Integer()),
            sa.column("lease_owner", sa.String(128)),
            sa.column("lease_token", sa.String(64)),
            sa.column("lease_expires_at", sa.String(40)),
        )
        host_input = _planning_input("/settling-valid")
        host_checkpoint = _host_checkpoint(host_input)
        failed_input = _planning_input("/failed-valid")
        failed_checkpoint = _host_checkpoint(failed_input)
        accepted_input = _planning_input("/accepted-checkpoint")
        accepted_checkpoint = _host_checkpoint(accepted_input)
        rejection_input = _planning_input("/rejection-checkpoint")
        rejection_checkpoint = TransferPlanCheckpoint(
            planning_input=rejection_input,
            target_storage="local",
            root_target_path="/library",
            final_target_path="/library/Movies",
            resolved_transfer_type="copy",
            items=(),
            rejection_error="未识别到媒体信息",
            schema_version=1,
        )
        invalid_outcome_input = _planning_input("/invalid-outcome")
        invalid_outcome_checkpoint = _host_checkpoint(invalid_outcome_input)
        invalid_overwrite_input = _planning_input("/invalid-overwrite")
        invalid_overwrite_checkpoint = _host_checkpoint(invalid_overwrite_input)
        for task_id, planning_input, checkpoint, state in (
            (
                "settling-valid",
                host_input,
                host_checkpoint,
                "planned",
            ),
            (
                "failed-valid",
                failed_input,
                failed_checkpoint,
                "planned",
            ),
            (
                "accepted-checkpoint",
                accepted_input,
                accepted_checkpoint,
                "accepted",
            ),
            (
                "rejection-checkpoint",
                rejection_input,
                rejection_checkpoint,
                "accepted",
            ),
            (
                "invalid-outcome",
                invalid_outcome_input,
                invalid_outcome_checkpoint,
                "planned",
            ),
            (
                "invalid-overwrite",
                invalid_overwrite_input,
                invalid_overwrite_checkpoint,
                "planned",
            ),
        ):
            connection.execute(
                current.update()
                .where(current.c.task_id == task_id)
                .values(
                    state=state,
                    input_version=1,
                    planning_input=planning_input.to_payload(),
                    input_fingerprint=planning_input.fingerprint,
                    checkpoint_version=1,
                    checkpoint_payload=checkpoint.to_payload(),
                    planned_at="2026-08-27 10:30:00",
                )
            )
        connection.execute(
            current.update()
            .where(current.c.task_id == "settling-valid")
            .values(
                execution_state="settling",
                execution_version=1,
                execution_payload=payload,
                execution_fingerprint=fingerprint,
                lease_owner="active-worker",
                lease_token="active-lease",
                lease_expires_at="2099-01-01 00:00:00.000000",
            )
        )
        invalid_outcome_payload = {
            **payload,
            "payload": {"outcome": "future", "preview": True},
        }
        invalid_overwrite_payload = {
            **payload,
            "payload": {"outcome": "overwrite_skipped", "preview": True},
        }
        for task_id, invalid_payload in (
            ("invalid-outcome", invalid_outcome_payload),
            ("invalid-overwrite", invalid_overwrite_payload),
        ):
            connection.execute(
                current.update()
                .where(current.c.task_id == task_id)
                .values(
                    execution_state="settling",
                    execution_version=1,
                    execution_payload=invalid_payload,
                    execution_fingerprint=_canonical_fingerprint(invalid_payload),
                )
            )
        connection.execute(sa.text(
            "INSERT INTO transferhistory (id, src, src_storage, status) "
            "VALUES (42, '/failed-valid', 'local', 0)"
        ))
        connection.execute(
            current.update()
            .where(current.c.task_id == "failed-valid")
            .values(
                execution_state="failed",
                execution_version=1,
                execution_payload=payload,
                execution_fingerprint=fingerprint,
                settlement_revision=1,
                terminal_history_id=42,
            )
        )
        connection.execute(sa.text(
            "INSERT INTO transfersettlementreceipt ("
            "task_id, history_id, settlement_revision, outcome, "
            "execution_fingerprint, lease_token, history_status, src, src_storage, "
            "pending_deleted, error, created_at, updated_at"
            ") VALUES ("
            "'failed-valid', 42, 1, 'failed', :fingerprint, 'failed-lease', 0, "
            "'/failed-valid', 'local', 0, 'failed', "
            "'2026-08-27 11:00:00', '2026-08-27 11:00:00'"
            ")"
        ), {"fingerprint": fingerprint})

        reconciliation = _bind_migration(
            monkeypatch,
            connection,
            RECONCILIATION_MIGRATION,
        )
        reconciliation.upgrade()
        reconciliation.upgrade()

        rows = {
            row["task_id"]: dict(row)
            for row in connection.execute(sa.text(
                "SELECT task_id, execution_state, execution_version, "
                "execution_payload, execution_fingerprint, lease_owner, lease_token "
                "FROM transferpending"
            )).mappings()
        }
        invalid = {
            "unknown",
            "settling-missing",
            "failed-missing",
            "retry-missing-due",
            "partial-checkpoint",
            "completed",
            "invalid-outcome",
            "invalid-overwrite",
        }
        assert {rows[task_id]["execution_state"] for task_id in invalid} == {
            "manual_review"
        }
        assert all(
            rows[task_id]["execution_version"] is None
            and rows[task_id]["execution_payload"] is None
            and rows[task_id]["execution_fingerprint"] is None
            for task_id in invalid
        )
        assert rows["manual-lease"]["execution_state"] == "manual_review"
        assert rows["manual-lease"]["lease_owner"] is None
        assert rows["manual-lease"]["lease_token"] is None
        assert rows["settling-valid"]["execution_state"] == "settling"
        assert rows["settling-valid"]["lease_token"] == "active-lease"
        assert rows["failed-valid"]["execution_state"] == "failed"
        planning_states = dict(connection.execute(sa.text(
            "SELECT task_id, state FROM transferpending "
            "WHERE task_id IN ('accepted-checkpoint', 'rejection-checkpoint')"
        )).all())
        assert planning_states == {
            "accepted-checkpoint": "planned",
            "rejection-checkpoint": "planned",
        }
        assert connection.execute(sa.text(
            "SELECT COUNT(*) FROM transferexecutionstep "
                "WHERE kind = 'legacy_execution_review' "
                "AND task_id IN ('unknown', 'settling-missing', 'failed-missing', "
                "'retry-missing-due', 'partial-checkpoint', 'completed', "
                "'invalid-outcome', 'invalid-overwrite')"
            )).scalar_one() == len(invalid)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    reviews = TransferManualReviewQuery(
        TransactionalTransferExecutionRepository(factory)
    ).list(page=1, page_size=20)
    assert reviews.total == len(invalid) + 1
    assert {item.task_id for item in reviews.items} == invalid | {"manual-lease"}
    engine.dispose()


def test_reconciliation_migration_runs_on_postgresql(monkeypatch) -> None:
    """配置隔离 PostgreSQL 时真实验证人工态归一、租约清理与可逆 DDL。"""
    prefix = "MOVIEPILOT_TEST_POSTGRESQL_"
    host = os.getenv(f"{prefix}HOST")
    database = os.getenv(f"{prefix}DATABASE")
    username = os.getenv(f"{prefix}USERNAME")
    if not host or not database or not username:
        pytest.skip("未配置隔离 PostgreSQL migration 测试库")

    port = os.getenv(f"{prefix}PORT", "5432")
    password = os.getenv(f"{prefix}PASSWORD", "")
    schema = f"transfer_reconciliation_{uuid.uuid4().hex}"
    with postgres_driver.connect(
        host=host,
        port=port,
        dbname=database,
        user=username,
        password=password,
    ) as connection:
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    engine = None
    try:
        engine = sa.create_engine(
            sa.URL.create(
                POSTGRESQL_DIALECT,
                username=username,
                password=password,
                host=host,
                port=int(port),
                database=database,
            ),
            connect_args={"options": f"-csearch_path={schema}"},
        )
        with engine.begin() as connection:
            pending = _create_3_0_15_tables(connection)
            _seed_execution_rows(connection, pending)
            execution = _bind_migration(
                monkeypatch,
                connection,
                EXECUTION_MIGRATION,
            )
            reconciliation = _bind_migration(
                monkeypatch,
                connection,
                RECONCILIATION_MIGRATION,
            )
            execution.upgrade()
            connection.execute(sa.text(
                "UPDATE transferpending SET state = 'manual_review', "
                "execution_state = 'manual_review', lease_owner = 'old-worker', "
                "lease_token = 'old-token', "
                "lease_expires_at = '2099-01-01 00:00:00.000000' "
                "WHERE task_id = 'manual-lease'"
            ))

            reconciliation.upgrade()

            row = connection.execute(sa.text(
                "SELECT state, execution_state, lease_owner, lease_token "
                "FROM transferpending WHERE task_id = 'manual-lease'"
            )).one()
            assert row == ("accepted", "manual_review", None, None)
            reconciliation.downgrade()
            execution.downgrade()
            assert "execution_state" not in {
                column["name"]
                for column in sa.inspect(connection).get_columns("transferpending")
            }
    finally:
        if engine is not None:
            engine.dispose()
        with postgres_driver.connect(
            host=host,
            port=port,
            dbname=database,
            user=username,
            password=password,
        ) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                        sql.Identifier(schema)
                    )
                )


def _planning_input(path: str) -> TransferPlanningInput:
    """构造完整且可持久恢复的规划输入。"""
    return TransferPlanningInput(
        source_fileitem={"storage": "local", "path": path, "type": "file"},
        meta={"name": "Movie", "year": 2026},
        mediainfo={"title": "Movie", "tmdb_id": 42},
        target_directory={"storage": "local", "path": "/library"},
        target_storage="local",
        target_path="/library/Movies",
        requested_transfer_type="copy",
    )


def _host_checkpoint(planning_input: TransferPlanningInput) -> TransferPlanCheckpoint:
    """构造完整宿主计划检查点。"""
    return TransferPlanCheckpoint(
        planning_input=planning_input,
        target_storage="local",
        root_target_path="/library",
        final_target_path="/library/Movies/Movie.mkv",
        resolved_transfer_type="copy",
        items=(TransferPlanItem(
            sequence=0,
            source_fileitem=planning_input.source_fileitem,
            target_storage="local",
            target_path="/library/Movies/Movie.mkv",
        ),),
        schema_version=1,
    )


def _provider_checkpoint(
        planning_input: TransferPlanningInput,
) -> TransferPlanCheckpoint:
    """构造完整 provider 待执行检查点。"""
    invocation = TransferProviderInvocationSnapshot(
        fileitem=planning_input.source_fileitem,
        meta=planning_input.meta,
        meta_kind="MetaVideo",
        mediainfo=planning_input.mediainfo,
        mediainfo_kind="MediaInfo",
    )
    return TransferPlanCheckpoint(
        planning_input=planning_input,
        target_storage="",
        root_target_path="",
        final_target_path="",
        resolved_transfer_type="",
        items=(),
        resolved_meta=invocation.meta,
        resolved_meta_kind=invocation.meta_kind,
        resolved_mediainfo=invocation.mediainfo,
        resolved_mediainfo_kind=invocation.mediainfo_kind,
        legacy_transfer_providers=(TransferProviderReference(
            plugin_id="provider-a",
            plugin_name="Provider A",
        ),),
        provider_invocation=invocation,
        schema_version=1,
    )


def _create_history_table(connection) -> None:
    """创建迁移链所需的最小整理历史表。"""
    connection.execute(sa.text(
        "CREATE TABLE transferhistory ("
        "id INTEGER PRIMARY KEY, src VARCHAR, "
        "src_storage VARCHAR NOT NULL, status BOOLEAN)"
    ))


def _run_upgrade_chain(monkeypatch, connection) -> list[object]:
    """从 3.0.13 顺序升级到 3.0.17 并返回迁移模块。"""
    migrations = [
        _bind_migration(monkeypatch, connection, module_name)
        for module_name in (
            ADMISSION_MIGRATION,
            PLANNING_MIGRATION,
            LEASE_MIGRATION,
            EXECUTION_MIGRATION,
            RECONCILIATION_MIGRATION,
        )
    ]
    for migration in migrations:
        migration.upgrade()
    return migrations


def test_full_legacy_chain_projects_after_downgrade_and_reupgrade(
        monkeypatch,
) -> None:
    """旧登记经完整升级、人工判定、降级再升级后仍可被真实仓储投影。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        initial = _bind_migration(monkeypatch, connection, INITIAL_MIGRATION)
        initial.upgrade()
        _create_history_table(connection)
        connection.execute(sa.text(
            "INSERT INTO transferpending (id, storage, src_path, created_at) VALUES "
            "(1, 'local', '/accepted.mkv', '2026-08-27 09:00:00'), "
            "(2, 'local', '/planned.mkv', '2026-08-27 09:00:01'), "
            "(3, 'local', '/provider.mkv', '2026-08-27 09:00:02')"
        ))
        admission = _bind_migration(
            monkeypatch,
            connection,
            ADMISSION_MIGRATION,
        )
        planning = _bind_migration(
            monkeypatch,
            connection,
            PLANNING_MIGRATION,
        )
        lease = _bind_migration(monkeypatch, connection, LEASE_MIGRATION)
        execution = _bind_migration(
            monkeypatch,
            connection,
            EXECUTION_MIGRATION,
        )
        reconciliation = _bind_migration(
            monkeypatch,
            connection,
            RECONCILIATION_MIGRATION,
        )
        admission.upgrade()
        planning.upgrade()
        lease.upgrade()
        rows = connection.execute(sa.text(
            "SELECT id, task_id, src_path, planning_input FROM transferpending"
        )).mappings().all()
        by_path = {row["src_path"]: row for row in rows}
        for path, checkpoint in (
            (
                "/planned.mkv",
                _host_checkpoint(_planning_input("/planned.mkv")),
            ),
            (
                "/provider.mkv",
                _provider_checkpoint(_planning_input("/provider.mkv")),
            ),
        ):
            planning_input = checkpoint.planning_input
            connection.execute(sa.text(
                "UPDATE transferpending SET state = 'manual_review', "
                "input_version = 1, planning_input = :planning_input, "
                "input_fingerprint = :input_fingerprint, checkpoint_version = 1, "
                "checkpoint_payload = :checkpoint_payload, "
                "planned_at = '2026-08-27 09:30:00', "
                "lease_owner = 'old-worker', lease_token = 'old-token', "
                "lease_expires_at = '2099-01-01 00:00:00.000000' "
                "WHERE id = :id"
            ), {
                "id": by_path[path]["id"],
                "planning_input": json.dumps(planning_input.to_payload()),
                "input_fingerprint": planning_input.fingerprint,
                "checkpoint_payload": json.dumps(checkpoint.to_payload()),
            })
        connection.execute(sa.text(
            "UPDATE transferpending SET state = 'manual_review', "
            "lease_owner = 'old-worker', lease_token = 'old-token', "
            "lease_expires_at = '2099-01-01 00:00:00.000000' WHERE id = 1"
        ))
        execution.upgrade()
        reconciliation.upgrade()
        normalized = dict(connection.execute(sa.text(
            "SELECT src_path, state FROM transferpending ORDER BY id"
        )).all())
        assert normalized == {
            "/accepted.mkv": "accepted",
            "/planned.mkv": "planned",
            "/provider.mkv": "provider_pending",
        }
        assert connection.execute(sa.text(
            "SELECT COUNT(*) FROM transferpending WHERE lease_token IS NOT NULL"
        )).scalar_one() == 0

    factory = sessionmaker(bind=engine, expire_on_commit=False)
    execution_repository = TransactionalTransferExecutionRepository(
        factory,
        local_clock=lambda: datetime(2026, 8, 27, 10, 0, 0),
        lease_clock=lambda: datetime(2026, 8, 27, 2, 0, 0, tzinfo=timezone.utc),
    )
    reviews = TransferManualReviewQuery(execution_repository).list(
        page=1,
        page_size=10,
    )
    assert reviews.total == 3
    command = TransferExecutionCommand(execution_repository)
    for review in reviews.items:
        resolved = command.resolve_manual_review(
            task_id=review.task_id,
            operation_id=review.step.operation_id,
            decision=TransferManualReviewDecision.NOT_APPLIED,
            actor="migration-test",
            reason="确认旧执行未发生",
            result=TransferStepResult(payload={"confirmed": False}),
        )
        assert resolved.state is TransferExecutionState.RETRY_WAIT

    admission_repository = TransactionalTransferAdmissionRepository(factory)
    monkeypatch.setattr(
        admission_repository,
        "_now",
        lambda: "2026-08-27 10:00:01",
    )
    monkeypatch.setattr(
        admission_repository,
        "_lease_now",
        lambda: datetime(2026, 8, 27, 2, 0, 1, tzinfo=timezone.utc),
    )
    claimed_states = set()
    for task_id in (row["task_id"] for row in rows):
        claimed = admission_repository.claim_task(
            task_id=task_id,
            owner_id="migration-worker",
            lease_seconds=60,
        )
        assert claimed is not None
        claimed_states.add(claimed.state)
    assert claimed_states == {"accepted", "planned", "provider_pending"}

    with engine.begin() as connection:
        for migration in (
                reconciliation,
                execution,
                lease,
                planning,
                admission,
        ):
            monkeypatch.setattr(
                migration,
                "op",
                Operations(MigrationContext.configure(connection)),
            )
            migration.downgrade()
        assert {
            column["name"]
            for column in sa.inspect(connection).get_columns("transferpending")
        } == {"id", "storage", "src_path", "created_at"}
        migrations = _run_upgrade_chain(monkeypatch, connection)
        assert connection.execute(sa.text(
            "SELECT COUNT(*) FROM transferpending"
        )).scalar_one() == 3
        assert connection.execute(sa.text(
            "SELECT COUNT(*) FROM transferpending "
            "WHERE state = 'accepted' AND execution_state = 'not_started' "
            "AND lease_token IS NULL"
        )).scalar_one() == 3
        assert all(migration is not None for migration in migrations)

    reupgraded_repository = TransactionalTransferAdmissionRepository(factory)
    monkeypatch.setattr(
        reupgraded_repository,
        "_now",
        lambda: "2026-08-27 10:01:01",
    )
    monkeypatch.setattr(
        reupgraded_repository,
        "_lease_now",
        lambda: datetime(2026, 8, 27, 2, 1, 1, tzinfo=timezone.utc),
    )
    reupgraded = [
        reupgraded_repository.claim_task(
            task_id=row["task_id"],
            owner_id="reupgraded-worker",
            lease_seconds=60,
        )
        for row in rows
    ]
    assert all(item is not None for item in reupgraded)
    assert {item.state for item in reupgraded if item is not None} == {"accepted"}
    engine.dispose()
