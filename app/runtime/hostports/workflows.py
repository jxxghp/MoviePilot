"""agent 扩展触发工作流执行的端口槽位。

工作流的执行由 workflow 扩展实现，agent 扩展只声明所需的最小协议，
具体实现由组合根注入。
"""

from typing import Optional, Protocol, Tuple

from app.runtime.hostports.port import HostPort


class WorkflowExecutionProvider(Protocol):
    """agent 扩展所需的工作流执行能力。"""

    def process(
        self, workflow_id: int, from_begin: Optional[bool] = True
    ) -> Tuple[bool, str]:
        """
        执行指定工作流。

        :param workflow_id: 工作流 ID
        :param from_begin: 是否从头开始执行，为 False 时从上次执行位置继续
        :return: (是否执行成功, 成功为空字符串、失败为错误原因)
        """
        ...


workflow_execution_port: HostPort[WorkflowExecutionProvider] = HostPort(
    "workflow_execution"
)
