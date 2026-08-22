"""
规则域：用户规则组配置访问，内置规则定义与规则解析器经 app.domain.filterrule 再导出。
"""

from typing import Any, Dict, List, Optional, Tuple

from app.application.configuration import get_configured_system_config
from app.domain.context import MediaInfo
from app.domain.filterrule import BUILTIN_RULE_SET, RuleParser  # noqa: F401
from app.runtime.extensions.registry.filter_rule import (
    RULE_GROUP_KIND,
    RULE_KIND,
    FilterRuleClaim,
    plugin_filter_rule_registry,
)
from app.runtime.extensions.contract.instance import split_instance_key
from app.schemas.rule import (
    CustomRule,
    FilterRuleConflict,
    FilterRuleLayer,
    FilterRuleOrigin,
)
from app.schemas.system import FilterRuleGroup
from app.schemas.types import SystemConfigKey

# 规则来源层标识，次序即合并次序，靠后的压住靠前的
BUILTIN_LAYER = "builtin"
PLUGIN_LAYER = "plugin"
USER_LAYER = "user"


class RuleHelper:
    """读取过滤规则配置，并按媒体上下文选择适用规则组。"""

    @staticmethod
    def get_rule_groups() -> List[FilterRuleGroup]:
        """返回当前可用的全部过滤规则组，插件提供的排在用户配置之前。

        同名时以用户配置为准：用户手改过的规则组不能被装了个插件之后悄悄改掉。
        插件规则组因此与插件规则同一套优先级，四个使用场景按组名引用时不必区分
        一个组来自插件还是用户配置。
        """
        groups: dict[str, FilterRuleGroup] = {
            name: FilterRuleGroup(**definition)
            for name, definition in plugin_filter_rule_registry.rule_group_definitions().items()
        }
        user_groups: List[dict] = get_configured_system_config().get(
            SystemConfigKey.UserFilterRuleGroups
        )
        for group in user_groups or []:
            model = FilterRuleGroup(**group)
            groups[model.name] = model
        return list(groups.values())

    def get_rule_group(self, group_name: str) -> Optional[FilterRuleGroup]:
        """按名称返回过滤规则组。"""
        return next(
            (group for group in self.get_rule_groups() if group.name == group_name),
            None,
        )

    def get_rule_group_by_media(
        self,
        media: Optional[MediaInfo] = None,
        group_names: Optional[list] = None,
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
                and (
                    (not group.category and group.media_type == media.type.value)
                    or group.category == media.category
                )
            )
        ]

    @staticmethod
    def get_custom_rules() -> List[CustomRule]:
        """返回用户配置的全部自定义过滤规则。"""
        rules: List[dict] = get_configured_system_config().get(SystemConfigKey.CustomFilterRules)
        if not rules:
            return []
        return [CustomRule(**rule) for rule in rules]

    def get_custom_rule(self, rule_id: str) -> Optional[CustomRule]:
        """按 ID 返回一条自定义过滤规则。"""
        return next(
            (rule for rule in self.get_custom_rules() if rule.id == rule_id),
            None,
        )


class FilterRuleOriginService:
    """按内置 < 插件 < 用户三层交出筛选规则与规则组的来源。

    规则集是三层合并出来的一张平表，合并完就看不出哪条来自哪里；插件带来的规则因此
    在设置页上无从辨认，跨插件冲突失效的标识更是只在日志里留过一次告警。本服务把合并
    前的分层原样交出，用户据此知道一条规则归谁管、以及一个标识为什么不生效。
    """

    def __init__(self, registry: Any = None, rule_helper: Any = None) -> None:
        """
        绑定插件规则注册表与用户规则读取器

        :param registry: 插件筛选规则注册表，为空时取宿主全局注册表
        :param rule_helper: 用户规则读取器，为空时取默认实现
        """
        self._registry = registry or plugin_filter_rule_registry
        self._rules = rule_helper or RuleHelper()

    def list_rule_origins(self) -> List[FilterRuleOrigin]:
        """
        列出全部筛选规则标识的来源分层

        :return: 按规则标识排序的来源条目
        """
        return self._origins(
            kind=RULE_KIND,
            builtin=dict(BUILTIN_RULE_SET),
            plugin=self._registry.rule_definitions(),
            user={
                rule.id: rule.model_dump()
                for rule in self._rules.get_custom_rules()
                if rule.id
            },
        )

    def list_rule_group_origins(self) -> List[FilterRuleOrigin]:
        """
        列出全部筛选规则组名的来源分层

        规则组没有内置层：内置的是规则本身，规则组一律由插件或用户给出。

        :return: 按规则组名排序的来源条目
        """
        return self._origins(
            kind=RULE_GROUP_KIND,
            builtin={},
            plugin=self._registry.rule_group_definitions(),
            user={
                group.name: group.model_dump()
                for group in self._user_rule_groups()
                if group.name
            },
        )

    def list_conflicts(self) -> List[FilterRuleOrigin]:
        """
        列出插件声明因跨插件同名而整体失效的标识

        :return: 按种类与标识排序的来源条目，仅含存在冲突的标识
        """
        origins = [*self.list_rule_origins(), *self.list_rule_group_origins()]
        return [origin for origin in origins if origin.conflict is not None]

    @staticmethod
    def _user_rule_groups() -> List[FilterRuleGroup]:
        """
        读取用户自己配置的规则组

        `RuleHelper.get_rule_groups()` 交出的是插件与用户合并后的结果，分不出哪个组
        是用户加的，因此此处直读用户配置。

        :return: 用户配置的规则组列表
        """
        groups: List[dict] = get_configured_system_config().get(
            SystemConfigKey.UserFilterRuleGroups
        )
        return [FilterRuleGroup(**group) for group in groups or []]

    def _origins(
        self,
        kind: str,
        builtin: Dict[str, dict],
        plugin: Dict[str, dict],
        user: Dict[str, dict],
    ) -> List[FilterRuleOrigin]:
        """
        把三层定义与插件声明的裁决结果合成为来源条目

        :param kind: 标识种类
        :param builtin: 内置层定义
        :param plugin: 插件层当前生效的定义，冲突失效的标识不在其中
        :param user: 用户自定义层定义
        :return: 按标识排序的来源条目
        """
        claims = {
            claim.identity: claim
            for claim in self._registry.claims()
            if claim.kind == kind
        }
        identities = sorted({*builtin, *plugin, *user, *claims})
        return [
            self._origin(
                kind=kind,
                identity=identity,
                builtin=builtin.get(identity),
                plugin=plugin.get(identity),
                user=user.get(identity),
                claim=claims.get(identity),
            )
            for identity in identities
        ]

    @staticmethod
    def _origin(
        kind: str,
        identity: str,
        builtin: Optional[dict],
        plugin: Optional[dict],
        user: Optional[dict],
        claim: Optional[FilterRuleClaim],
    ) -> FilterRuleOrigin:
        """
        判定一个标识的生效来源与被压住的下层

        :param kind: 标识种类
        :param identity: 规则标识或规则组名
        :param builtin: 内置层定义，无则为 None
        :param plugin: 插件层生效定义，无或已因冲突失效则为 None
        :param user: 用户自定义层定义，无则为 None
        :param claim: 插件对该标识的声明及裁决结果，无插件声明则为 None
        :return: 该标识的来源条目
        """
        layers: List[Tuple[FilterRuleLayer, dict]] = []
        if builtin is not None:
            layers.append((FilterRuleLayer(layer=BUILTIN_LAYER), builtin))
        if plugin is not None and claim is not None and claim.effective:
            owner = claim.owners[0]
            extension_id, instance_id = split_instance_key(owner)
            layers.append((
                FilterRuleLayer(
                    layer=PLUGIN_LAYER,
                    owner=owner,
                    extension_id=extension_id,
                    instance_id=instance_id,
                ),
                plugin,
            ))
        if user is not None:
            layers.append((FilterRuleLayer(layer=USER_LAYER), user))
        conflict = (
            FilterRuleConflict(
                plugins=list(claim.plugins), owners=list(claim.owners)
            )
            if claim is not None and not claim.effective
            else None
        )
        if not layers:
            return FilterRuleOrigin(
                id=identity, kind=kind, effective=False, conflict=conflict
            )
        source, definition = layers[-1]
        return FilterRuleOrigin(
            id=identity,
            kind=kind,
            effective=True,
            source=source,
            shadowed=[layer for layer, _ in layers[:-1]],
            conflict=conflict,
            definition=definition,
        )
