"""执行工作流工具"""

from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.runtime.hostports.workflows import workflow_execution_port
from app.application.agentdata import WorkflowPort as WorkflowOper
from app.runtime.log import logger


class RunWorkflowInput(BaseModel):
    """执行工作流工具的输入参数模型"""

    workflow_id: int = Field(
        ..., description="Workflow ID (can be obtained from query_workflows tool)"
    )
    from_begin: Optional[bool] = Field(
        True,
        description="Whether to run workflow from the beginning (default: True, if False will continue from last executed action)",
    )


class RunWorkflowTool(MoviePilotTool):
    name: str = "run_workflow"
    tags: list[str] = [
        ToolTag.Write,
        ToolTag.Workflow,
        ToolTag.Admin,
    ]
    description: str = "Execute a specific workflow manually by workflow ID. Supports running from the beginning or continuing from the last executed action."
    args_schema: Type[BaseModel] = RunWorkflowInput
    require_admin: bool = True

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据工作流参数生成友好的提示消息"""
        workflow_id = kwargs.get("workflow_id")
        from_begin = kwargs.get("from_begin", True)

        message = f"执行工作流: {workflow_id}"
        if not from_begin:
            message += " (从上次位置继续)"
        else:
            message += " (从头开始)"

        return message

    @staticmethod
    def _run_workflow_sync(
        workflow_id: int, from_begin: Optional[bool] = True
    ) -> tuple[bool, str]:
        """同步执行工作流，放到专用线程池避免长流程阻塞 API 响应。"""
        return workflow_execution_port.resolve().process(
            workflow_id, from_begin=from_begin
        )

    async def run(
        self, workflow_id: int, from_begin: Optional[bool] = True, **kwargs
    ) -> str:
        logger.info(
            f"执行工具: {self.name}, 参数: workflow_id={workflow_id}, from_begin={from_begin}"
        )

        try:
            workflow_oper = WorkflowOper()
            workflow = await workflow_oper.async_get(workflow_id)

            if not workflow:
                return f"未找到工作流：{workflow_id}，请使用 query_workflows 工具查询可用的工作流"

            # 工作流执行链路包含大量同步步骤，统一放到 workflow 线程池。
            state, errmsg = await self.run_blocking(
                "workflow",
                self._run_workflow_sync,
                workflow.id,
                from_begin,
            )

            if not state:
                return f"执行工作流失败：{workflow.name} (ID: {workflow.id})\n错误原因：{errmsg}"
            else:
                return f"工作流执行成功：{workflow.name} (ID: {workflow.id})"
        except Exception as e:
            logger.error(f"执行工作流失败: {e}", exc_info=True)
            return f"执行工作流时发生错误: {str(e)}"
