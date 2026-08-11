"""3.0.1
为下载历史保存音乐实体类型

Revision ID: 6f9a1c2d3e4b
Revises: 4dadad1d161a
Create Date: 2026-08-12
"""

import json

from alembic import op
import sqlalchemy as sa


revision = "6f9a1c2d3e4b"
down_revision = "4dadad1d161a"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    """检查数据表是否已存在指定字段。"""
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return False
    return any(
        column["name"] == column_name
        for column in inspector.get_columns(table_name)
    )


def _parse_note(value: object) -> dict:
    """将不同数据库驱动返回的 JSON 备注统一转换为字典。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _backfill_music_type() -> None:
    """从版本化音乐备注回填旧下载记录的实体类型。"""
    download_history = sa.table(
        "downloadhistory",
        sa.column("id", sa.Integer()),
        sa.column("note", sa.JSON()),
        sa.column("music_type", sa.String()),
    )
    connection = op.get_bind()
    rows = connection.execute(
        sa.select(download_history.c.id, download_history.c.note).where(
            download_history.c.music_type.is_(None)
        )
    ).mappings().all()
    for row in rows:
        note = _parse_note(row["note"])
        music_note = note.get("music")
        media = music_note.get("media") if isinstance(music_note, dict) else None
        music_type = media.get("music_type") if isinstance(media, dict) else None
        if music_type not in {"recording", "album"}:
            continue
        connection.execute(
            download_history.update()
            .where(download_history.c.id == row["id"])
            .values(music_type=music_type)
        )


def upgrade() -> None:
    """增加音乐实体字段，并从旧版下载备注幂等回填。"""
    if not _has_column("downloadhistory", "music_type"):
        op.add_column(
            "downloadhistory",
            sa.Column("music_type", sa.String(), nullable=True),
        )
    _backfill_music_type()


def downgrade() -> None:
    """移除下载历史音乐实体字段。"""
    if _has_column("downloadhistory", "music_type"):
        op.drop_column("downloadhistory", "music_type")
