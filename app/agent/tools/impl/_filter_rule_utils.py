"""过滤规则 Agent 工具共用的校验、查询和引用处理逻辑。"""

import copy
import re
from typing import Any, Dict, Iterable, Optional

from app.runtime.events import eventmanager
from app.application.agentdata import SubscribePort as SubscribeOper
from app.application.configuration import get_configured_system_config as SystemConfigOper
from app.application.rules import RuleHelper
from app.domain.filterrule import RuleParser
from app.domain.filterrule import BUILTIN_RULE_SET
from app.schemas.rule import CustomRule
from app.schemas.system import FilterRuleGroup
from app.schemas.event import ConfigChangeEventData
from app.schemas.types import EventType, SystemConfigKey

RULE_ID_PATTERN = re.compile(r"^[A-Za-z0-9]+$")
RULE_TOKEN_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9]*|[0-9][A-Za-z0-9]+")
NUMERIC_RANGE_PATTERN = re.compile(
    r"^\d+(?:\.\d+)?(?:\s*-\s*\d+(?:\.\d+)?)?$"
)

MEDIA_TYPE_ALIASES = {
    "movie": "电影",
    "film": "电影",
    "tv": "电视剧",
    "series": "电视剧",
    "show": "电视剧",
    "电影": "电影",
    "电视剧": "电视剧",
}

RULE_STRING_SYNTAX = {
    "level_separator": ">",
    "and_operator": "&",
    "not_operator": "!",
    "supported_grouping": "Parentheses are supported inside a single level.",
    "spacing_note": "Prefer spaces around '&', and '>' for readability; use '!RULE' for negation.",
    "match_order": "Levels are evaluated from left to right. The first matched level wins and stops further matching.",
    "match_result": "If no level matches, the torrent is filtered out. If a level matches, the torrent is kept.",
    "writing_workflow": [
        "First query built-in rules and custom rules to learn valid rule IDs.",
        "Compose one priority level with '&', '!' and optional parentheses.",
        "Join multiple priority levels with '>' from highest priority to lowest priority.",
        "Use spaces around '&', and '>' for readability.",
    ],
    "examples": [
        {
            "description": "Prefer torrents with special subtitles and Chinese dubbing at 4K, otherwise fall back to Chinese subtitles and Chinese dubbing at 4K.",
            "rule_string": "SPECSUB & CNVOI & 4K & !BLU & !REMUX & !WEBDL > CNSUB & CNVOI & 4K & !BLU & !REMUX & !WEBDL",
        },
        {
            "description": "Inside one level, require 4K and reject Blu-ray source.",
            "rule_string": "4K & !BLU",
        },
        {
            "description": "Inside one level, accept either special subtitles or Chinese subtitles, then also require 1080P.",
            "rule_string": "(SPECSUB | CNSUB) & 1080P",
        },
    ],
}


def normalize_optional_text(value: Optional[str]) -> Optional[str]:
    """把空白字符串折叠为 None，避免保存无意义的空值。"""
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def normalize_media_type(value: Optional[str]) -> Optional[str]:
    """兼容英中文媒体类型输入，最终统一为后端实际使用的中文值。"""
    value = normalize_optional_text(value)
    if not value:
        return None
    normalized = MEDIA_TYPE_ALIASES.get(value.lower(), value)
    if normalized not in {"电影", "电视剧"}:
        raise ValueError(
            "media_type 仅支持 '电影'、'电视剧'、'movie' 或 'tv'"
        )
    return normalized


def validate_numeric_range(
    field_name: str, value: Optional[str]
) -> Optional[str]:
    """校验 size_range / publish_time 这类单值或区间值。"""
    value = normalize_optional_text(value)
    if not value:
        return None
    if not NUMERIC_RANGE_PATTERN.match(value):
        raise ValueError(
            f"{field_name} 格式无效，支持 '1000' 或 '1000-5000' 这类数字区间格式"
        )

    parts = [float(item.strip()) for item in value.split("-")]
    if len(parts) == 2 and parts[0] > parts[1]:
        raise ValueError(f"{field_name} 区间起始值不能大于结束值")
    return value


def validate_seeders(value: Optional[str]) -> Optional[str]:
    """做种人数最终会被 int() 解析，这里提前拦住非法值。"""
    value = normalize_optional_text(value)
    if not value:
        return None
    if not value.isdigit():
        raise ValueError("seeders 必须是非负整数")
    return value


def get_builtin_rules() -> Dict[str, dict]:
    """返回内置规则的深拷贝，避免调用方误改共享常量。"""
    return copy.deepcopy(BUILTIN_RULE_SET)


def get_custom_rules() -> list[CustomRule]:
    return RuleHelper().get_custom_rules()


def get_rule_groups() -> list[FilterRuleGroup]:
    return RuleHelper().get_rule_groups()


def build_custom_rule_map(rules: Optional[Iterable[CustomRule]] = None) -> Dict[str, CustomRule]:
    return {
        rule.id: rule
        for rule in (rules or get_custom_rules())
        if rule.id
    }


def build_rule_group_map(
    groups: Optional[Iterable[FilterRuleGroup]] = None,
) -> Dict[str, FilterRuleGroup]:
    return {
        group.name: group
        for group in (groups or get_rule_groups())
        if group.name
    }


def extract_rule_tokens(rule_string: Optional[str]) -> list[str]:
    """从规则串里提取规则 ID，用于引用分析和未知规则校验。"""
    if not rule_string:
        return []
    # dict.fromkeys 用来在保留顺序的同时去重，便于展示和报错。
    return list(dict.fromkeys(RULE_TOKEN_PATTERN.findall(rule_string)))


def parse_rule_string(rule_string: str) -> dict:
    """使用后端同款 RuleParser 解析规则串，并拆出每一层的元数据。"""
    normalized = normalize_optional_text(rule_string)
    if not normalized:
        raise ValueError("rule_string 不能为空")

    parser = RuleParser()
    levels = [level.strip() for level in normalized.split(">")]
    if any(not level for level in levels):
        raise ValueError("rule_string 不能包含空层级，请检查 '>' 两侧内容")

    parsed_levels = []
    for index, level in enumerate(levels, start=1):
        try:
            parser.parse(level)
        except Exception as exc:  # pragma: no cover - 依赖 pyparsing 的具体异常
            raise ValueError(f"规则串第 {index} 层语法错误: {exc}") from exc

        parsed_levels.append(
            {
                "priority": index,
                "expression": level,
                "referenced_rules": extract_rule_tokens(level),
            }
        )

    return {
        "rule_string": " > ".join(levels),
        "levels": parsed_levels,
        "referenced_rules": extract_rule_tokens(normalized),
    }


def validate_rule_string(rule_string: str, available_rule_ids: Iterable[str]) -> dict:
    """校验规则串语法和引用规则是否都存在。"""
    parsed = parse_rule_string(rule_string)
    available_ids = set(available_rule_ids)
    unknown_rules = sorted(
        {
            rule_id
            for rule_id in parsed["referenced_rules"]
            if rule_id not in available_ids
        }
    )
    if unknown_rules:
        raise ValueError(
            f"rule_string 引用了不存在的规则: {', '.join(unknown_rules)}"
        )
    return parsed


def serialize_builtin_rule(rule_id: str, payload: dict) -> dict:
    """把内置规则整理成适合 Agent 阅读的结构。"""
    data = copy.deepcopy(payload)
    data["id"] = rule_id
    data["source"] = "builtin"
    return data


def serialize_custom_rule(rule: CustomRule, group_refs: Optional[list[str]] = None) -> dict:
    data = rule.model_dump(exclude_none=True)
    data["source"] = "custom"
    data["referenced_by_rule_groups"] = group_refs or []
    return data


def serialize_rule_group(group: FilterRuleGroup, usage: Optional[dict] = None) -> dict:
    """查询时尽量附带解析结果，便于 Agent 理解优先级层级。"""
    data = group.model_dump(exclude_none=True)
    if group.rule_string:
        try:
            parsed = parse_rule_string(group.rule_string)
            data["levels"] = parsed["levels"]
            data["referenced_rules"] = parsed["referenced_rules"]
            data["syntax_valid"] = True
        except ValueError as exc:
            data["syntax_valid"] = False
            data["syntax_error"] = str(exc)
            data["referenced_rules"] = extract_rule_tokens(group.rule_string)
    else:
        data["syntax_valid"] = False
        data["syntax_error"] = "rule_string 为空"
        data["referenced_rules"] = []
    data["usage"] = usage or default_rule_group_usage()
    return data


def default_rule_group_usage() -> dict:
    return {
        "used_in_global_search": False,
        "used_in_global_subscribe": False,
        "used_in_global_best_version": False,
        "subscribes": [],
    }


async def collect_rule_group_usages(
    group_names: Optional[Iterable[str]] = None,
) -> Dict[str, dict]:
    """收集规则组在全局配置和订阅上的引用情况。"""
    target_names = set(group_names or [])
    search_groups = set(
        SystemConfigOper().get(SystemConfigKey.SearchFilterRuleGroups) or []
    )
    subscribe_groups = set(
        SystemConfigOper().get(SystemConfigKey.SubscribeFilterRuleGroups) or []
    )
    best_version_groups = set(
        SystemConfigOper().get(SystemConfigKey.BestVersionFilterRuleGroups) or []
    )

    usage_map = {
        name: default_rule_group_usage()
        for name in target_names
    }

    def ensure_usage(name: str) -> dict:
        if name not in usage_map:
            usage_map[name] = default_rule_group_usage()
        return usage_map[name]

    for name in search_groups:
        if target_names and name not in target_names:
            continue
        ensure_usage(name)["used_in_global_search"] = True
    for name in subscribe_groups:
        if target_names and name not in target_names:
            continue
        ensure_usage(name)["used_in_global_subscribe"] = True
    for name in best_version_groups:
        if target_names and name not in target_names:
            continue
        ensure_usage(name)["used_in_global_best_version"] = True

    subscribes = await SubscribeOper().async_list()
    for subscribe in subscribes:
        filter_groups = subscribe.filter_groups or []
        for name in filter_groups:
            if target_names and name not in target_names:
                continue
            ensure_usage(name)["subscribes"].append(
                {
                    "subscribe_id": subscribe.id,
                    "name": subscribe.name,
                    "season": subscribe.season,
                    "type": subscribe.type,
                    "username": subscribe.username,
                    "best_version": bool(subscribe.best_version),
                }
            )

    return usage_map


def collect_custom_rule_group_refs(
    rule_groups: Iterable[FilterRuleGroup],
    rule_ids: Optional[Iterable[str]] = None,
) -> Dict[str, list[str]]:
    """收集自定义规则被哪些规则组引用。"""
    target_rule_ids = set(rule_ids or [])
    refs: Dict[str, list[str]] = {
        rule_id: []
        for rule_id in target_rule_ids
    }

    for group in rule_groups:
        if not group.name or not group.rule_string:
            continue
        referenced = set(extract_rule_tokens(group.rule_string))
        for rule_id in referenced:
            if target_rule_ids and rule_id not in target_rule_ids:
                continue
            refs.setdefault(rule_id, []).append(group.name)

    for names in refs.values():
        names.sort()
    return refs


def normalize_custom_rule(
    rule_id: str,
    name: str,
    include: Optional[str],
    exclude: Optional[str],
    size_range: Optional[str],
    seeders: Optional[str],
    publish_time: Optional[str],
    existing_rules: Iterable[CustomRule],
    original_rule_id: Optional[str] = None,
) -> CustomRule:
    """新增/更新自定义规则时统一走这里，避免多处散落校验逻辑。"""
    normalized_rule_id = normalize_optional_text(rule_id)
    normalized_name = normalize_optional_text(name)
    if not normalized_rule_id:
        raise ValueError("rule_id 不能为空")
    if not normalized_name:
        raise ValueError("name 不能为空")
    if not RULE_ID_PATTERN.match(normalized_rule_id):
        raise ValueError("rule_id 仅支持英文字母和数字")
    if (
        normalized_rule_id in BUILTIN_RULE_SET
        and normalized_rule_id != original_rule_id
    ):
        raise ValueError(
            f"rule_id '{normalized_rule_id}' 与内置规则冲突，不能覆盖内置规则"
        )

    for existing_rule in existing_rules:
        if (
            existing_rule.id == normalized_rule_id
            and existing_rule.id != original_rule_id
        ):
            raise ValueError(f"rule_id '{normalized_rule_id}' 已存在")
        if (
            existing_rule.name == normalized_name
            and existing_rule.id != original_rule_id
        ):
            raise ValueError(f"规则名称 '{normalized_name}' 已存在")

    return CustomRule(
        id=normalized_rule_id,
        name=normalized_name,
        include=normalize_optional_text(include),
        exclude=normalize_optional_text(exclude),
        size_range=validate_numeric_range("size_range", size_range),
        seeders=validate_seeders(seeders),
        publish_time=validate_numeric_range("publish_time", publish_time),
    )


def normalize_rule_group(
    name: str,
    rule_string: str,
    media_type: Optional[str],
    category: Optional[str],
    existing_groups: Iterable[FilterRuleGroup],
    available_rule_ids: Iterable[str],
    original_name: Optional[str] = None,
) -> tuple[FilterRuleGroup, dict]:
    """新增/更新规则组时统一校验名字、适用范围和规则串。"""
    normalized_name = normalize_optional_text(name)
    if not normalized_name:
        raise ValueError("规则组名称不能为空")

    for group in existing_groups:
        if group.name == normalized_name and group.name != original_name:
            raise ValueError(f"规则组名称 '{normalized_name}' 已存在")

    normalized_media_type = normalize_media_type(media_type)
    normalized_category = normalize_optional_text(category)
    if normalized_category and not normalized_media_type:
        raise ValueError("设置 category 时必须同时设置 media_type")

    parsed = validate_rule_string(rule_string, available_rule_ids)
    return (
        FilterRuleGroup(
            name=normalized_name,
            rule_string=parsed["rule_string"],
            media_type=normalized_media_type,
            category=normalized_category,
        ),
        parsed,
    )


async def save_system_config(
    key: SystemConfigKey, value: Any
) -> Optional[bool]:
    """通过统一入口保存配置并补发 ConfigChanged 事件。"""
    normalized_value = value
    if isinstance(normalized_value, list):
        normalized_value = [
            item
            for item in normalized_value
            if item is not None and item != ""
        ]
        normalized_value = normalized_value or None

    success = await SystemConfigOper().async_set(key, normalized_value)
    if success:
        await eventmanager.async_send_event(
            etype=EventType.ConfigChanged,
            data=ConfigChangeEventData(
                key=key,
                value=normalized_value,
                change_type="update",
            ),
        )
    return success


def replace_rule_id_in_rule_string(
    rule_string: str, old_rule_id: str, new_rule_id: str
) -> str:
    """只替换完整 token，避免误伤其他规则名。"""
    pattern = re.compile(
        rf"(?<![A-Za-z0-9]){re.escape(old_rule_id)}(?![A-Za-z0-9])"
    )
    return pattern.sub(new_rule_id, rule_string)


def replace_group_name_in_list(
    values: Optional[Iterable[str]], old_name: str, new_name: str
) -> list[str]:
    """更新配置里的规则组名引用，并顺手去重。"""
    result = []
    for value in values or []:
        mapped = new_name if value == old_name else value
        if mapped not in result:
            result.append(mapped)
    return result


async def rename_rule_group_references(old_name: str, new_name: str) -> dict:
    """规则组改名后，联动更新全局设置和订阅引用。"""
    changed = {
        "global_settings": {},
        "subscribes": [],
    }

    for config_key in (
        SystemConfigKey.SearchFilterRuleGroups,
        SystemConfigKey.SubscribeFilterRuleGroups,
        SystemConfigKey.BestVersionFilterRuleGroups,
    ):
        original = SystemConfigOper().get(config_key) or []
        updated = replace_group_name_in_list(original, old_name, new_name)
        if updated != original:
            await save_system_config(config_key, updated)
            changed["global_settings"][config_key.value] = updated

    subscribe_oper = SubscribeOper()
    subscribes = await subscribe_oper.async_list()
    for subscribe in subscribes:
        original = subscribe.filter_groups or []
        updated = replace_group_name_in_list(original, old_name, new_name)
        if updated == original:
            continue
        await subscribe_oper.async_update_filter_groups(subscribe.id, updated)
        changed["subscribes"].append(
            {
                "subscribe_id": subscribe.id,
                "name": subscribe.name,
                "season": subscribe.season,
                "filter_groups": updated,
            }
        )

    return changed


async def remove_rule_group_references(group_name: str) -> dict:
    """删除规则组后，清理全局设置和订阅里的悬空引用。"""
    changed = {
        "global_settings": {},
        "subscribes": [],
    }

    for config_key in (
        SystemConfigKey.SearchFilterRuleGroups,
        SystemConfigKey.SubscribeFilterRuleGroups,
        SystemConfigKey.BestVersionFilterRuleGroups,
    ):
        original = SystemConfigOper().get(config_key) or []
        updated = [value for value in original if value != group_name]
        if updated != original:
            await save_system_config(config_key, updated)
            changed["global_settings"][config_key.value] = updated

    subscribe_oper = SubscribeOper()
    subscribes = await subscribe_oper.async_list()
    for subscribe in subscribes:
        original = subscribe.filter_groups or []
        updated = [value for value in original if value != group_name]
        if updated == original:
            continue
        await subscribe_oper.async_update_filter_groups(subscribe.id, updated)
        changed["subscribes"].append(
            {
                "subscribe_id": subscribe.id,
                "name": subscribe.name,
                "season": subscribe.season,
                "filter_groups": updated,
            }
        )

    return changed
