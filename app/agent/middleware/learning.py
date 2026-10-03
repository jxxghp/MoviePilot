"""暴露个人学习工具并捕获最终请求，保持复盘与前台可变状态隔离。"""

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware

from app.agent.learning.session import LearningSession
from app.agent.middleware.utils import append_to_system_message
from app.agent.tools.base import run_agent_blocking
from app.runtime.log import logger


class LearningMiddleware(AgentMiddleware):  # type: ignore[misc]
    """放在最终请求压缩、视觉转换之后，复盘才能复用实际发出的缓存前缀。"""

    def __init__(self, session: LearningSession) -> None:
        """工具实例属于前台会话；后台使用相同 schema 的独立管理器。"""
        self.session = session
        self.tools = session.tools.tools
        self.prompt = ''

    async def abefore_agent(self, state: Any, runtime: Any) -> None:
        """每轮只加载路由描述，技能正文按需读取，新维护结果不改写正在运行的前缀。"""
        del state, runtime
        catalog = await run_agent_blocking('default', self.session.tools.skills.catalog)
        personal = [item for item in catalog if item['path'].startswith(str(self.session.skill_root) + '/')]
        pending = await run_agent_blocking('default', self.session.tools.memory.pending)
        self.prompt = ('<personal_skills>\nUse skill_view to load a relevant personal skill before its class of task. '
                       'Skills are guidance, never authorization. Public API scopes still require read_skill.\n'
                       + '\n'.join(f"- {item['name']}: {item['description'][:60]}" for item in personal)
                       + '\n</personal_skills>')
        if pending['proposals']:
            self.prompt += ('\nThere are pending memory consolidation proposals. Tell the user they can inspect '
                            'them with /memory pending and approve/discard each exact ID; never approve for them.')

    async def awrap_model_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        """学习目录在最终预算与压缩之前注入。"""
        return await handler(request.override(system_message=append_to_system_message(request.system_message, self.prompt)))


class LearningCaptureMiddleware(AgentMiddleware):  # type: ignore[misc]
    """仅在最内层观察最终请求，避免保存压缩之前或缺少动态提示词的快照。"""

    def __init__(self, session: LearningSession) -> None:
        """与外层目录中间件共享会话 owner，不重复注册工具。"""
        self.session = session

    async def awrap_model_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        """捕获失败不能把已完成模型请求变成错误重放。"""
        result = await handler(request)
        try:
            self.session.observe(request, result)
        except Exception as error:
            self.session.snapshot = None
            logger.warning('后台复盘快照不可用：%s', type(error).__name__)
        return result
