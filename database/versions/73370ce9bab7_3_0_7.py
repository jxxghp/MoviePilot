"""3.0.7
修复历史订阅 note 字段的双重 JSON 编码

Revision ID: 73370ce9bab7
Revises: f4c8d2a7b1e6
Create Date: 2026-08-17
"""

import json

from alembic import op
import sqlalchemy as sa


revision = "73370ce9bab7"
down_revision = "f4c8d2a7b1e6"
branch_labels = None
depends_on = None


def _parse_legacy_note(value: object):
    """解析历史写入的字符串型 note，兼容一层或两层 JSON 编码。

    2.0 时代旧代码对 JSON 列显式做了 ``json.dumps(note)``，SQLite 下 2.0.3
    的列类型迁移又是空操作，导致读回的值是 ``'[1, 2, 3]'`` 这类字符串而非列表；
    字符串会被响应模型按 ``List[int]`` 校验并触发 500。解析失败按空值处理，
    不丢弃整型数组以外的历史内容。
    """
    parsed = value
    while isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except (TypeError, ValueError):
            return None
    if isinstance(parsed, list) and all(isinstance(item, int) for item in parsed):
        return parsed
    return None


def _repair_subscribe_note() -> None:
    """把 subscribe 表中字符串型 note 回写为真正的 JSON 数组。

    通过 sa.JSON 类型绑定参数写回，SQLite 与 PostgreSQL 都会按各自方言
    序列化，保证后续按模型读回时得到整数列表。
    """
    subscribe = sa.table(
        "subscribe",
        sa.column("id", sa.Integer()),
        sa.column("note", sa.JSON()),
    )
    connection = op.get_bind()
    rows = connection.execute(
        sa.select(subscribe.c.id, subscribe.c.note)
    ).mappings().all()
    for row in rows:
        note = row["note"]
        if not isinstance(note, str):
            continue
        repaired = _parse_legacy_note(note)
        connection.execute(
            subscribe.update()
            .where(subscribe.c.id == row["id"])
            .values(note=repaired)
        )


def upgrade() -> None:
    """修复历史订阅 note 字段的双重 JSON 编码数据。"""
    if "subscribe" not in sa.inspect(op.get_bind()).get_table_names():
        return
    _repair_subscribe_note()


def downgrade() -> None:
    """数据修复不可逆，降级不做任何操作。"""
    pass
