"""
规则域：用户规则组配置访问、内置规则定义与规则解析器，
过滤模块与 Agent 工具共享同一事实来源。
"""

import copy
import threading
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol

from pyparsing import (
    Combine,
    Forward,
    Literal,
    ParseResults,
    Word,
    alphanums,
    alphas,
    infix_notation,
    nums,
    opAssoc,
)

from app.adapters.system import rust as rust_accel
from app.application.configuration import (
    SystemConfigStagingPort,
    get_configured_system_config,
)
from app.application.outbox import SyncUnitOfWork
from app.application.subscription.contract import (
    SubscriptionPatch,
    SubscriptionReferenceStagingPort,
    SubscriptionSnapshot,
)
from app.domain.context import MediaInfo
from app.schemas.common import JsonData
from app.schemas.rule import CustomRule
from app.schemas.system import FilterRuleGroup
from app.schemas.types import SystemConfigKey

_RULE_GROUP_LIST_CONFIG_KEYS = (
    SystemConfigKey.SearchFilterRuleGroups,
    SystemConfigKey.SubscribeFilterRuleGroups,
    SystemConfigKey.BestVersionFilterRuleGroups,
)
_RULE_GROUP_DEFAULT_CONFIG_KEYS = (
    SystemConfigKey.DefaultMovieSubscribeConfig,
    SystemConfigKey.DefaultTvSubscribeConfig,
    SystemConfigKey.DefaultMusicSubscribeConfig,
)


class AsyncRuleGroupUnitOfWork(Protocol):
    """异步规则组修改事务的最小提交与回滚端口。"""

    async def commit(self) -> None:
        """提交规则定义及全部引用更新。"""

    async def rollback(self) -> None:
        """回滚规则定义及全部引用更新。"""


class RuleGroupMutationConflictError(RuntimeError):
    """规则定义已被并发修改，拒绝用过期快照覆盖新值。"""


@dataclass(frozen=True, slots=True)
class RuleGroupSubscriptionChange:
    """一条已提交的订阅规则组引用变更。"""

    subscribe_id: int
    name: str
    season: Optional[int]
    filter_groups: tuple[str, ...]

    def to_dict(self) -> dict[str, JsonData]:
        """返回 Agent 响应可直接序列化的兼容字典。"""
        return {
            "subscribe_id": self.subscribe_id,
            "name": self.name,
            "season": self.season,
            "filter_groups": list(self.filter_groups),
        }


@dataclass(frozen=True, slots=True)
class RuleGroupMutation:
    """规则定义和全部引用在同一事务提交后的稳定结果。"""

    configurations: Mapping[SystemConfigKey, JsonData]
    subscriptions: tuple[RuleGroupSubscriptionChange, ...]

    def to_dict(self) -> dict[str, JsonData]:
        """返回现有 Agent 工具使用的引用更新响应结构。"""
        return {
            "global_settings": {
                key.value: copy.deepcopy(value)
                for key, value in self.configurations.items()
                if key != SystemConfigKey.UserFilterRuleGroups
            },
            "subscribes": [change.to_dict() for change in self.subscriptions],
        }


def _map_rule_group_names(
    values: object,
    previous_name: Optional[str],
    current_name: Optional[str],
    valid_names: set[str],
) -> list[str]:
    """按删除或改名意图映射引用，并按最终定义清理悬空名称。"""
    result: list[str] = []
    for raw_name in values if isinstance(values, list) else []:
        name = str(raw_name)
        if previous_name is not None and name == previous_name:
            if current_name is None:
                continue
            name = current_name
        if name in valid_names and name not in result:
            result.append(name)
    return result


def _rewrite_default_config(
    value: object,
    previous_name: Optional[str],
    current_name: Optional[str],
    valid_names: set[str],
) -> dict[str, JsonData]:
    """复制默认订阅配置并只重写其中的规则组引用。"""
    original = copy.deepcopy(value) if isinstance(value, dict) else {}
    original_groups = original.get("filter_groups")
    updated_groups = _map_rule_group_names(
        original_groups, previous_name, current_name, valid_names
    )
    if updated_groups == (original_groups or []):
        return original
    original["filter_groups"] = updated_groups
    return original


def _subscription_change(
    subscription: SubscriptionSnapshot,
    filter_groups: list[str],
) -> RuleGroupSubscriptionChange:
    """把事务内订阅快照投影为提交结果。"""
    return RuleGroupSubscriptionChange(
        subscribe_id=subscription.id,
        name=subscription.name,
        season=subscription.season,
        filter_groups=tuple(filter_groups),
    )


class SyncRuleGroupMutationService:
    """在一个同步 UoW 内原子提交规则定义、配置引用和订阅引用。"""

    def __init__(
        self,
        configuration: SystemConfigStagingPort,
        subscriptions: SubscriptionReferenceStagingPort,
        unit_of_work: SyncUnitOfWork,
        publish: Callable[[Mapping[SystemConfigKey, JsonData]], None],
    ) -> None:
        """注入共享 Session 的端口、UoW 和必填提交后快照发布器。"""
        self._configuration = configuration
        self._subscriptions = subscriptions
        self._unit_of_work = unit_of_work
        self._publish = publish

    def apply(
        self,
        rule_groups: list[dict[str, JsonData]],
        *,
        expected_rule_groups: list[dict[str, JsonData]],
        custom_rules: Optional[list[dict[str, JsonData]]] = None,
        expected_custom_rules: Optional[list[dict[str, JsonData]]] = None,
        previous_name: Optional[str] = None,
        current_name: Optional[str] = None,
    ) -> RuleGroupMutation:
        """原子保存规则组及可选自定义规则，并重写全部规则组引用。"""
        if (custom_rules is None) != (expected_custom_rules is None):
            raise ValueError("自定义规则写入必须同时提供当前快照和目标值")
        try:
            result = self._stage(
                rule_groups,
                expected_rule_groups,
                custom_rules,
                expected_custom_rules,
                previous_name,
                current_name,
            )
            self._unit_of_work.commit()
        except Exception:
            self._unit_of_work.rollback()
            raise
        self._publish(copy.deepcopy(dict(result.configurations)))
        return result

    def _stage(
        self,
        rule_groups: list[dict[str, JsonData]],
        expected_rule_groups: list[dict[str, JsonData]],
        custom_rules: Optional[list[dict[str, JsonData]]],
        expected_custom_rules: Optional[list[dict[str, JsonData]]],
        previous_name: Optional[str],
        current_name: Optional[str],
    ) -> RuleGroupMutation:
        """在同步 Session 中锁定事实源并暂存全部变化。"""
        changes: dict[SystemConfigKey, JsonData] = {}
        definitions = copy.deepcopy(rule_groups)
        valid_names = {
            str(group["name"])
            for group in definitions
            if isinstance(group.get("name"), str) and group["name"]
        }
        original_definitions = self._configuration.get_for_update(
            SystemConfigKey.UserFilterRuleGroups
        )
        if (
            (original_definitions or []) != expected_rule_groups
            and original_definitions != definitions
        ):
            raise RuleGroupMutationConflictError("规则组已被其他请求修改，请重新读取后再试")
        if original_definitions != definitions:
            self._configuration.stage_set(
                SystemConfigKey.UserFilterRuleGroups, definitions
            )
            changes[SystemConfigKey.UserFilterRuleGroups] = definitions

        if custom_rules is not None:
            assert expected_custom_rules is not None
            custom_definitions = copy.deepcopy(custom_rules)
            original_custom_rules = self._configuration.get_for_update(
                SystemConfigKey.CustomFilterRules
            )
            if (
                (original_custom_rules or []) != expected_custom_rules
                and original_custom_rules != custom_definitions
            ):
                raise RuleGroupMutationConflictError(
                    "自定义规则已被其他请求修改，请重新读取后再试"
                )
            if original_custom_rules != custom_definitions:
                self._configuration.stage_set(
                    SystemConfigKey.CustomFilterRules,
                    custom_definitions,
                )
                changes[SystemConfigKey.CustomFilterRules] = custom_definitions

        for key in _RULE_GROUP_LIST_CONFIG_KEYS:
            original = self._configuration.get_for_update(key)
            list_updated = _map_rule_group_names(
                original, previous_name, current_name, valid_names
            )
            if list_updated != (original or []):
                self._configuration.stage_set(key, list_updated)
                changes[key] = list_updated

        for key in _RULE_GROUP_DEFAULT_CONFIG_KEYS:
            original = self._configuration.get_for_update(key)
            default_updated = _rewrite_default_config(
                original, previous_name, current_name, valid_names
            )
            if default_updated != (original or {}):
                self._configuration.stage_set(key, default_updated)
                changes[key] = default_updated

        subscription_changes = []
        for subscription in self._subscriptions.list_for_reference_rewrite():
            original = list(subscription.filter_groups or [])
            updated = _map_rule_group_names(
                original, previous_name, current_name, valid_names
            )
            if updated == original:
                continue
            self._subscriptions.stage_update(
                subscription.id,
                SubscriptionPatch({"filter_groups": updated}),
            )
            subscription_changes.append(_subscription_change(subscription, updated))
        return RuleGroupMutation(changes, tuple(subscription_changes))


class AsyncRuleGroupMutationService:
    """在一个异步 UoW 内原子提交规则定义、配置引用和订阅引用。"""

    def __init__(
        self,
        configuration: SystemConfigStagingPort,
        subscriptions: SubscriptionReferenceStagingPort,
        unit_of_work: AsyncRuleGroupUnitOfWork,
        publish: Callable[[Mapping[SystemConfigKey, JsonData]], Awaitable[None]],
    ) -> None:
        """注入共享 AsyncSession 的端口、UoW 和必填提交后快照发布器。"""
        self._configuration = configuration
        self._subscriptions = subscriptions
        self._unit_of_work = unit_of_work
        self._publish = publish

    async def apply(
        self,
        rule_groups: list[dict[str, JsonData]],
        *,
        expected_rule_groups: list[dict[str, JsonData]],
        custom_rules: Optional[list[dict[str, JsonData]]] = None,
        expected_custom_rules: Optional[list[dict[str, JsonData]]] = None,
        previous_name: Optional[str] = None,
        current_name: Optional[str] = None,
    ) -> RuleGroupMutation:
        """异步原子保存规则组及可选自定义规则，并重写全部引用。"""
        if (custom_rules is None) != (expected_custom_rules is None):
            raise ValueError("自定义规则写入必须同时提供当前快照和目标值")
        try:
            result = await self._stage(
                rule_groups,
                expected_rule_groups,
                custom_rules,
                expected_custom_rules,
                previous_name,
                current_name,
            )
            await self._unit_of_work.commit()
        except Exception:
            await self._unit_of_work.rollback()
            raise
        await self._publish(copy.deepcopy(dict(result.configurations)))
        return result

    async def _stage(
        self,
        rule_groups: list[dict[str, JsonData]],
        expected_rule_groups: list[dict[str, JsonData]],
        custom_rules: Optional[list[dict[str, JsonData]]],
        expected_custom_rules: Optional[list[dict[str, JsonData]]],
        previous_name: Optional[str],
        current_name: Optional[str],
    ) -> RuleGroupMutation:
        """在 AsyncSession 中锁定事实源并暂存全部变化。"""
        changes: dict[SystemConfigKey, JsonData] = {}
        definitions = copy.deepcopy(rule_groups)
        valid_names = {
            str(group["name"])
            for group in definitions
            if isinstance(group.get("name"), str) and group["name"]
        }
        original_definitions = await self._configuration.async_get_for_update(
            SystemConfigKey.UserFilterRuleGroups
        )
        if (
            (original_definitions or []) != expected_rule_groups
            and original_definitions != definitions
        ):
            raise RuleGroupMutationConflictError("规则组已被其他请求修改，请重新读取后再试")
        if original_definitions != definitions:
            await self._configuration.async_stage_set(
                SystemConfigKey.UserFilterRuleGroups, definitions
            )
            changes[SystemConfigKey.UserFilterRuleGroups] = definitions

        if custom_rules is not None:
            assert expected_custom_rules is not None
            custom_definitions = copy.deepcopy(custom_rules)
            original_custom_rules = (
                await self._configuration.async_get_for_update(
                    SystemConfigKey.CustomFilterRules
                )
            )
            if (
                (original_custom_rules or []) != expected_custom_rules
                and original_custom_rules != custom_definitions
            ):
                raise RuleGroupMutationConflictError(
                    "自定义规则已被其他请求修改，请重新读取后再试"
                )
            if original_custom_rules != custom_definitions:
                await self._configuration.async_stage_set(
                    SystemConfigKey.CustomFilterRules,
                    custom_definitions,
                )
                changes[SystemConfigKey.CustomFilterRules] = custom_definitions

        for key in _RULE_GROUP_LIST_CONFIG_KEYS:
            original = await self._configuration.async_get_for_update(key)
            list_updated = _map_rule_group_names(
                original, previous_name, current_name, valid_names
            )
            if list_updated != (original or []):
                await self._configuration.async_stage_set(key, list_updated)
                changes[key] = list_updated

        for key in _RULE_GROUP_DEFAULT_CONFIG_KEYS:
            original = await self._configuration.async_get_for_update(key)
            default_updated = _rewrite_default_config(
                original, previous_name, current_name, valid_names
            )
            if default_updated != (original or {}):
                await self._configuration.async_stage_set(key, default_updated)
                changes[key] = default_updated

        subscription_changes = []
        subscriptions = await self._subscriptions.async_list_for_reference_rewrite()
        for subscription in subscriptions:
            original = list(subscription.filter_groups or [])
            updated = _map_rule_group_names(
                original, previous_name, current_name, valid_names
            )
            if updated == original:
                continue
            await self._subscriptions.async_stage_update(
                subscription.id,
                SubscriptionPatch({"filter_groups": updated}),
            )
            subscription_changes.append(_subscription_change(subscription, updated))
        return RuleGroupMutation(changes, tuple(subscription_changes))


class RuleHelper:
    """读取用户过滤规则配置，并按媒体上下文选择适用规则组。"""

    @staticmethod
    def get_rule_groups() -> List[FilterRuleGroup]:
        """返回用户配置的全部过滤规则组。"""
        rule_groups: List[dict[str, Any]] = get_configured_system_config().get(
            SystemConfigKey.UserFilterRuleGroups
        )
        if not rule_groups:
            return []
        return [FilterRuleGroup(**group) for group in rule_groups]

    def get_rule_group(self, group_name: str) -> Optional[FilterRuleGroup]:
        """按名称返回过滤规则组。"""
        return next(
            (group for group in self.get_rule_groups() if group.name == group_name),
            None,
        )

    def get_rule_group_by_media(
        self,
        media: Optional[MediaInfo] = None,
        group_names: Optional[list[str]] = None,
    ) -> List[FilterRuleGroup]:
        """按媒体类型、分类和候选名称筛选适用规则组。"""
        rule_groups = self.get_rule_groups()
        if group_names:
            rule_groups = [
                group for group in rule_groups if group.name in group_names
            ]
        return [
            group
            for group in rule_groups
            if not group.media_type
            or (
                media
                and group.media_type == media.type.value
                and (not group.category or group.category == media.category)
            )
        ]

    @staticmethod
    def get_custom_rules() -> List[CustomRule]:
        """返回用户配置的全部自定义过滤规则。"""
        rules: List[dict[str, Any]] = get_configured_system_config().get(
            SystemConfigKey.CustomFilterRules
        )
        if not rules:
            return []
        return [CustomRule(**rule) for rule in rules]

    def get_custom_rule(self, rule_id: str) -> Optional[CustomRule]:
        """按 ID 返回一条自定义过滤规则。"""
        return next(
            (rule for rule in self.get_custom_rules() if rule.id == rule_id),
            None,
        )


def replace_group_name_in_list(
    values: Optional[Iterable[str]],
    old_name: str,
    new_name: str,
) -> list[str]:
    """更新配置里的规则组名引用，并顺手去重。"""
    result = []
    for value in values or []:
        mapped = new_name if value == old_name else value
        if mapped not in result:
            result.append(mapped)
    return result


# 内置规则只在这里维护一份，便于过滤模块和 Agent 工具共享同一套事实来源。
BUILTIN_RULE_SET: Dict[str, dict[str, Any]] = {
    # 蓝光原盘
    "BLU": {
        "include": [
            r"(?i)(\bBlu-?Ray\b.*\b(?:VC-?1|AVC|MPEG-?2)\b|\b(?:UHD|4K|2160p)\b(?:.*Blu-?Ray)?.*\b(?:HEVC|H\.?265)\b|\bBlu-?Ray\b.*\b(?:UHD|4K|2160p)\b.*\b(?:HEVC|H\.?265)\b|\b(?:COMPLETE|FULL)\b.*\b(?:(?:UHD|4K|2160p)\b.*)?Blu-?Ray\b|\b(BD25|BD50|BD66|BD100|BDMV|MiniBD)\b)"
        ],
        "exclude": [
            r"(?i)(\b[XH]\.?264\b|\b[XH]\.?265\b|\bWEB-?DL\b|\bWEB-?RIP\b|\bHDTV(?:RIP)?\b|\bREMUX\b|\bBDRip\b|\bBRRip\b|\bHDRip\b|\bENCODE\b|\b(?<!WEB-|HDTV)RIP\b)"
        ],
    },
    # 4K
    "4K": {
        "include": [r"4k|2160p|x2160"],
        "exclude": [],
    },
    # 1080P
    "1080P": {
        "include": [r"1080[pi]|x1080"],
        "exclude": [],
    },
    # 720P
    "720P": {
        "include": [r"720[pi]|x720"],
        "exclude": [],
    },
    # 中字
    "CNSUB": {
        "include": [
            r"[中国國繁简](/|\s|\\|\|)?[繁简英粤]|[英简繁](/|\s|\\|\|)?[中繁简]"
            r"|繁體|简体|[中国國][字配]|国语|國語|中文|中字|简日|繁日|简繁|繁体"
            r"|([\s,.-\[])(chs|cht)(|[\s,.-\]])"
            r"|(?<![a-z0-9])(?<!\d\s)(gb|big5)(?![a-z0-9])"
        ],
        "exclude": [],
        "tmdb": {
            "original_language": "zh,cn",
        },
    },
    # 官种
    "GZ": {
        "include": [r"官方", r"官种", r"官组"],
        "match": ["labels"],
    },
    # 特效字幕
    "SPECSUB": {
        "include": [r"特效"],
        "exclude": [],
    },
    # BluRay
    "BLURAY": {
        "include": [r"Blu-?Ray"],
        "exclude": [],
    },
    # UHD
    "UHD": {
        "include": [r"UHD|UltraHD"],
        "exclude": [],
    },
    # H265
    "H265": {
        "include": [r"[Hx].?265|HEVC"],
        "exclude": [],
    },
    # H264
    "H264": {
        "include": [r"[Hx].?264|AVC"],
        "exclude": [],
    },
    # 杜比视界
    "DOLBY": {
        "include": [r"Dolby[\s.]+Vision|DOVI|[\s.]+DV[\s.]+|杜比视界"],
        "exclude": [],
    },
    # 杜比全景声
    "ATMOS": {
        "include": [r"Dolby[\s.+]+Atmos|Atmos|杜比全景[声聲]"],
        "exclude": [],
    },
    # HDR
    "HDR": {
        "include": [r"[\s.]+HDR[\s.]+|HDR10|HDR10\+|HDRVivid"],
        "exclude": [],
    },
    # SDR
    "SDR": {
        "include": [r"[\s.]+SDR[\s.]+"],
        "exclude": [],
    },
    # 重编码
    "REMUX": {
        "include": [r"REMUX"],
        "exclude": [],
    },
    # WEB-DL
    "WEBDL": {
        "include": [r"WEB-?DL|WEB-?RIP"],
        "exclude": [],
    },
    # 免费
    "FREE": {
        "downloadvolumefactor": 0,
    },
    # 国语配音
    "CNVOI": {
        "include": [r"[国國][语語]配音|[国國]配|[国國][语語]"],
        "exclude": [],
        "tmdb": {
            "original_language": "zh",
        },
    },
    # 粤语配音
    "HKVOI": {
        "include": [r"粤语配音|粤语"],
        "exclude": [],
    },
    # 60FPS
    "60FPS": {
        "include": [r"60fps|60帧"],
        "exclude": [],
    },
    # 3D
    "3D": {
        "include": [r"3D"],
        "exclude": [],
    },
    # Hi-Res 无损音频
    "HIRES": {
        "include": [r"(?i)\b(?:Hi[ ._-]?Res(?:olution)?|DSD(?:64|128|256|512)?)\b|高解析|(?:24|32)\s*(?:-?bit|位)"],
        "exclude": [],
    },
    # 无损音频
    "LOSSLESS": {
        "include": [r"(?i)\b(?:Lossless|FLAC|ALAC|APE|WAV|WAVE|AIFF?|PCM|DSD|DSF|DFF)\b|无损"],
        "exclude": [],
    },
    "FLAC": {"include": [r"(?i)(?<![A-Z0-9])FLAC(?![A-Z0-9])"], "exclude": []},
    "ALAC": {"include": [r"(?i)(?<![A-Z0-9])ALAC(?![A-Z0-9])"], "exclude": []},
    "APE": {"include": [r"(?i)(?<![A-Z0-9])APE(?![A-Z0-9])"], "exclude": []},
    "WAV": {"include": [r"(?i)(?<![A-Z0-9])WAV(?:E)?(?![A-Z0-9])"], "exclude": []},
    "DSD": {"include": [r"(?i)(?<![A-Z0-9])(?:DSD(?:64|128|256|512)?|DSF|DFF)(?![A-Z0-9])"], "exclude": []},
    "MP3": {"include": [r"(?i)(?<![A-Z0-9])MP3(?![A-Z0-9])"], "exclude": []},
    "AAC": {"include": [r"(?i)(?<![A-Z0-9])(?:AAC|M4A)(?![A-Z0-9])"], "exclude": []},
    "OPUS": {"include": [r"(?i)(?<![A-Z0-9])OPUS(?![A-Z0-9])"], "exclude": []},
    "BITRATE320": {"include": [r"(?i)(?<!\d)320\s*k(?:bps?|b(?:it)?/?s?)?(?![a-z])"], "exclude": []},
    "BITRATE256": {"include": [r"(?i)(?<!\d)256\s*k(?:bps?|b(?:it)?/?s?)?(?![a-z])"], "exclude": []},
    "BITRATE192": {"include": [r"(?i)(?<!\d)192\s*k(?:bps?|b(?:it)?/?s?)?(?![a-z])"], "exclude": []},
}



class RuleParser:

    _lock = threading.Lock()
    _thread_local = threading.local()

    def __init__(self) -> None:
        """
        定义语法规则
        """
        with self._lock:
            if not hasattr(self._thread_local, 'initialized'):
                # 表达式
                expr: Forward = Forward()
                # 原子
                atom: Combine = Combine(Word(alphas, alphanums) | (Word(nums) + Word(alphas, alphanums)))
                # 逻辑非操作符
                operator_not: Literal = Literal('!').set_parse_action(lambda t: 'not')
                # 逻辑或操作符
                operator_or: Literal = Literal('|').set_parse_action(lambda t: 'or')
                # 逻辑与操作符
                operator_and: Literal = Literal('&').set_parse_action(lambda t: 'and')
                # 定义表达式的语法规则
                expr <<= (operator_not + expr) | atom | ('(' + expr + ')')

                # 运算符优先级
                self.expr = infix_notation(expr,
                                          [(operator_not, 1, opAssoc.RIGHT),
                                           (operator_and, 2, opAssoc.LEFT),
                                           (operator_or, 2, opAssoc.LEFT)])

                self._thread_local.expr = self.expr
                self._thread_local.initialized = True
            else:
                self.expr = self._thread_local.expr

    def parse(self, expression: str) -> ParseResults:
        """
        解析给定的表达式。

        参数:
        expression -- 要解析的表达式

        返回:
        解析结果
        """
        rust_result = rust_accel.parse_filter_rule(expression)
        if rust_result is not None:
            return _RustParseResults(rust_result)
        return self.expr.parse_string(expression)


class _RustParseResults(list[Any]):
    """
    包装 Rust 解析结果，提供本模块调用方使用的 as_list/asList 接口。
    """

    def as_list(self) -> list[Any]:
        """
        返回兼容 pyparsing.ParseResults.as_list 的列表结构。
        """
        return list(self)

    def asList(self) -> list[Any]:  # noqa: N802
        """
        返回兼容 pyparsing.ParseResults.asList 的列表结构。
        """
        return self.as_list()


if __name__ == '__main__':
    # 测试代码
    expression_str = """
     SPECSUB & CNVOI & 4K & !BLU & !REMUX & !WEBDL & 60FPS & !DOLBY & !SDR & !3D > CNSUB & CNVOI & 4K & !BLU & !REMUX & !WEBDL & 60FPS & !DOLBY & !SDR & !3D > SPECSUB & 4K & !BLU & !REMUX & !WEBDL & 60FPS & !DOLBY & !SDR & !3D > CNSUB & 4K & !BLU & !REMUX & !WEBDL & 60FPS & !DOLBY & !SDR & !3D > SPECSUB & CNVOI & 4K & !BLU & !REMUX & !WEBDL & !DOLBY & HDR & !3D > CNSUB & CNVOI & 4K & !BLU & !REMUX & !WEBDL & !DOLBY & HDR & !3D > SPECSUB & CNVOI & 4K & !BLU & !REMUX & !WEBDL & !DOLBY & !3D > CNSUB & CNVOI & 4K & !BLU & !REMUX & !WEBDL & !DOLBY & !3D > SPECSUB & 4K & !BLU & !REMUX & !WEBDL & !DOLBY & HDR & !3D > CNSUB & 4K & !BLU & !REMUX & !WEBDL & !DOLBY & HDR & !3D > SPECSUB & 4K & !BLU & !REMUX & !WEBDL & !DOLBY & !3D > CNSUB & 4K & !BLU & !REMUX & !WEBDL & !DOLBY & !3D > SPECSUB & CNVOI & 4K & WEBDL & 60FPS & !DOLBY & !SDR & !3D > CNSUB & CNVOI & 4K & WEBDL & 60FPS & !DOLBY & !SDR & !3D > SPECSUB & 4K & WEBDL & 60FPS & !DOLBY & !SDR & !3D > CNSUB & 4K & WEBDL & 60FPS & !DOLBY & !SDR & !3D > SPECSUB & CNVOI & 4K & WEBDL & !DOLBY & HDR & !3D > CNSUB & CNVOI & 4K & WEBDL & !DOLBY & HDR & !3D > SPECSUB & CNVOI & 4K & WEBDL & !DOLBY & !3D > CNSUB & CNVOI & 4K & WEBDL & !DOLBY & !3D > SPECSUB & 4K & WEBDL & !DOLBY & HDR & !3D > CNSUB & 4K & WEBDL & !DOLBY & HDR & !3D > SPECSUB & 4K & WEBDL & !DOLBY & !3D > CNSUB & 4K & WEBDL & !DOLBY & !3D > SPECSUB & CNVOI & 4K & !BLU & !WEBDL & !DOLBY & HDR & !3D > CNSUB & CNVOI & 4K & !BLU & !WEBDL & !DOLBY & HDR & !3D > SPECSUB & CNVOI & 4K & !BLU & !WEBDL & !DOLBY & !3D > CNSUB & CNVOI & 4K & !BLU & !WEBDL & !DOLBY & !3D > SPECSUB & 4K & !BLU & !WEBDL & !DOLBY & HDR & !3D > CNSUB & 4K & !BLU & !WEBDL & !DOLBY & HDR & !3D > SPECSUB & 4K & !BLU & !WEBDL & !DOLBY & !SDR & !3D > CNSUB & 4K & !BLU & !WEBDL & !DOLBY & !SDR & !3D > 4K & !BLU & !REMUX & !DOLBY & HDR & !3D > 4K & !BLURAY & !REMUX & !DOLBY & !3D > SPECSUB & 1080P & !BLU & !REMUX & !WEBDL & !DOLBY & HDR & !3D > CNSUB & 1080P & !BLU & !REMUX & !WEBDL & !DOLBY & HDR & !3D > SPECSUB & 1080P & !BLU & !REMUX & !WEBDL & !DOLBY & !3D > CNSUB & 1080P & !BLU & !REMUX & !WEBDL & !DOLBY & !3D > SPECSUB & 1080P & !BLU & !WEBDL & !DOLBY & HDR & !3D > CNSUB & 1080P & !BLU & !WEBDL & !DOLBY & HDR & !3D > SPECSUB & 1080P & !BLU & !WEBDL & !DOLBY & !3D > CNSUB & 1080P & !BLU & !WEBDL & !DOLBY & !3D > SPECSUB & 1080P & WEBDL & !DOLBY & HDR & !3D > CNSUB & 1080P & WEBDL & !DOLBY & HDR & !3D > SPECSUB & 1080P & WEBDL & !DOLBY & !3D > CNSUB & 1080P & WEBDL & !DOLBY & !3D > 1080P & !BLU & !REMUX & !DOLBY & HDR & !3D > 1080P & !BLU & !REMUX & !DOLBY & !3D
    """
    for exp in expression_str.split('>'):
        parsed_expr = RuleParser().parse(exp.strip())
        print(parsed_expr.asList())
