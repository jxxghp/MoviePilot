"""查询工作流工具"""

import json
from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.db import AsyncSessionFactory
from app.db.workflow_oper import WorkflowOper
from app.log import logger


class QueryWorkflowsInput(BaseModel):
    """查询工作流工具的输入参数模型"""
    explanation: str = Field(..., description="Clear explanation of why this tool is being used in the current context")
    state: Optional[str] = Field("all", description="Filter workflows by state: 'W' for waiting, 'R' for running, 'P' for paused, 'S' for success, 'F' for failed, 'all' for all workflows (default: 'all')")
    name: Optional[str] = Field(None, description="Filter workflows by name (partial match, optional)")
    trigger_type: Optional[str] = Field("all", description="Filter workflows by trigger type: 'timer' for scheduled, 'event' for event-triggered, 'manual' for manual, 'all' for all types (default: 'all')")


class QueryWorkflowsTool(MoviePilotTool):
    name: str = "query_workflows"
    description: str = "Query workflow list and status. Shows workflow name, description, trigger type, state, execution count, and other workflow details. Supports filtering by state, name, and trigger type."
    args_schema: Type[BaseModel] = QueryWorkflowsInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据查询参数生成友好的提示消息"""
        state = kwargs.get("state", "all")
        name = kwargs.get("name")
        trigger_type = kwargs.get("trigger_type", "all")
        
        parts = ["查询工作流"]
        
        if state != "all":
            state_map = {"W": "等待", "R": "运行中", "P": "暂停", "S": "成功", "F": "失败"}
            parts.append(f"状态: {state_map.get(state, state)}")
        
        if trigger_type != "all":
            trigger_map = {"timer": "定时触发", "event": "事件触发", "manual": "手动触发"}
            parts.append(f"触发类型: {trigger_map.get(trigger_type, trigger_type)}")
        
        if name:
            parts.append(f"名称: {name}")
        
        return " | ".join(parts) if len(parts) > 1 else parts[0]

    async def run(self, state: Optional[str] = "all",
                  name: Optional[str] = None,
                  trigger_type: Optional[str] = "all", **kwargs) -> str:
        logger.info(f"执行工具: {self.name}, 参数: state={state}, name={name}, trigger_type={trigger_type}")

        try:
            # 获取数据库会话
            async with AsyncSessionFactory() as db:
                workflow_oper = WorkflowOper(db)
                workflows = await workflow_oper.async_list()
                
                # 过滤工作流
                filtered_workflows = []
                for wf in workflows:
                    # 按状态过滤
                    if state != "all" and wf.state != state:
                        continue
                    
                    # 按触发类型过滤
                    if trigger_type != "all":
                        if trigger_type == "timer" and wf.trigger_type not in ["timer", None]:
                            continue
                        elif trigger_type == "event" and wf.trigger_type != "event":
                            continue
                        elif trigger_type == "manual" and wf.trigger_type != "manual":
                            continue
                    
                    # 按名称过滤（部分匹配）
                    if name and wf.name and name.lower() not in wf.name.lower():
                        continue
                    
                    filtered_workflows.append(wf)
                
                if not filtered_workflows:
                    return "未找到相关工作流"
                
                # 转换为字典格式，只保留关键信息
                simplified_workflows = []
                for wf in filtered_workflows:
                    # 状态说明
                    state_map = {
                        "W": "等待",
                        "R": "运行中",
                        "P": "暂停",
                        "S": "成功",
                        "F": "失败"
                    }
                    state_desc = state_map.get(wf.state, wf.state)
                    
                    # 触发类型说明
                    trigger_type_map = {
                        "timer": "定时触发",
                        "event": "事件触发",
                        "manual": "手动触发"
                    }
                    trigger_type_desc = trigger_type_map.get(wf.trigger_type, wf.trigger_type or "定时触发")
                    
                    simplified = {
                        "id": wf.id,
                        "name": wf.name,
                        "description": wf.description,
                        "trigger_type": trigger_type_desc,
                        "state": state_desc,
                        "run_count": wf.run_count,
                        "timer": wf.timer,
                        "event_type": wf.event_type,
                        "add_time": wf.add_time,
                        "last_time": wf.last_time,
                        "current_action": wf.current_action
                    }
                    # wf.result 往往是执行日志或上下文快照，不适合作为列表查询结果返回。
                    simplified_workflows.append(simplified)
                
                result_json = json.dumps(simplified_workflows, ensure_ascii=False, indent=2)
                return result_json
        except Exception as e:
            logger.error(f"查询工作流失败: {e}", exc_info=True)
            return f"查询工作流时发生错误: {str(e)}"
