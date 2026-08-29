"""AgentManager 稳定门面。"""

from typing import Optional

from app.agent.memory import MemoryManager
from app.agent.tasks import AgentTaskOwner
from app.application.agent import AgentDataContext


class AgentManager(AgentTaskOwner):
    """
    AI 智能体管理门面。

    会话、生命周期和后台任务分别由继承链中的单一 owner 实现，门面只保留
    稳定构造身份与公开方法集合。
    """

    def __init__(
        self,
        data: Optional[AgentDataContext] = None,
        memory: Optional[MemoryManager] = None,
    ) -> None:
        """创建稳定门面并初始化会话 owner。"""
        super().__init__(data=data, memory=memory)


agent_manager = AgentManager()
