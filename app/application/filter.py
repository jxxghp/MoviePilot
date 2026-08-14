from typing import List, Optional

from app.db.oper.systemconfig import SystemConfigOper
from app.domain.context import MediaInfo
from app.schemas import CustomRule, FilterRuleGroup
from app.schemas.types import SystemConfigKey


class RuleHelper:
    """读取用户过滤规则配置，并按媒体上下文选择适用规则组。"""

    @staticmethod
    def get_rule_groups() -> List[FilterRuleGroup]:
        """返回用户配置的全部过滤规则组。"""
        rule_groups: List[dict] = SystemConfigOper().get(
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
        rules: List[dict] = SystemConfigOper().get(SystemConfigKey.CustomFilterRules)
        if not rules:
            return []
        return [CustomRule(**rule) for rule in rules]

    def get_custom_rule(self, rule_id: str) -> Optional[CustomRule]:
        """按 ID 返回一条自定义过滤规则。"""
        return next(
            (rule for rule in self.get_custom_rules() if rule.id == rule_id),
            None,
        )
