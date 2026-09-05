"""过滤规则查询、校验、序列化和引用处理应用服务。"""

import copy
import re
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, Dict, Iterable, Optional, cast

from app.application.configuration import get_configured_system_config
from app.application.rules import (
    BUILTIN_RULE_SET,
    AsyncRuleGroupMutationService,
    RuleHelper,
    RuleParser,
)
from app.application.subscription.contract import SubscriptionRepository
from app.schemas.rule import CustomRule, FilterRuleGroup
from app.schemas.types import SystemConfigKey

RULE_ID_PATTERN = re.compile(r"^[A-Za-z0-9]+$")
RULE_TOKEN_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9]*|[0-9][A-Za-z0-9]+")
NUMERIC_RANGE_PATTERN = re.compile(r"^\d+(?:\.\d+)?(?:\s*-\s*\d+(?:\.\d+)?)?$")
RuleConfigPublisher = Callable[[SystemConfigKey, Any], Awaitable[None]]

MEDIA_TYPE_ALIASES = {
    "movie": "电影",
    "film": "电影",
    "tv": "电视剧",
    "series": "电视剧",
    "show": "电视剧",
    "music": "音乐",
    "电影": "电影",
    "电视剧": "电视剧",
    "音乐": "音乐",
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
    if normalized not in {"电影", "电视剧", "音乐"}:
        raise ValueError("media_type 仅支持 '电影'、'电视剧'、'音乐'、'movie'、'tv' 或 'music'")
    return normalized


def validate_numeric_range(field_name: str, value: Optional[str]) -> Optional[str]:
    """校验 size_range / publish_time 这类单值或区间值。"""
    value = normalize_optional_text(value)
    if not value:
        return None
    if not NUMERIC_RANGE_PATTERN.match(value):
        raise ValueError(f"{field_name} 格式无效，支持 '1000' 或 '1000-5000' 这类数字区间格式")

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


def get_builtin_rules() -> Dict[str, dict[str, Any]]:
    """返回内置规则的深拷贝，避免调用方误改共享常量。"""
    return copy.deepcopy(BUILTIN_RULE_SET)


def get_custom_rules() -> list[CustomRule]:
    """读取当前配置中的自定义规则。"""
    return RuleHelper().get_custom_rules()


def get_rule_groups() -> list[FilterRuleGroup]:
    """读取当前配置中的过滤规则组。"""
    return RuleHelper().get_rule_groups()


def build_custom_rule_map(rules: Optional[Iterable[CustomRule]] = None) -> Dict[str, CustomRule]:
    return {rule.id: rule for rule in (rules or get_custom_rules()) if rule.id}


def build_rule_group_map(
    groups: Optional[Iterable[FilterRuleGroup]] = None,
) -> Dict[str, FilterRuleGroup]:
    return {group.name: group for group in (groups or get_rule_groups()) if group.name}


def extract_rule_tokens(rule_string: Optional[str]) -> list[str]:
    """从规则串里提取规则 ID，用于引用分析和未知规则校验。"""
    if not rule_string:
        return []
    # dict.fromkeys 用来在保留顺序的同时去重，便于展示和报错。
    return list(dict.fromkeys(RULE_TOKEN_PATTERN.findall(rule_string)))


def parse_rule_string(rule_string: str) -> dict[str, Any]:
    """使用后端同款 RuleParser 解析规则串，并拆出每一层的元数据。"""
    normalized = normalize_optional_text(rule_string)
    if not normalized:
        raise ValueError("rule_string 不能为空")

    parser = RuleParser()
    levels = [level.strip() for level in normalized.split(">")]
    if any(not level for level in levels):
        raise ValueError("rule_string 不能包含空层级，请检查 '>' 两侧内容")

    parsed_levels: list[dict[str, Any]] = []
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


def validate_rule_string(rule_string: str, available_rule_ids: Iterable[str]) -> dict[str, Any]:
    """校验规则串语法和引用规则是否都存在。"""
    parsed = parse_rule_string(rule_string)
    available_ids = set(available_rule_ids)
    unknown_rules = sorted({rule_id for rule_id in parsed["referenced_rules"] if rule_id not in available_ids})
    if unknown_rules:
        raise ValueError(f"rule_string 引用了不存在的规则: {', '.join(unknown_rules)}")
    return parsed


def serialize_builtin_rule(rule_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """把内置规则整理成适合 Agent 阅读的结构。"""
    data = copy.deepcopy(payload)
    data["id"] = rule_id
    data["source"] = "builtin"
    return data


def serialize_custom_rule(
    rule: CustomRule,
    group_refs: Optional[list[str]] = None,
) -> dict[str, Any]:
    data = cast(dict[str, Any], rule.model_dump(exclude_none=True))
    data["source"] = "custom"
    data["referenced_by_rule_groups"] = group_refs or []
    return data


def serialize_rule_group(
    group: FilterRuleGroup,
    usage: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """查询时尽量附带解析结果，便于 Agent 理解优先级层级。"""
    data = cast(dict[str, Any], group.model_dump(exclude_none=True))
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


def default_rule_group_usage() -> dict[str, Any]:
    return {
        "used_in_global_search": False,
        "used_in_global_subscribe": False,
        "used_in_global_best_version": False,
        "subscribes": [],
    }


async def collect_rule_group_usages(
    repository: SubscriptionRepository,
    group_names: Optional[Iterable[str]] = None,
) -> Dict[str, dict[str, Any]]:
    """收集规则组在全局配置和订阅上的引用情况。"""
    target_names = set(group_names or [])
    search_groups = set(get_configured_system_config().get(SystemConfigKey.SearchFilterRuleGroups) or [])
    subscribe_groups = set(get_configured_system_config().get(SystemConfigKey.SubscribeFilterRuleGroups) or [])
    best_version_groups = set(get_configured_system_config().get(SystemConfigKey.BestVersionFilterRuleGroups) or [])

    usage_map: dict[str, dict[str, Any]] = {
        name: default_rule_group_usage() for name in target_names
    }

    def ensure_usage(name: str) -> dict[str, Any]:
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

    subscribes = await repository.async_list()
    for subscribe in subscribes:
        filter_groups = (
            [str(name) for name in subscribe.filter_groups] if isinstance(subscribe.filter_groups, list) else []
        )
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
    refs: Dict[str, list[str]] = {rule_id: [] for rule_id in target_rule_ids}

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
    if normalized_rule_id in BUILTIN_RULE_SET and normalized_rule_id != original_rule_id:
        raise ValueError(f"rule_id '{normalized_rule_id}' 与内置规则冲突，不能覆盖内置规则")

    for existing_rule in existing_rules:
        if existing_rule.id == normalized_rule_id and existing_rule.id != original_rule_id:
            raise ValueError(f"rule_id '{normalized_rule_id}' 已存在")
        if existing_rule.name == normalized_name and existing_rule.id != original_rule_id:
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
) -> tuple[FilterRuleGroup, dict[str, Any]]:
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
    key: SystemConfigKey,
    value: Any,
    publish_config_changed: RuleConfigPublisher,
) -> Optional[bool]:
    """通过统一入口保存配置并补发 ConfigChanged 事件。"""
    normalized_value = value
    if isinstance(normalized_value, list):
        normalized_value = [item for item in normalized_value if item is not None and item != ""]
        normalized_value = normalized_value or None

    success = await get_configured_system_config().async_set(key, normalized_value)
    if success:
        await publish_config_changed(key, normalized_value)
    return success


def replace_rule_id_in_rule_string(rule_string: str, old_rule_id: str, new_rule_id: str) -> str:
    """只替换完整 token，避免误伤其他规则名。"""
    pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(old_rule_id)}(?![A-Za-z0-9])")
    return pattern.sub(new_rule_id, rule_string)


class FilterRuleService:
    """提供过滤规则和规则组的查询与事务化修改用例。"""

    def __init__(
        self,
        subscriptions: SubscriptionRepository,
        mutation_scope: Callable[[], AbstractAsyncContextManager[AsyncRuleGroupMutationService]],
        publish_config_changed: RuleConfigPublisher,
    ) -> None:
        """注入订阅端口、规则组事务作用域和配置事件发布端口。"""
        self._subscriptions = subscriptions
        self._mutation_scope = mutation_scope
        self._publish_config_changed = publish_config_changed

    @staticmethod
    def query_builtin(rule_ids: Optional[list[str]] = None) -> dict[str, Any]:
        """查询内置过滤规则及规则串语法。"""
        rules = get_builtin_rules()
        if rule_ids:
            target_ids = set(rule_ids)
            rules = {key: value for key, value in rules.items() if key in target_ids}
        serialized = [serialize_builtin_rule(rule_id, payload) for rule_id, payload in rules.items()]
        return {
            "count": len(serialized),
            "rule_string_syntax": RULE_STRING_SYNTAX,
            "rules": serialized,
        }

    @staticmethod
    def query_custom(
        rule_ids: Optional[list[str]] = None,
        *,
        include_group_refs: bool = True,
    ) -> dict[str, Any]:
        """查询自定义过滤规则及规则组引用。"""
        rules = get_custom_rules()
        if rule_ids:
            target_ids = set(rule_ids)
            rules = [rule for rule in rules if rule.id in target_ids]
        refs = (
            collect_custom_rule_group_refs(
                get_rule_groups(),
                [str(rule.id) for rule in rules if rule.id],
            )
            if include_group_refs
            else {}
        )
        serialized = [
            serialize_custom_rule(rule, refs.get(str(rule_id)))
            for rule in rules
            if (rule_id := rule.id)
        ]
        return {"count": len(serialized), "rules": serialized}

    async def query_groups(
        self,
        group_names: Optional[list[str]] = None,
        *,
        include_usage: bool = True,
    ) -> dict[str, Any]:
        """查询规则组、解析层级和可选引用位置。"""
        groups = get_rule_groups()
        if group_names:
            target_names = set(group_names)
            groups = [group for group in groups if group.name in target_names]
        usage = (
            await collect_rule_group_usages(
                self._subscriptions,
                [str(group.name) for group in groups if group.name],
            )
            if include_usage
            else {}
        )
        serialized = [
            serialize_rule_group(group, usage.get(group.name or "")) for group in groups
        ]
        return {
            "count": len(serialized),
            "rule_string_syntax": RULE_STRING_SYNTAX,
            "rule_groups": serialized,
        }

    async def add_custom(self, **values: Any) -> dict[str, Any]:
        """校验并新增一条自定义过滤规则。"""
        rules = get_custom_rules()
        new_rule = normalize_custom_rule(existing_rules=rules, **values)
        rules.append(new_rule)
        await save_system_config(
            SystemConfigKey.CustomFilterRules,
            [rule.model_dump(exclude_none=True) for rule in rules],
            self._publish_config_changed,
        )
        return {
            "message": f"已新增自定义过滤规则 {new_rule.id}",
            "custom_rule": serialize_custom_rule(new_rule),
            "count": len(rules),
        }

    async def reorder_custom(
        self,
        rule_ids: list[str],
        *,
        expected_rule_ids: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """按完整 ID 列表重排规则，并用事务快照拒绝并发覆盖。"""
        rules = get_custom_rules()
        current_ids = [str(rule.id) for rule in rules if rule.id]
        if len(current_ids) != len(rules):
            raise ValueError("自定义规则存在缺少 ID 的损坏项，不能调整顺序")
        self._validate_reorder(
            "自定义规则",
            current_ids,
            rule_ids,
            expected_rule_ids,
        )
        rules_by_id = {str(rule.id): rule for rule in rules if rule.id}
        ordered_rules = [rules_by_id[rule_id] for rule_id in rule_ids]
        expected_rules = [rule.model_dump(exclude_none=True) for rule in rules]
        rule_definitions = [rule.model_dump(exclude_none=True) for rule in ordered_rules]
        groups = get_rule_groups()
        group_definitions = [group.model_dump(exclude_none=True) for group in groups]
        async with self._mutation_scope() as mutation:
            await mutation.apply(
                group_definitions,
                expected_rule_groups=group_definitions,
                custom_rules=rule_definitions,
                expected_custom_rules=expected_rules,
            )
        await self._publish_config_changed(SystemConfigKey.CustomFilterRules, rule_definitions)
        return {
            "message": "已调整自定义过滤规则顺序",
            "count": len(rule_ids),
            "rule_ids": rule_ids,
        }

    async def update_custom(
        self,
        *,
        current_rule_id: str,
        new_rule_id: Optional[str] = None,
        name: Optional[str] = None,
        include: Optional[str] = None,
        exclude: Optional[str] = None,
        size_range: Optional[str] = None,
        seeders: Optional[str] = None,
        publish_time: Optional[str] = None,
    ) -> dict[str, Any]:
        """更新自定义规则，并在改名时原子重写规则组引用。"""
        rules = get_custom_rules()
        current = next((rule for rule in rules if rule.id == current_rule_id), None)
        if current is None:
            raise ValueError(f"自定义过滤规则 '{current_rule_id}' 不存在")
        if current.id is None or current.name is None:
            raise ValueError("自定义过滤规则缺少 id 或 name")
        updated = normalize_custom_rule(
            rule_id=new_rule_id or current.id,
            name=name if name is not None else current.name,
            include=include if include is not None else current.include,
            exclude=exclude if exclude is not None else current.exclude,
            size_range=size_range if size_range is not None else current.size_range,
            seeders=seeders if seeders is not None else current.seeders,
            publish_time=(publish_time if publish_time is not None else current.publish_time),
            existing_rules=rules,
            original_rule_id=current.id,
        )
        expected_rules = [rule.model_dump(exclude_none=True) for rule in rules]
        final_rules = [updated if rule.id == current.id else rule for rule in rules]
        rule_definitions = [rule.model_dump(exclude_none=True) for rule in final_rules]
        groups = get_rule_groups()
        updated_groups = groups
        renamed_refs: list[str] = []
        if updated.id != current.id:
            updated_groups = []
            for group in groups:
                new_rule_string = replace_rule_id_in_rule_string(
                    group.rule_string or "",
                    current.id or "",
                    updated.id or "",
                )
                if new_rule_string != (group.rule_string or ""):
                    renamed_refs.append(group.name or "")
                    group = group.model_copy(update={"rule_string": new_rule_string})
                updated_groups.append(group)
            expected_groups = [group.model_dump(exclude_none=True) for group in groups]
            group_definitions = [group.model_dump(exclude_none=True) for group in updated_groups]
            async with self._mutation_scope() as mutation:
                await mutation.apply(
                    group_definitions,
                    expected_rule_groups=expected_groups,
                    custom_rules=rule_definitions,
                    expected_custom_rules=expected_rules,
                )
            await self._publish_config_changed(SystemConfigKey.CustomFilterRules, rule_definitions)
            await self._publish_config_changed(SystemConfigKey.UserFilterRuleGroups, group_definitions)
        else:
            await save_system_config(
                SystemConfigKey.CustomFilterRules,
                rule_definitions,
                self._publish_config_changed,
            )
        if updated.id is None:
            raise ValueError("更新后的自定义过滤规则缺少 id")
        refs = collect_custom_rule_group_refs(updated_groups, [updated.id]).get(updated.id, [])
        return {
            "message": f"已更新自定义过滤规则 {updated.id}",
            "custom_rule": serialize_custom_rule(updated, refs),
            "rule_groups_updated_for_rule_id_rename": renamed_refs,
        }

    async def delete_custom(self, rule_id: str) -> dict[str, Any]:
        """删除未被规则组引用的自定义过滤规则。"""
        rules = get_custom_rules()
        if not any(rule.id == rule_id for rule in rules):
            raise ValueError(f"自定义过滤规则 '{rule_id}' 不存在")
        refs = collect_custom_rule_group_refs(get_rule_groups(), [rule_id]).get(rule_id, [])
        if refs:
            raise ValueError(f"自定义过滤规则 '{rule_id}' 仍被规则组引用: {', '.join(refs)}")
        remaining = [rule for rule in rules if rule.id != rule_id]
        await save_system_config(
            SystemConfigKey.CustomFilterRules,
            [rule.model_dump(exclude_none=True) for rule in remaining],
            self._publish_config_changed,
        )
        return {
            "message": f"已删除自定义过滤规则 {rule_id}",
            "count": len(remaining),
        }

    async def add_group(
        self,
        *,
        name: str,
        rule_string: str,
        media_type: Optional[str] = None,
        category: Optional[str] = None,
    ) -> dict[str, Any]:
        """校验并新增规则组。"""
        groups = get_rule_groups()
        available_ids = set(get_builtin_rules()) | set(build_custom_rule_map())
        new_group, parsed = normalize_rule_group(
            name,
            rule_string,
            media_type,
            category,
            groups,
            available_ids,
        )
        if new_group.name is None:
            raise ValueError("新规则组缺少名称")
        expected = [group.model_dump(exclude_none=True) for group in groups]
        definitions = [*expected, new_group.model_dump(exclude_none=True)]
        async with self._mutation_scope() as mutation:
            await mutation.apply(definitions, expected_rule_groups=expected)
        await self._publish_config_changed(SystemConfigKey.UserFilterRuleGroups, definitions)
        usage = await collect_rule_group_usages(self._subscriptions, [new_group.name])
        return {
            "message": f"已新增规则组 {new_group.name}",
            "rule_group": serialize_rule_group(new_group, usage.get(new_group.name)),
            "parsed": parsed,
            "count": len(definitions),
        }

    async def reorder_groups(
        self,
        group_names: list[str],
        *,
        expected_group_names: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """按完整名称列表重排规则组，并复用原子修改作用域。"""
        groups = get_rule_groups()
        current_names = [str(group.name) for group in groups if group.name]
        if len(current_names) != len(groups):
            raise ValueError("规则组存在缺少名称的损坏项，不能调整顺序")
        self._validate_reorder(
            "规则组",
            current_names,
            group_names,
            expected_group_names,
        )
        groups_by_name = {str(group.name): group for group in groups if group.name}
        ordered_groups = [groups_by_name[name] for name in group_names]
        expected = [group.model_dump(exclude_none=True) for group in groups]
        definitions = [group.model_dump(exclude_none=True) for group in ordered_groups]
        async with self._mutation_scope() as mutation:
            await mutation.apply(definitions, expected_rule_groups=expected)
        await self._publish_config_changed(SystemConfigKey.UserFilterRuleGroups, definitions)
        return {
            "message": "已调整过滤规则组顺序",
            "count": len(group_names),
            "group_names": group_names,
        }

    async def update_group(
        self,
        *,
        current_name: str,
        new_name: Optional[str] = None,
        rule_string: Optional[str] = None,
        media_type: Optional[str] = None,
        category: Optional[str] = None,
    ) -> dict[str, Any]:
        """更新规则组并原子重写全局配置和订阅引用。"""
        groups = get_rule_groups()
        current = next((group for group in groups if group.name == current_name), None)
        if current is None:
            raise ValueError(f"规则组 '{current_name}' 不存在")
        if current.name is None:
            raise ValueError("当前规则组缺少名称")
        available_ids = set(get_builtin_rules()) | set(build_custom_rule_map())
        updated, parsed = normalize_rule_group(
            new_name or current.name or "",
            rule_string if rule_string is not None else current.rule_string or "",
            media_type if media_type is not None else current.media_type,
            category if category is not None else current.category,
            groups,
            available_ids,
            original_name=current.name,
        )
        expected = [group.model_dump(exclude_none=True) for group in groups]
        final_groups = [updated if group.name == current.name else group for group in groups]
        definitions = [group.model_dump(exclude_none=True) for group in final_groups]
        async with self._mutation_scope() as mutation:
            result = await mutation.apply(
                definitions,
                expected_rule_groups=expected,
                previous_name=current.name,
                current_name=updated.name,
            )
        updated_name = updated.name
        if updated_name is None:
            raise ValueError("更新后的规则组缺少名称")
        await self._publish_config_changed(SystemConfigKey.UserFilterRuleGroups, definitions)
        usage = await collect_rule_group_usages(self._subscriptions, [updated_name])
        return {
            "message": f"已更新规则组 {updated_name}",
            "rule_group": serialize_rule_group(updated, usage.get(updated_name)),
            "parsed": parsed,
            "reference_updates": result.to_dict(),
        }

    async def delete_group(self, name: str) -> dict[str, Any]:
        """删除规则组并原子清理全部引用。"""
        groups = get_rule_groups()
        if not any(group.name == name for group in groups):
            raise ValueError(f"规则组 '{name}' 不存在")
        expected = [group.model_dump(exclude_none=True) for group in groups]
        remaining = [group for group in groups if group.name != name]
        definitions = [group.model_dump(exclude_none=True) for group in remaining]
        async with self._mutation_scope() as mutation:
            result = await mutation.apply(
                definitions,
                expected_rule_groups=expected,
                previous_name=name,
            )
        await self._publish_config_changed(SystemConfigKey.UserFilterRuleGroups, definitions)
        return {
            "message": f"已删除规则组 {name}",
            "count": len(remaining),
            "reference_updates": result.to_dict(),
        }

    @staticmethod
    def _validate_reorder(
        item_label: str,
        current_names: list[str],
        requested_names: list[str],
        expected_names: Optional[list[str]],
    ) -> None:
        """验证重排列表完整、唯一且基于未过期的名称集合。"""
        if any(not name.strip() for name in requested_names):
            raise ValueError(f"{item_label}顺序不能包含空名称")
        if len(set(requested_names)) != len(requested_names):
            raise ValueError(f"{item_label}顺序不能包含重复名称")
        if set(requested_names) != set(current_names):
            raise ValueError(f"{item_label}集合已变化，请重新读取后再试")
        if (
            expected_names is not None
            and current_names != expected_names
            and current_names != requested_names
        ):
            raise ValueError(f"{item_label}顺序已被其他请求修改，请重新读取后再试")
