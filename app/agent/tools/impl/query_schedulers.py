"""查询定时服务工具"""

import json
from typing import Optional, Type

from pydantic import BaseModel

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.log import logger


class QuerySchedulersInput(BaseModel):
    """查询运行时定时服务的输入参数模型。"""


class QuerySchedulersTool(MoviePilotTool):
    """查询系统、插件和工作流注册的运行时定时服务。"""

    name: str = "query_schedulers"
    tags: list[str] = [
        ToolTag.Read,
        ToolTag.Scheduler,
        ToolTag.Admin,
    ]
    description: str = (
        "Query runtime scheduler services registered by MoviePilot system components, "
        "plugins, and workflows. It excludes user-created autonomous agent tasks; use "
        "query_agent_tasks for reminders, monitoring tasks, and other agent schedules."
    )
    args_schema: Type[BaseModel] = QuerySchedulersInput
    require_admin: bool = True

    def get_tool_message(self, **kwargs: object) -> Optional[str]:
        """生成查询运行时定时服务的提示消息。"""
        return "查询系统定时服务"

    async def run(self, **kwargs: object) -> str:
        """查询非 Agent 自主任务的运行时定时服务。"""
        logger.info(f"执行工具: {self.name}")
        try:
            from app.scheduler import AGENT_TASK_JOB_PREFIX, Scheduler

            scheduler = Scheduler()
            agent_task_prefix = f"{AGENT_TASK_JOB_PREFIX}-"
            schedulers = [
                scheduler_item
                for scheduler_item in scheduler.list()
                if not str(scheduler_item.id or "").startswith(agent_task_prefix)
            ]
            if schedulers:
                schedulers_list = [
                    {
                        "id": scheduler_item.id,
                        "name": scheduler_item.name,
                        "provider": scheduler_item.provider,
                        "status": scheduler_item.status,
                        "next_run": scheduler_item.next_run,
                    }
                    for scheduler_item in schedulers
                ]
                result_json = json.dumps(schedulers_list, ensure_ascii=False, indent=2)
                total_count = len(schedulers_list)
                if total_count > 30:
                    limited_schedulers = schedulers_list[:30]
                    limited_json = json.dumps(
                        limited_schedulers,
                        ensure_ascii=False,
                        indent=2,
                    )
                    return (
                        f"注意：查询结果共找到 {total_count} 条，为节省上下文空间，"
                        f"仅显示前 30 条结果。\n\n{limited_json}"
                    )
                return result_json
            return "未找到系统、插件或工作流定时服务"
        except Exception as e:
            logger.error(f"查询定时服务失败: {e}", exc_info=True)
            return f"查询定时服务时发生错误: {str(e)}"
