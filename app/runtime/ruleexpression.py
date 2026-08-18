"""过滤扩展解析规则表达式、取用内置规则集的端口槽位。

内置规则定义与规则表达式解析（含可选的原生加速）由规则领域实现维护，
过滤扩展只声明用得到的最小协议，具体实现由组合根注入。
"""

from typing import Dict, Protocol, Union

from app.runtime.hostport import HostPort


class RuleExpressionProvider(Protocol):
    """过滤扩展所需的内置规则集查询与规则表达式解析能力。"""

    def get_builtin_rule_set(self) -> Dict[str, dict]:
        """返回内置过滤规则定义。"""
        ...

    def parse_rule_group(self, rule_group: str) -> Union[list, str]:
        """解析单个优先级层级表达式，返回布尔组合结构或规则名称。"""
        ...


rule_expression_port: HostPort[RuleExpressionProvider] = HostPort("rule_expression")
