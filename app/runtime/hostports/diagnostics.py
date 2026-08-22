"""agent 扩展查询自检诊断报告的端口槽位。

自检诊断由 doctor 扩展实现，agent 扩展只声明所需的最小协议，
具体实现由组合根注入。
"""

from typing import Any, Protocol

from app.runtime.hostports.port import HostPort


class DiagnosticsProvider(Protocol):
    """agent 扩展所需的自检诊断查询能力。"""

    def run_doctor(self, deep: bool = False) -> Any:
        """
        运行只读自检诊断。

        :param deep: 是否执行更慢的深度探测，例如数据库 TCP 连通性检查
        :return: 自检报告对象，提供 to_dict() 方法可序列化为字典
        """
        ...


diagnostics_port: HostPort[DiagnosticsProvider] = HostPort("diagnostics")
