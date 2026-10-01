"""Agent 独立消息库的用例端口；与业务主库及恢复快照解耦。"""

import asyncio
from dataclasses import dataclass
from functools import partial
from time import monotonic
from typing import Any, Protocol

from app.application.database import AsyncDatabaseExecutor
from app.runtime.log import logger
from app.runtime.tasks import TaskRegistry


@dataclass(frozen=True, slots=True)
class RecallMessage:
    """保留完整脱敏文字和工具关联；压缩摘要不作为原始消息。"""

    message_id: str
    role: str
    content: str
    tool_call_id: str = ""
    tool_name: str = ""
    tool_calls: str = ""
    tool_status: str = ""
    timestamp: float = 0
    provenance: str = "observed"


@dataclass(frozen=True, slots=True)
class RecallSession:
    """宿主提供的会话元数据；不允许工具参数修改归属或续接关系。"""

    session_id: str
    source: str = "chat"
    title: str = ""
    model: str = ""
    started_at: float = 0
    last_active: float = 0
    parent_session_id: str = ""
    end_reason: str = ""


@dataclass(frozen=True, slots=True)
class RecallLegacyRecord:
    """升级前仍保留的恢复快照；无法恢复已被旧压缩丢弃的内容。"""

    id: int
    session: RecallSession
    messages: tuple[dict[str, Any], ...]


class RecallLegacyRepository(Protocol):
    """仅用于一次性搬迁现存快照，不是新消息库的在线存储依赖。"""

    def page(self, user_id: str, after: int, limit: int) -> tuple[RecallLegacyRecord, ...]:
        """按真实用户和主键分页导出，不能以显示名扩大身份范围。"""
        ...


@dataclass(frozen=True, slots=True)
class RecallQuery:
    """支持发现、阅读、锚点滚动和近期浏览四种检索形态；时间过滤作用于会话开始时间。"""

    query: str = ""
    session_id: str = ""
    around_message_id: int = 0
    window: int = 5
    limit: int = 3
    sort: str = ""
    detail: str = "adaptive"
    role_filter: tuple[str, ...] = ("user", "assistant")
    after: float | None = None
    before: float | None = None
    exclude_session_ids: tuple[str, ...] = ()
    current_session_id: str = ""


class RecallRepository(Protocol):
    """身份由宿主绑定的独立存储；任何读取都不得跨用户寻找同名会话。"""

    def append(self, user_id: str, session: RecallSession, messages: tuple[RecallMessage, ...]) -> None:
        """原子追加和同步索引，稳定消息 ID 保证重试幂等。"""
        ...

    def search(self, user_id: str, query: RecallQuery) -> dict[str, Any]:
        """返回实际消息的发现、阅读、窗口或近期会话结果，不调用 LLM。"""
        ...

    def compact(self, user_id: str, session_id: str, message_ids: tuple[str, ...]) -> None:
        """标记已离开活动上下文的消息，保留原文和索引可见性。"""
        ...

    def delete(self, user_id: str, session_id: str) -> None:
        """删除会话及索引，并防止在途旧消息重新创建它。"""
        ...

    def maintain(self, user_id: str, *, batch_size: int = 500) -> dict[str, Any]:
        """执行一批可续跑的历史搬迁和索引回填，返回是否仍有工作。"""
        ...


class RecallService:
    """通过有界宿主 worker 执行独立 SQLite I/O，不阻塞事件循环。"""

    def __init__(self, repository: RecallRepository, executor: AsyncDatabaseExecutor, tasks: TaskRegistry | None = None) -> None:
        """注入独立存储与现有执行预算，构造阶段不打开数据库。"""
        self._repository = repository
        self._executor = executor
        self._tasks = tasks
        self._maintenance: dict[str, asyncio.Task[None]] = {}

    async def append(self, user_id: str, session: RecallSession, messages: tuple[RecallMessage, ...]) -> None:
        """等待本批证据和索引一起提交，失败保持可重试。"""
        if messages:
            await self._executor.run(lambda: self._repository.append(user_id, session, messages))
            self._schedule_maintenance(user_id)

    async def search(self, user_id: str, query: RecallQuery) -> dict[str, Any]:
        """共享 worker 容量并保留查询超时和索引能力状态。"""
        self._schedule_maintenance(user_id)
        return await self._executor.run(lambda: self._repository.search(user_id, query))

    async def compact(self, user_id: str, session_id: str, message_ids: tuple[str, ...]) -> None:
        """仅在实际压缩成功后归档，不把异常丢失的状态当作成功压缩。"""
        if message_ids:
            await self._executor.run(lambda: self._repository.compact(user_id, session_id, message_ids))

    async def delete(self, user_id: str, session_id: str) -> None:
        """让显式历史删除同步收回独立消息库中的证据。"""
        await self._executor.run(lambda: self._repository.delete(user_id, session_id))

    def _schedule_maintenance(self, user_id: str) -> None:
        """每用户最多一个后台回填，进程最多八个，由宿主负责取消和关停。"""
        if self._tasks is None or user_id in self._maintenance or len(self._maintenance) >= 8:
            return
        task = self._tasks.create(self._maintain(user_id), owner='agent.history')
        self._maintenance[user_id] = task
        task.add_done_callback(partial(self._maintenance_finished, user_id))

    def _maintenance_finished(self, user_id: str, task: asyncio.Task[None]) -> None:
        """即使任务首次运行前被取消也释放名额，旧回调不能移除新任务。"""
        if self._maintenance.get(user_id) is task:
            self._maintenance.pop(user_id, None)

    async def _maintain(self, user_id: str) -> None:
        """每批释放数据库 worker 和事件循环；故障保留游标供下一次运行恢复。"""
        try:
            while True:
                started = monotonic()
                status = await self._executor.run(lambda: self._repository.maintain(user_id))
                if not status.get('pending'):
                    break
                # 以至少四倍批次耗时让出写入机会，限制后台回填占空比。
                await asyncio.sleep(max(0.2, 4 * (monotonic() - started)))
        except Exception as error:
            logger.warning('历史消息后台维护未完成: %s', type(error).__name__)
