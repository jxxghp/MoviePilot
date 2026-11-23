"""
迁移旧版允许保存、但现有 Python/Rust 规则语法无法解析的自定义 ID。

规则定义与完整操作数引用在同一 Alembic 事务内更新；不重新序列化业务字段，
不清理规则组、订阅或全局配置。历史迁移固定使用此时的语法与内置 ID 集合。
"""

import copy
import re
from collections import Counter
from typing import Any

import sqlalchemy as sa
from alembic import op

from app.runtime.log import logger
from app.schemas.types import SystemConfigKey

revision = "a7383c1d9e20"
down_revision = "b849e613a72f"
branch_labels = None
depends_on = None

# 两套解析器的 ID 都只能包含 ASCII 字母、数字，且必须至少包含一个字母。
_RULE_ID = re.compile(r"[A-Za-z0-9]*[A-Za-z][A-Za-z0-9]*\Z")
_OPERAND = re.compile(r"[^!&|()>]+")
# 固定迁移时的内置 ID，避免历史迁移随运行代码变化而覆盖内置规则。
_BUILTIN_IDS = frozenset({
    "BLU", "4K", "1080P", "720P", "CNSUB", "GZ", "SPECSUB", "BLURAY", "UHD",
    "H265", "H264", "DOLBY", "ATMOS", "HDR", "SDR", "REMUX", "WEBDL", "FREE",
    "CNVOI", "HKVOI", "60FPS", "3D", "HIRES", "LOSSLESS", "FLAC", "ALAC", "APE",
    "WAV", "DSD", "MP3", "AAC", "OPUS", "BITRATE320", "BITRATE256", "BITRATE192",
})


def _rename_rules(custom_rules: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """
    保留规则顺序和全部业务字段，为无效 ID 分配不冲突的稳定新 ID。
    """
    ids = [rule.get("id") for rule in custom_rules]
    if any(not isinstance(rule_id, str) for rule_id in ids) or len(set(ids)) != len(ids):
        raise RuntimeError("自定义过滤规则存在缺失或重复 ID，无法安全迁移")
    occupied = _BUILTIN_IDS | set(ids)
    renames = {}
    updated = copy.deepcopy(custom_rules)
    for rule in updated:
        old_id = rule["id"]
        if _RULE_ID.fullmatch(old_id):
            continue
        base = re.sub(r"[^A-Za-z0-9]", "", old_id)
        if not _RULE_ID.fullmatch(base):
            base = f"Rule{base}"
        new_id = base
        suffix = 1
        while new_id in occupied:
            new_id = f"{base}{suffix}"
            suffix += 1
        occupied = occupied | {new_id}
        renames[old_id] = new_id
        rule["id"] = new_id
    return updated, renames


def _rewrite_groups(
    groups: list[dict[str, Any]], custom_rules: list[dict[str, Any]], renames: dict[str, str],
) -> list[dict[str, Any]]:
    """
    一次性改写完整操作数，保留外围空白，避免片段误替换及连续改名的串扰。

    外围空白属于表达式排版，去除后存在多个候选 ID 时拒绝猜测引用归属。
    """
    aliases = {old_id.strip(): new_id for old_id, new_id in renames.items() if old_id.strip()}
    counts = Counter([*_BUILTIN_IDS, *(rule["id"].strip() for rule in custom_rules)])
    updated = copy.deepcopy(groups)
    for group in updated:
        expression = group.get("rule_string")
        if expression is None:
            continue
        if not isinstance(expression, str):
            raise RuntimeError(f"过滤规则组 {group.get('name')!r} 的表达式不是字符串，无法安全迁移")

        def replace(match: re.Match[str]) -> str:
            """
            只替换完整旧 ID，无法唯一定位时在任何数据库写入前中止迁移。
            """
            span = match.group()
            operand = span.strip()
            if operand not in aliases:
                return span
            if counts[operand] > 1:
                raise RuntimeError(f"过滤规则组 {group.get('name')!r} 的旧规则 ID {operand!r} 无法唯一定位，请先手动改名")
            start = len(span) - len(span.lstrip())
            end = len(span.rstrip())
            return f"{span[:start]}{aliases[operand]}{span[end:]}"

        group["rule_string"] = _OPERAND.sub(replace, expression)
    return updated


def upgrade() -> None:
    """
    在调用方迁移事务中同步更新 ID 和引用，异常时由 Alembic 整体回滚。
    """
    connection = op.get_bind()
    if "systemconfig" not in sa.inspect(connection).get_table_names():
        return
    table = sa.table("systemconfig", sa.column("key", sa.String()), sa.column("value", sa.JSON()))
    keys = (SystemConfigKey.CustomFilterRules, SystemConfigKey.UserFilterRuleGroups)
    values = dict(connection.execute(
        sa.select(table.c.key, table.c.value).where(table.c.key.in_([key.value for key in keys]))
        .order_by(table.c.key).with_for_update()
    ).tuples().all())
    custom = values.get(SystemConfigKey.CustomFilterRules.value)
    if custom is None or custom == []:
        return
    if not isinstance(custom, list) or any(not isinstance(rule, dict) for rule in custom):
        raise RuntimeError("自定义过滤规则格式无效，无法安全迁移")
    updated_custom, renames = _rename_rules(custom)
    if not renames:
        return
    groups = values.get(SystemConfigKey.UserFilterRuleGroups.value)
    if groups is not None and (not isinstance(groups, list) or any(not isinstance(group, dict) for group in groups)):
        raise RuntimeError("过滤规则组格式无效，无法安全迁移")
    updated_groups = _rewrite_groups(groups or [], custom, renames)
    connection.execute(table.update().where(table.c.key == SystemConfigKey.CustomFilterRules.value)
                       .values(value=updated_custom))
    if updated_groups != (groups or []):
        connection.execute(table.update().where(table.c.key == SystemConfigKey.UserFilterRuleGroups.value)
                           .values(value=updated_groups))
    for old_id, new_id in renames.items():
        logger.info(f"自定义过滤规则 ID 迁移：{old_id!r} -> {new_id!r}")


def downgrade() -> None:
    """
    保留迁移后的合法 ID；不能无依据恢复已失效的旧引用，回退数据需使用升级前备份。
    """
