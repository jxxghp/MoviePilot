"""3.0.12 consolidate committed plugin package declarations.

Revision ID: 5f2a9c1e7b4d
Revises: a6c8e2f4b1d3
Create Date: 2026-08-26
"""

from collections.abc import Mapping

import sqlalchemy as sa
from alembic import op

revision = "5f2a9c1e7b4d"
down_revision = "a6c8e2f4b1d3"
branch_labels = None
depends_on = None

_LEGACY_COLUMNS = {
    "system_version",
    "supports_v3",
    "supports_v3t",
}


def _column_names() -> set[str]:
    """返回当前插件身份表字段集合。"""
    inspector = sa.inspect(op.get_bind())
    if "pluginidentity" not in inspector.get_table_names():
        return set()
    return {
        column["name"]
        for column in inspector.get_columns("pluginidentity")
    }


def _legacy_snapshot(row: Mapping[str, object]) -> dict[str, object] | None:
    """把旧固定声明列保守回填为仅供展示的版本化快照。"""
    if row["payload_source_type"] == "unknown":
        return None
    manifest: dict[str, object] = {}
    system_version = row.get("system_version")
    if isinstance(system_version, str) and system_version.strip():
        manifest["system_version"] = system_version.strip()
    runtime = {
        field_name.removeprefix("supports_"): row[field_name]
        for field_name in ("supports_v3", "supports_v3t")
        if isinstance(row.get(field_name), bool)
    }
    return {
        "schema_version": 1,
        "declaration_version": None,
        "manifest_matches_payload": False,
        "manifest": manifest,
        "runtime": runtime,
    }


def _backfill_declared_metadata() -> None:
    """只为仍有旧载荷事实且尚无快照的身份生成保守声明。"""
    identity = sa.table(
        "pluginidentity",
        sa.column("id", sa.Integer()),
        sa.column("payload_source_type", sa.String()),
        sa.column("system_version", sa.String()),
        sa.column("supports_v3", sa.Boolean()),
        sa.column("supports_v3t", sa.Boolean()),
        sa.column("declared_metadata", sa.JSON(none_as_null=True)),
    )
    connection = op.get_bind()
    rows = connection.execute(
        sa.select(
            identity.c.id,
            identity.c.payload_source_type,
            identity.c.system_version,
            identity.c.supports_v3,
            identity.c.supports_v3t,
        ).where(identity.c.declared_metadata.is_(None))
    ).mappings().all()
    for row in rows:
        connection.execute(
            identity.update()
            .where(identity.c.id == row["id"])
            .values(declared_metadata=_legacy_snapshot(row))
        )


def _legacy_values(value: object) -> dict[str, object]:
    """从版本化快照恢复旧版可表达的三个固定声明字段。"""
    if not isinstance(value, Mapping) or value.get("schema_version") != 1:
        return {
            "system_version": None,
            "supports_v3": None,
            "supports_v3t": None,
        }
    manifest = value.get("manifest")
    runtime = value.get("runtime")
    return {
        "system_version": (
            manifest.get("system_version")
            if isinstance(manifest, Mapping)
            and isinstance(manifest.get("system_version"), str)
            else None
        ),
        "supports_v3": (
            runtime.get("v3")
            if isinstance(runtime, Mapping)
            and isinstance(runtime.get("v3"), bool)
            else None
        ),
        "supports_v3t": (
            runtime.get("v3t")
            if isinstance(runtime, Mapping)
            and isinstance(runtime.get("v3t"), bool)
            else None
        ),
    }


def _restore_legacy_columns() -> None:
    """在降级前把当前快照投影回旧版固定声明列。"""
    identity = sa.table(
        "pluginidentity",
        sa.column("id", sa.Integer()),
        sa.column("declared_metadata", sa.JSON(none_as_null=True)),
        sa.column("system_version", sa.String()),
        sa.column("supports_v3", sa.Boolean()),
        sa.column("supports_v3t", sa.Boolean()),
    )
    connection = op.get_bind()
    rows = connection.execute(
        sa.select(identity.c.id, identity.c.declared_metadata)
    ).mappings().all()
    for row in rows:
        connection.execute(
            identity.update()
            .where(identity.c.id == row["id"])
            .values(**_legacy_values(row["declared_metadata"]))
        )


def upgrade() -> None:
    """新增声明快照、离线回填并移除会随 package 扩张的固定列。"""
    columns = _column_names()
    if not columns:
        return
    legacy_columns = _LEGACY_COLUMNS & columns
    if legacy_columns and legacy_columns != _LEGACY_COLUMNS:
        raise RuntimeError("pluginidentity 旧声明字段不完整，拒绝继续迁移")
    if "declared_metadata" not in columns:
        op.add_column(
            "pluginidentity",
            sa.Column(
                "declared_metadata",
                sa.JSON(none_as_null=True),
                nullable=True,
            ),
        )
        columns.add("declared_metadata")
    if _LEGACY_COLUMNS <= columns:
        _backfill_declared_metadata()
    if legacy_columns:
        with op.batch_alter_table("pluginidentity") as batch_op:
            for column_name in sorted(legacy_columns):
                batch_op.drop_column(column_name)


def downgrade() -> None:
    """恢复旧版固定声明列，并删除版本化声明快照。"""
    columns = _column_names()
    if not columns or "declared_metadata" not in columns:
        return
    missing_legacy = _LEGACY_COLUMNS - columns
    if missing_legacy:
        with op.batch_alter_table("pluginidentity") as batch_op:
            if "system_version" in missing_legacy:
                batch_op.add_column(
                    sa.Column("system_version", sa.String(length=128))
                )
            if "supports_v3" in missing_legacy:
                batch_op.add_column(sa.Column("supports_v3", sa.Boolean()))
            if "supports_v3t" in missing_legacy:
                batch_op.add_column(sa.Column("supports_v3t", sa.Boolean()))
    _restore_legacy_columns()
    with op.batch_alter_table("pluginidentity") as batch_op:
        batch_op.drop_column("declared_metadata")
