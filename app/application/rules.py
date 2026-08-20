"""
规则域：用户规则组配置访问，内置规则定义与规则解析器经 app.domain.filterrule 再导出。
"""

from typing import List, Optional

from app.application.configuration import get_configured_system_config
from app.domain.context import MediaInfo
from app.domain.filterrule import BUILTIN_RULE_SET, RuleParser  # noqa: F401
from app.runtime.extensions.filter_rule_registry import plugin_filter_rule_registry
from app.schemas.rule import CustomRule
from app.schemas.system import FilterRuleGroup
from app.schemas.types import SystemConfigKey


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
