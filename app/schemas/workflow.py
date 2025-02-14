from abc import ABC, abstractmethod
from typing import Optional

from pydantic import BaseModel, Field


class Workflow(BaseModel):
    """
    工作流信息
    """
    name: Optional[str] = Field(None, description="工作流名称")
    description: Optional[str] = Field(None, description="工作流描述")
    timer: Optional[str] = Field(None, description="定时器")
    state: Optional[str] = Field(None, description="状态")
    current_action: Optional[str] = Field(None, description="当前执行动作")
    result: Optional[str] = Field(None, description="任务执行结果")
    run_count: Optional[int] = Field(0, description="已执行次数")
    actions: Optional[list] = Field([], description="任务列表")
    add_time: Optional[str] = Field(None, description="创建时间")
    last_time: Optional[str] = Field(None, description="最后执行时间")


class Action(BaseModel):
    """
    动作信息
    """
    name: Optional[str] = Field(None, description="动作名称")
    description: Optional[str] = Field(None, description="动作描述")


class ActionContext(BaseModel, ABC):
    """
    动作上下文
    """
    pass
