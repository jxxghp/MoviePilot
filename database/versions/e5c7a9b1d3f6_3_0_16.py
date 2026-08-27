"""3.0.16 增加整理步骤执行证据与幂等终态结算字段。

Revision ID: e5c7a9b1d3f6
Revises: d3a9e5f7b2c4
Create Date: 2026-08-27
"""

from collections.abc import Callable
import hashlib
import json

import sqlalchemy as sa
from alembic import op

revision = "e5c7a9b1d3f6"
down_revision = "d3a9e5f7b2c4"
branch_labels = None
depends_on = None

_PENDING_TABLE = "transferpending"
_HISTORY_TABLE = "transferhistory"
_STEP_TABLE = "transferexecutionstep"
_RECEIPT_TABLE = "transfersettlementreceipt"
_PENDING_INDEX = "ix_transferpending_execution_due"
_HISTORY_INDEX = "ux_transferhistory_transfer_task_id"
_STEP_OPERATION_UNIQUE = "uq_transferexecutionstep_operation_id"
_STEP_ORDINAL_UNIQUE = "uq_transferexecutionstep_task_ordinal"
_STEP_INDEX = "ix_transferexecutionstep_task_state_ordinal"
_RECEIPT_TASK_REVISION_UNIQUE = "uq_transfersettlementreceipt_task_revision"
_RECEIPT_HISTORY_INDEX = "ix_transfersettlementreceipt_history_id"
_RECEIPT_TASK_REVISION_INDEX = "ix_transfersettlementreceipt_task_revision"
_MANUAL_REVIEW_DIAGNOSTIC = "升级检测到既有执行迹象，需人工确认后再处理"
_LEGACY_REVIEW_STEP_KIND = "legacy_execution_review"
_LEGACY_REVIEW_STEP_ORDINAL = 2_147_483_647
_LEGACY_REVIEW_FALLBACK_TIME = "1970-01-01 00:00:00"

_PENDING_COLUMNS = {
    "execution_state",
    "execution_version",
    "execution_payload",
    "execution_fingerprint",
    "retry_generation",
    "retry_count",
    "retry_due_at",
    "retry_requested_by",
    "retry_reason",
    "settlement_revision",
    "terminal_history_id",
    "manual_review_revision",
    "reviewed_at",
    "reviewed_by",
    "review_reason",
    "review_decision",
}
_HISTORY_COLUMNS = {
    "transfer_task_id",
    "transfer_settlement_revision",
}
_STEP_COLUMNS = {
    "id",
    "task_id",
    "operation_id",
    "checkpoint_fingerprint",
    "ordinal",
    "phase",
    "kind",
    "state",
    "attempt_token",
    "attempt_count",
    "intent_version",
    "intent_payload",
    "result_version",
    "result_payload",
    "last_error",
    "prepared_at",
    "started_at",
    "completed_at",
    "updated_at",
}
_STEP_NULLABLE_COLUMNS = {
    "attempt_token",
    "result_version",
    "result_payload",
    "last_error",
    "started_at",
    "completed_at",
}
_STEP_COLUMN_TYPES = {
    "id": ("integer", None),
    "task_id": ("string", 64),
    "operation_id": ("string", 64),
    "checkpoint_fingerprint": ("string", 64),
    "ordinal": ("integer", None),
    "phase": ("string", 32),
    "kind": ("string", 32),
    "state": ("string", 32),
    "attempt_token": ("string", 64),
    "attempt_count": ("integer", None),
    "intent_version": ("integer", None),
    "intent_payload": ("json", None),
    "result_version": ("integer", None),
    "result_payload": ("json", None),
    "last_error": ("text", None),
    "prepared_at": ("string", 40),
    "started_at": ("string", 40),
    "completed_at": ("string", 40),
    "updated_at": ("string", 40),
}
_RECEIPT_COLUMNS = {
    "id",
    "task_id",
    "history_id",
    "settlement_revision",
    "outcome",
    "execution_fingerprint",
    "lease_token",
    "history_status",
    "src",
    "src_storage",
    "pending_deleted",
    "error",
    "created_at",
    "updated_at",
}
_RECEIPT_NULLABLE_COLUMNS = {
    "src",
    "src_storage",
    "error",
}
_RECEIPT_COLUMN_TYPES = {
    "id": ("integer", None),
    "task_id": ("string", 64),
    "history_id": ("integer", None),
    "settlement_revision": ("integer", None),
    "outcome": ("string", 16),
    "execution_fingerprint": ("string", 64),
    "lease_token": ("string", 64),
    "history_status": ("boolean", None),
    "src": ("string", None),
    "src_storage": ("string", None),
    "pending_deleted": ("boolean", None),
    "error": ("text", None),
    "created_at": ("string", 40),
    "updated_at": ("string", 40),
}


def _table_names() -> set[str]:
    """返回当前数据库的表名集合。"""
    return set(sa.inspect(op.get_bind()).get_table_names())


def _column_names(table_name: str) -> set[str]:
    """返回指定表的字段集合。"""
    if table_name not in _table_names():
        return set()
    return {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def _index_names(table_name: str) -> set[str]:
    """返回指定表的显式索引名称集合。"""
    if table_name not in _table_names():
        return set()
    return {
        index["name"]
        for index in sa.inspect(op.get_bind()).get_indexes(table_name)
        if index.get("name")
    }


def _table_row_count(table_name: str) -> int:
    """不依赖业务字段读取迁移表行数，供残缺结构修复决策使用。"""
    return op.get_bind().execute(
        sa.select(sa.func.count()).select_from(sa.table(table_name))
    ).scalar_one()


def _repair_unique_constraints(
        *,
        table_name: str,
        expected: dict[str, tuple[str, ...]],
        create_table: Callable[[], None],
) -> None:
    """把迁移自有表的唯一约束收敛到命名的 ORM 精确集合。"""
    constraints = sa.inspect(op.get_bind()).get_unique_constraints(table_name)
    unnamed = [item for item in constraints if not item.get("name")]
    if unnamed:
        if _table_row_count(table_name) > 0:
            raise RuntimeError(
                f"检测到含数据迁移表 {table_name} 存在未命名唯一约束，"
                "无法安全收敛到当前模型"
            )
        op.drop_table(table_name)
        create_table()
        return
    actual = {
        item["name"]: tuple(item.get("column_names") or ())
        for item in constraints
    }
    to_drop = {
        name
        for name, columns in actual.items()
        if name not in expected or columns != expected[name]
    }
    to_create = {
        name
        for name, columns in expected.items()
        if actual.get(name) != columns
    }
    if not to_drop and not to_create:
        return
    with op.batch_alter_table(table_name) as batch_op:
        for constraint_name in sorted(to_drop):
            batch_op.drop_constraint(constraint_name, type_="unique")
        for constraint_name in sorted(to_create):
            batch_op.create_unique_constraint(
                constraint_name,
                list(expected[constraint_name]),
            )


def _repair_indexes(
        *,
        table_name: str,
        expected: dict[str, tuple[str, ...]],
) -> None:
    """把迁移自有表的显式索引收敛到 ORM 精确集合。"""
    actual = {
        item["name"]: tuple(item.get("column_names") or ())
        for item in sa.inspect(op.get_bind()).get_indexes(table_name)
        if item.get("name") and not item.get("duplicates_constraint")
    }
    for index_name, columns in actual.items():
        if index_name not in expected or columns != expected[index_name]:
            op.drop_index(index_name, table_name=table_name)
    remaining = _index_names(table_name)
    for index_name, columns in expected.items():
        if index_name not in remaining:
            op.create_index(
                index_name,
                table_name,
                list(columns),
                unique=False,
            )


def _column_type_signature(column_type: sa.types.TypeEngine) -> tuple[str, object]:
    """把方言反射类型归一为迁移可稳定比较的类型与长度。"""
    if isinstance(column_type, sa.JSON):
        return "json", None
    if isinstance(column_type, sa.Boolean):
        return "boolean", None
    if isinstance(column_type, sa.Integer):
        return "integer", None
    if isinstance(column_type, sa.Text):
        return "text", None
    if isinstance(column_type, sa.String):
        return "string", column_type.length
    return column_type.__class__.__name__.lower(), None


def _validate_or_recreate_empty_table(
        *,
        table_name: str,
        expected_columns: set[str],
        nullable_columns: set[str],
        expected_types: dict[str, tuple[str, object]],
        expected_foreign_keys: set[tuple[str, str, str, str]],
        create_table: Callable[[], None],
) -> None:
    """校验中断升级留下的表结构；仅空表允许无损重建。"""
    inspected_columns = sa.inspect(op.get_bind()).get_columns(table_name)
    actual_columns = {column["name"] for column in inspected_columns}
    actual_nullable = {
        column["name"]
        for column in inspected_columns
        if column.get("nullable", True)
    }
    missing = expected_columns - actual_columns
    unexpected = actual_columns - expected_columns
    wrong_nullable = {
        column_name
        for column_name in expected_columns & actual_columns
        if (column_name in actual_nullable) != (column_name in nullable_columns)
    }
    wrong_types = {
        column["name"]
        for column in inspected_columns
        if (
            column["name"] in expected_types
            and _column_type_signature(column["type"])
            != expected_types[column["name"]]
        )
    }
    primary_key = tuple(
        sa.inspect(op.get_bind()).get_pk_constraint(table_name)
        .get("constrained_columns") or ()
    )
    foreign_keys = {
        (
            foreign_key["constrained_columns"][0],
            foreign_key["referred_table"],
            foreign_key["referred_columns"][0],
            str(foreign_key.get("options", {}).get("ondelete") or "").upper(),
        )
        for foreign_key in sa.inspect(op.get_bind()).get_foreign_keys(table_name)
        if (
            len(foreign_key.get("constrained_columns") or ()) == 1
            and len(foreign_key.get("referred_columns") or ()) == 1
        )
    }
    wrong_primary_key = primary_key != ("id",)
    wrong_foreign_keys = foreign_keys != expected_foreign_keys
    if not any((
            missing,
            unexpected,
            wrong_nullable,
            wrong_types,
            wrong_primary_key,
            wrong_foreign_keys,
    )):
        return
    row_count = _table_row_count(table_name)
    if row_count == 0:
        op.drop_table(table_name)
        create_table()
        return
    details = ", ".join(filter(None, (
        f"缺少字段 {sorted(missing)}" if missing else "",
        f"未知字段 {sorted(unexpected)}" if unexpected else "",
        f"空值约束不一致 {sorted(wrong_nullable)}" if wrong_nullable else "",
        f"字段类型不一致 {sorted(wrong_types)}" if wrong_types else "",
        "主键不一致" if wrong_primary_key else "",
        "外键不一致" if wrong_foreign_keys else "",
    )))
    raise RuntimeError(
        f"检测到含数据的不完整迁移表 {table_name}（{details}），"
        "无法自动修复，请先恢复该版本的完整表结构后重试"
    )


def _add_pending_columns() -> None:
    """补齐 pending 执行 checkpoint、重试与结算字段。"""
    columns = _column_names(_PENDING_TABLE)
    additions = (
        ("execution_state", sa.Column("execution_state", sa.String(32))),
        ("execution_version", sa.Column("execution_version", sa.Integer())),
        ("execution_payload", sa.Column("execution_payload", sa.JSON())),
        (
            "execution_fingerprint",
            sa.Column("execution_fingerprint", sa.String(64)),
        ),
        ("retry_generation", sa.Column("retry_generation", sa.Integer())),
        ("retry_count", sa.Column("retry_count", sa.Integer())),
        ("retry_due_at", sa.Column("retry_due_at", sa.String(40))),
        ("retry_requested_by", sa.Column("retry_requested_by", sa.String(128))),
        ("retry_reason", sa.Column("retry_reason", sa.Text())),
        ("settlement_revision", sa.Column("settlement_revision", sa.Integer())),
        ("terminal_history_id", sa.Column("terminal_history_id", sa.Integer())),
        ("manual_review_revision", sa.Column("manual_review_revision", sa.Integer())),
        ("reviewed_at", sa.Column("reviewed_at", sa.String(40))),
        ("reviewed_by", sa.Column("reviewed_by", sa.String(128))),
        ("review_reason", sa.Column("review_reason", sa.Text())),
        ("review_decision", sa.Column("review_decision", sa.String(32))),
    )
    for column_name, column in additions:
        if column_name not in columns:
            op.add_column(_PENDING_TABLE, column)


def _backfill_pending() -> None:
    """保守隔离旧执行迹象，并补齐新增非空字段。"""
    columns = _column_names(_PENDING_TABLE)
    if not _PENDING_COLUMNS.issubset(columns):
        return
    pending = sa.table(
        _PENDING_TABLE,
        sa.column("state", sa.String(32)),
        sa.column("checkpoint_version", sa.Integer()),
        sa.column("checkpoint_payload", sa.JSON()),
        sa.column("lease_token", sa.String(64)),
        sa.column("attempt_count", sa.Integer()),
        sa.column("last_error", sa.Text()),
        sa.column("execution_state", sa.String(32)),
        sa.column("retry_generation", sa.Integer()),
        sa.column("retry_count", sa.Integer()),
        sa.column("settlement_revision", sa.Integer()),
        sa.column("manual_review_revision", sa.Integer()),
    )
    uncertain = sa.or_(
        pending.c.state.in_(("provider_pending", "planned", "manual_review")),
        pending.c.checkpoint_version.is_not(None),
        pending.c.lease_token.is_not(None),
        pending.c.attempt_count > 0,
        pending.c.last_error.contains(_MANUAL_REVIEW_DIAGNOSTIC),
    )
    bind = op.get_bind()
    bind.execute(
        pending.update()
        .where(pending.c.execution_state.is_(None), uncertain)
        .values(
            execution_state="manual_review",
            last_error=sa.func.coalesce(
                pending.c.last_error,
                _MANUAL_REVIEW_DIAGNOSTIC,
            ),
        )
    )
    bind.execute(
        pending.update()
        .where(pending.c.execution_state.is_(None))
        .values(execution_state="not_started")
    )
    for column_name in (
            "retry_generation",
            "retry_count",
            "settlement_revision",
            "manual_review_revision",
    ):
        column = getattr(pending.c, column_name)
        bind.execute(
            pending.update().where(column.is_(None)).values({column_name: 0})
        )
    with op.batch_alter_table(_PENDING_TABLE) as batch_op:
        batch_op.alter_column(
            "execution_state",
            existing_type=sa.String(32),
            nullable=False,
        )
        for column_name in (
                "retry_generation",
                "retry_count",
                "settlement_revision",
                "manual_review_revision",
        ):
            batch_op.alter_column(
                column_name,
                existing_type=sa.Integer(),
                nullable=False,
            )


def _add_history_columns() -> None:
    """补齐成功删除 pending 后仍可幂等回读的历史身份字段。"""
    columns = _column_names(_HISTORY_TABLE)
    if "transfer_task_id" not in columns:
        op.add_column(
            _HISTORY_TABLE,
            sa.Column("transfer_task_id", sa.String(64), nullable=True),
        )
    if "transfer_settlement_revision" not in columns:
        op.add_column(
            _HISTORY_TABLE,
            sa.Column("transfer_settlement_revision", sa.Integer(), nullable=True),
        )


def _create_step_table() -> None:
    """按 ORM 契约创建整理步骤执行证据表。"""
    op.create_table(
        _STEP_TABLE,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.String(64), nullable=False),
        sa.Column("operation_id", sa.String(64), nullable=False),
        sa.Column("checkpoint_fingerprint", sa.String(64), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("phase", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("attempt_token", sa.String(64), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("intent_version", sa.Integer(), nullable=False),
        sa.Column("intent_payload", sa.JSON(), nullable=False),
        sa.Column("result_version", sa.Integer(), nullable=True),
        sa.Column("result_payload", sa.JSON(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("prepared_at", sa.String(40), nullable=False),
        sa.Column("started_at", sa.String(40), nullable=True),
        sa.Column("completed_at", sa.String(40), nullable=True),
        sa.Column("updated_at", sa.String(40), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["transferpending.task_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "operation_id",
            name=_STEP_OPERATION_UNIQUE,
        ),
        sa.UniqueConstraint(
            "task_id",
            "ordinal",
            name=_STEP_ORDINAL_UNIQUE,
        ),
    )


def _create_or_repair_step_table() -> None:
    """创建步骤证据表，并在任何查询前拒绝有数据的残缺结构。"""
    if _STEP_TABLE not in _table_names():
        _create_step_table()
    else:
        _validate_or_recreate_empty_table(
            table_name=_STEP_TABLE,
            expected_columns=_STEP_COLUMNS,
            nullable_columns=_STEP_NULLABLE_COLUMNS,
            expected_types=_STEP_COLUMN_TYPES,
            expected_foreign_keys={(
                "task_id",
                _PENDING_TABLE,
                "task_id",
                "CASCADE",
            )},
            create_table=_create_step_table,
        )
    _repair_unique_constraints(
        table_name=_STEP_TABLE,
        expected={
            _STEP_OPERATION_UNIQUE: ("operation_id",),
            _STEP_ORDINAL_UNIQUE: ("task_id", "ordinal"),
        },
        create_table=_create_step_table,
    )


def _create_receipt_table() -> None:
    """按 append-only ORM 契约创建任务终态结算回执表。"""
    op.create_table(
        _RECEIPT_TABLE,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.String(64), nullable=False),
        sa.Column("history_id", sa.Integer(), nullable=False),
        sa.Column("settlement_revision", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("execution_fingerprint", sa.String(64), nullable=False),
        sa.Column("lease_token", sa.String(64), nullable=False),
        sa.Column("history_status", sa.Boolean(), nullable=False),
        sa.Column("src", sa.String(), nullable=True),
        sa.Column("src_storage", sa.String(), nullable=True),
        sa.Column("pending_deleted", sa.Boolean(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("updated_at", sa.String(40), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "task_id",
            "settlement_revision",
            name=_RECEIPT_TASK_REVISION_UNIQUE,
        ),
    )


def _create_or_repair_receipt_table() -> None:
    """创建结算回执表，并只对空的残缺表执行无损重建。"""
    if _RECEIPT_TABLE not in _table_names():
        _create_receipt_table()
    else:
        _validate_or_recreate_empty_table(
            table_name=_RECEIPT_TABLE,
            expected_columns=_RECEIPT_COLUMNS,
            nullable_columns=_RECEIPT_NULLABLE_COLUMNS,
            expected_types=_RECEIPT_COLUMN_TYPES,
            expected_foreign_keys=set(),
            create_table=_create_receipt_table,
        )
    _repair_unique_constraints(
        table_name=_RECEIPT_TABLE,
        expected={
            _RECEIPT_TASK_REVISION_UNIQUE: (
                "task_id",
                "settlement_revision",
            ),
        },
        create_table=_create_receipt_table,
    )
    _repair_indexes(
        table_name=_RECEIPT_TABLE,
        expected={
            _RECEIPT_HISTORY_INDEX: ("history_id",),
            _RECEIPT_TASK_REVISION_INDEX: (
                "task_id",
                "settlement_revision",
            ),
        },
    )


def _legacy_review_identity(*, task_id: str, suffix: str) -> str:
    """生成迁移遗留复核步骤使用的确定性 SHA-256 身份。"""
    return hashlib.sha256(
        f"moviepilot:3.0.16:legacy-review:{suffix}:{task_id}".encode("utf-8")
    ).hexdigest()


def _backfill_legacy_review_steps() -> None:
    """为没有可判定人工步骤的遗留人工态补一条 synthetic 审计步骤。"""
    if _STEP_TABLE not in _table_names():
        return
    pending = sa.table(
        _PENDING_TABLE,
        sa.column("task_id", sa.String(64)),
        sa.column("state", sa.String(32)),
        sa.column("execution_state", sa.String(32)),
        sa.column("attempt_count", sa.Integer()),
        sa.column("last_error", sa.Text()),
        sa.column("created_at", sa.String(40)),
        sa.column("updated_at", sa.String(40)),
    )
    steps = sa.table(
        _STEP_TABLE,
        sa.column("task_id", sa.String(64)),
        sa.column("operation_id", sa.String(64)),
        sa.column("checkpoint_fingerprint", sa.String(64)),
        sa.column("ordinal", sa.Integer()),
        sa.column("phase", sa.String(32)),
        sa.column("kind", sa.String(32)),
        sa.column("state", sa.String(32)),
        sa.column("attempt_token", sa.String(64)),
        sa.column("attempt_count", sa.Integer()),
        sa.column("intent_version", sa.Integer()),
        sa.column("intent_payload", sa.JSON()),
        sa.column("result_version", sa.Integer()),
        sa.column("result_payload", sa.JSON()),
        sa.column("last_error", sa.Text()),
        sa.column("prepared_at", sa.String(40)),
        sa.column("started_at", sa.String(40)),
        sa.column("completed_at", sa.String(40)),
        sa.column("updated_at", sa.String(40)),
    )
    bind = op.get_bind()
    rows = bind.execute(
        sa.select(
            pending.c.task_id,
            pending.c.state,
            pending.c.attempt_count,
            pending.c.last_error,
            pending.c.created_at,
            pending.c.updated_at,
        ).where(
            pending.c.execution_state == "manual_review",
            ~sa.exists(
                sa.select(steps.c.task_id).where(
                    steps.c.task_id == pending.c.task_id,
                    steps.c.state == "manual_review",
                )
            ),
        )
    ).mappings().all()
    values = []
    for row in rows:
        task_id = row["task_id"]
        intent_payload = {
            "schema_version": 1,
            "origin": "3.0.16_migration",
            "legacy_state": row["state"],
            "diagnostic": row["last_error"] or _MANUAL_REVIEW_DIAGNOSTIC,
        }
        checkpoint_fingerprint = hashlib.sha256(
            json.dumps(
                intent_payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        evidence_time = (
            row["updated_at"]
            or row["created_at"]
            or _LEGACY_REVIEW_FALLBACK_TIME
        )
        values.append({
            "task_id": task_id,
            "operation_id": _legacy_review_identity(
                task_id=task_id,
                suffix="operation",
            ),
            "checkpoint_fingerprint": checkpoint_fingerprint,
            "ordinal": _LEGACY_REVIEW_STEP_ORDINAL,
            "phase": "legacy_upgrade",
            "kind": _LEGACY_REVIEW_STEP_KIND,
            "state": "manual_review",
            "attempt_token": None,
            "attempt_count": row["attempt_count"] or 0,
            "intent_version": 1,
            "intent_payload": intent_payload,
            "result_version": None,
            "result_payload": None,
            "last_error": row["last_error"] or _MANUAL_REVIEW_DIAGNOSTIC,
            "prepared_at": evidence_time,
            "started_at": None,
            "completed_at": None,
            "updated_at": evidence_time,
        })
    if values:
        bind.execute(steps.insert(), values)


def upgrade() -> None:
    """增加执行证据、幂等结算身份和恢复调度字段，支持中断重跑。"""
    tables = _table_names()
    if _PENDING_TABLE not in tables:
        return
    _add_pending_columns()
    _backfill_pending()
    if _PENDING_INDEX not in _index_names(_PENDING_TABLE):
        op.create_index(
            _PENDING_INDEX,
            _PENDING_TABLE,
            [
                "execution_state",
                "retry_due_at",
                "state",
                "created_at",
                "id",
            ],
            unique=False,
        )
    if _HISTORY_TABLE in tables:
        _add_history_columns()
        if _HISTORY_INDEX not in _index_names(_HISTORY_TABLE):
            op.create_index(
                _HISTORY_INDEX,
                _HISTORY_TABLE,
                ["transfer_task_id"],
                unique=True,
            )
    _create_or_repair_receipt_table()
    _create_or_repair_step_table()
    _backfill_legacy_review_steps()
    _repair_indexes(
        table_name=_STEP_TABLE,
        expected={
            _STEP_INDEX: ("task_id", "state", "ordinal"),
        },
    )


def _mark_downgrade_uncertain() -> None:
    """在丢弃执行证据前恢复旧版可消费状态并保留再升级诊断。"""
    columns = _column_names(_PENDING_TABLE)
    if "execution_state" not in columns:
        return
    pending = sa.table(
        _PENDING_TABLE,
        sa.column("task_id", sa.String(64)),
        sa.column("state", sa.String(32)),
        sa.column("last_error", sa.Text()),
        sa.column("lease_owner", sa.String(128)),
        sa.column("lease_token", sa.String(64)),
        sa.column("lease_expires_at", sa.String(40)),
        sa.column("heartbeat_at", sa.String(40)),
        sa.column("execution_state", sa.String(32)),
        sa.column("execution_version", sa.Integer()),
        sa.column("execution_fingerprint", sa.String(64)),
        sa.column("retry_count", sa.Integer()),
        sa.column("settlement_revision", sa.Integer()),
        sa.column("terminal_history_id", sa.Integer()),
    )
    uncertain = sa.or_(
        pending.c.execution_state != "not_started",
        pending.c.execution_version.is_not(None),
        pending.c.execution_fingerprint.is_not(None),
        pending.c.retry_count > 0,
        pending.c.settlement_revision > 0,
        pending.c.terminal_history_id.is_not(None),
    )
    if _STEP_TABLE in _table_names():
        steps = sa.table(
            _STEP_TABLE,
            sa.column("task_id", sa.String(64)),
        )
        uncertain = sa.or_(
            uncertain,
            sa.exists(sa.select(steps.c.task_id).where(
                steps.c.task_id == pending.c.task_id
            )),
        )
    op.get_bind().execute(
        pending.update()
        .where(uncertain)
        .values(
            state="accepted",
            lease_owner=None,
            lease_token=None,
            lease_expires_at=None,
            heartbeat_at=None,
            last_error=sa.case(
                (
                    sa.or_(
                        pending.c.last_error.is_(None),
                        pending.c.last_error == "",
                    ),
                    _MANUAL_REVIEW_DIAGNOSTIC,
                ),
                (
                    pending.c.last_error.contains(_MANUAL_REVIEW_DIAGNOSTIC),
                    pending.c.last_error,
                ),
                else_=(
                    pending.c.last_error
                    + "\n"
                    + _MANUAL_REVIEW_DIAGNOSTIC
                ),
            ),
        )
    )


def downgrade() -> None:
    """保守折叠执行状态后移除 3.0.16 字段、索引与步骤表。"""
    if _PENDING_TABLE not in _table_names():
        return
    _mark_downgrade_uncertain()
    if _STEP_TABLE in _table_names():
        op.drop_table(_STEP_TABLE)
    if _RECEIPT_TABLE in _table_names():
        op.drop_table(_RECEIPT_TABLE)
    if _HISTORY_TABLE in _table_names():
        history_columns = _column_names(_HISTORY_TABLE)
        if _HISTORY_INDEX in _index_names(_HISTORY_TABLE):
            op.drop_index(_HISTORY_INDEX, table_name=_HISTORY_TABLE)
        with op.batch_alter_table(_HISTORY_TABLE) as batch_op:
            for column_name in (
                    "transfer_settlement_revision",
                    "transfer_task_id",
            ):
                if column_name in history_columns:
                    batch_op.drop_column(column_name)
    pending_columns = _column_names(_PENDING_TABLE)
    if _PENDING_INDEX in _index_names(_PENDING_TABLE):
        op.drop_index(_PENDING_INDEX, table_name=_PENDING_TABLE)
    with op.batch_alter_table(_PENDING_TABLE) as batch_op:
        for column_name in (
                "terminal_history_id",
                "review_decision",
                "review_reason",
                "reviewed_by",
                "reviewed_at",
                "manual_review_revision",
                "settlement_revision",
                "retry_reason",
                "retry_requested_by",
                "retry_due_at",
                "retry_count",
                "retry_generation",
                "execution_fingerprint",
                "execution_payload",
                "execution_version",
                "execution_state",
        ):
            if column_name in pending_columns:
                batch_op.drop_column(column_name)
