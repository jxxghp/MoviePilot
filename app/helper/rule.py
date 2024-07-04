from typing import List, Optional

from app.db.systemconfig_oper import SystemConfigOper
from app.schemas import FilterRuleGroup, CustomRule
from app.schemas.types import SystemConfigKey


class RuleHelper:
    """
    规划帮助类
    """

    def __init__(self):
        self.systemconfig = SystemConfigOper()

    def get_rule_groups(self) -> List[FilterRuleGroup]:
        """
        获取用户所有规则组
        """
        rule_groups: List[dict] = self.systemconfig.get(SystemConfigKey.UserRuleGroups)
        if not rule_groups:
            return []
        return [FilterRuleGroup(**group) for group in rule_groups]

    def get_rule_group(self, group_name: str) -> Optional[FilterRuleGroup]:
        """
        获取规则组
        """
        rule_groups = self.get_rule_groups()
        for group in rule_groups:
            if group.name == group_name:
                return group
        return None

    def get_custom_rules(self) -> List[CustomRule]:
        """
        获取用户所有自定义规则
        """
        rules: List[dict] = self.systemconfig.get(SystemConfigKey.CustomFilterRules)
        if not rules:
            return []
        return [CustomRule(**rule) for rule in rules]

    def get_custom_rule(self, rule_id: str) -> Optional[CustomRule]:
        """
        获取自定义规则
        """
        rules = self.get_custom_rules()
        for rule in rules:
            if rule.id == rule_id:
                return rule
        return None
