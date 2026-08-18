"""Agent 业务处理链。

AgentChain 是 agent 编排在链层的入口：Agent 运行时会话需要复用
ChainBase 提供的消息处理状态机（渠道处理状态、直发消息等），
因此继承关系归属链层；具体 Agent 运行时（MoviePilotAgent 等）留在 app.agent。
"""

from app.application.orchestration import ChainBase


class AgentChain(ChainBase):
    """Agent 业务处理链。"""

    pass
