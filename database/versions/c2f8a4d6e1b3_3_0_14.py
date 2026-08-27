"""3.0.14 为整理任务增加版本化规划输入与原子计划检查点。

Revision ID: c2f8a4d6e1b3
Revises: b1e7d3f5a9c2
Create Date: 2026-08-27
"""

import hashlib
import json

import sqlalchemy as sa
from alembic import op

revision = "c2f8a4d6e1b3"
down_revision = "b1e7d3f5a9c2"
branch_labels = None
depends_on = None

_TABLE_NAME = "transferpending"
_NEW_COLUMNS = {
    "input_version",
    "planning_input",
    "input_fingerprint",
    "checkpoint_version",
    "checkpoint_payload",
    "planned_at",
}


def _column_names() -> set[str]:
    """返回当前待整理登记表的字段集合。"""
    inspector = sa.inspect(op.get_bind())
    if _TABLE_NAME not in inspector.get_table_names():
        return set()
    return {
        column["name"]
        for column in inspector.get_columns(_TABLE_NAME)
    }


def _legacy_planning_payload(storage: str, src_path: str) -> dict[str, object]:
    """构造与 Application 规划输入第一版一致的保守重规划 JSON。"""
    return {
        "schema_version": 1,
        "source_fileitem": {"storage": storage, "path": src_path},
        "meta": None,
        "mediainfo": None,
        "target_directory": None,
        "target_storage": None,
        "target_path": None,
        "requested_transfer_type": None,
        "media_source": None,
        "media_id": None,
        "media_type": None,
        "need_scrape": False,
        "need_rename": True,
        "need_notify": True,
        "overwrite_mode": None,
        "episodes_info": [],
        "preview": False,
        "options": {"legacy_replan": True},
    }


def _fingerprint(payload: dict[str, object]) -> str:
    """按稳定 JSON 编码计算与 Application 一致的输入指纹。"""
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _backfill_planning_input() -> None:
    """为 3.0.13 登记生成可辨识且必须重新规划的最小输入。"""
    pending = sa.table(
        _TABLE_NAME,
        sa.column("id", sa.Integer()),
        sa.column("storage", sa.String()),
        sa.column("src_path", sa.String()),
        sa.column("input_version", sa.Integer()),
        sa.column("planning_input", sa.JSON()),
        sa.column("input_fingerprint", sa.String()),
    )
    connection = op.get_bind()
    rows = connection.execute(
        sa.select(
            pending.c.id,
            pending.c.storage,
            pending.c.src_path,
            pending.c.input_version,
            pending.c.planning_input,
            pending.c.input_fingerprint,
        )
    ).mappings().all()
    for row in rows:
        if (
                row["input_version"] is not None
                and row["planning_input"] is not None
                and row["input_fingerprint"]
        ):
            continue
        payload = row["planning_input"]
        if not isinstance(payload, dict):
            payload = _legacy_planning_payload(row["storage"], row["src_path"])
        payload_version = payload.get("schema_version")
        input_version = row["input_version"]
        if input_version is None:
            input_version = payload_version if isinstance(payload_version, int) else 1
        input_fingerprint = row["input_fingerprint"] or _fingerprint(payload)
        connection.execute(
            pending.update()
            .where(pending.c.id == row["id"])
            .values(
                input_version=input_version,
                planning_input=payload,
                input_fingerprint=input_fingerprint,
            )
        )


def upgrade() -> None:
    """增加规划输入和完整检查点字段并保守回填旧登记。"""
    columns = _column_names()
    if not columns:
        return
    additions = (
        ("input_version", sa.Column("input_version", sa.Integer(), nullable=True)),
        ("planning_input", sa.Column("planning_input", sa.JSON(), nullable=True)),
        (
            "input_fingerprint",
            sa.Column("input_fingerprint", sa.String(length=64), nullable=True),
        ),
        ("checkpoint_version", sa.Column("checkpoint_version", sa.Integer(), nullable=True)),
        ("checkpoint_payload", sa.Column("checkpoint_payload", sa.JSON(), nullable=True)),
        ("planned_at", sa.Column("planned_at", sa.String(length=40), nullable=True)),
    )
    for column_name, column in additions:
        if column_name not in columns:
            op.add_column(_TABLE_NAME, column)

    _backfill_planning_input()
    with op.batch_alter_table(_TABLE_NAME) as batch_op:
        batch_op.alter_column(
            "input_version", existing_type=sa.Integer(), nullable=False
        )
        batch_op.alter_column(
            "planning_input", existing_type=sa.JSON(), nullable=False
        )
        batch_op.alter_column(
            "input_fingerprint",
            existing_type=sa.String(length=64),
            nullable=False,
        )


def downgrade() -> None:
    """移除规划字段并把旧版本无法识别的状态保守恢复为接纳态。"""
    columns = _column_names()
    if not columns or not (_NEW_COLUMNS & columns):
        return
    if "state" in columns:
        pending = sa.table(
            _TABLE_NAME,
            sa.column("state", sa.String()),
        )
        op.get_bind().execute(
            pending.update()
            .where(pending.c.state.in_(("planned", "provider_pending")))
            .values(state="accepted")
        )
    with op.batch_alter_table(_TABLE_NAME) as batch_op:
        for column_name in (
                "planned_at",
                "checkpoint_payload",
                "checkpoint_version",
                "input_fingerprint",
                "planning_input",
                "input_version",
        ):
            if column_name in columns:
                batch_op.drop_column(column_name)
