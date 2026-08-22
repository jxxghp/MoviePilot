"""过滤扩展查询用户自定义规则组配置的端口槽位。

用户自定义规则组与自定义过滤规则保存在系统配置中，过滤扩展只声明
用得到的最小协议，具体实现由组合根注入。
"""

from typing import Any, List, Optional, Protocol

from app.runtime.hostports.port import HostPort
from app.schemas.rule import CustomRule
from app.schemas.system import FilterRuleGroup


class FilterRuleGroupProvider(Protocol):
    """过滤扩展所需的用户规则组查询能力。"""

    def get_custom_rules(self) -> List[CustomRule]:
        """返回用户配置的全部自定义过滤规则。"""
        ...

    def get_rule_group_by_media(
            self,
            media: Optional[Any] = None,
            group_names: Optional[list] = None,
    ) -> List[FilterRuleGroup]:
        """按媒体类型、分类和候选名称筛选适用规则组。"""
        ...


filter_rule_group_port: HostPort[FilterRuleGroupProvider] = HostPort("filter_rule_group")
