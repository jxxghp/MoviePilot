"""每会话学习计数、复盘任务 owner 和人工记忆/技能管理命令。"""

import asyncio
import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from langchain_core.messages import BaseMessage, HumanMessage

from app.agent.learning.memory import MemoryStore
from app.agent.learning.review import ReviewLoop, ReviewSnapshot
from app.agent.learning.skills import SkillLibrary
from app.agent.learning.tools import LearningTools
from app.agent.tools.base import run_agent_blocking
from app.agent.tools.result import TOOL_OBSERVATION_MARKER
from app.application.messaging.interaction.agent import is_agent_learning_command
from app.runtime.log import logger
from app.runtime.tasks import get_task_registry


@dataclass
class ReviewRun:
    """独立令牌避免旧任务结束回调清掉新任务，并给阻塞写入线程提供取消屏障。"""

    cancelled: threading.Event = field(default_factory=threading.Event)
    task: asyncio.Task[Any] | None = None


class LearningSession:
    """计数跨图缓存重建保留，按 Hermes 默认十轮记忆/十次工具迭代触发。"""

    def __init__(self, *, session_id: str, skill_root: Path, memory_root: Path,
                 public_roots: tuple[Path, ...], on_usage: Callable[[dict[str, Any]], None]) -> None:
        """会话与用户目录由宿主解析，后台和前台永不共享可变存储实例。"""
        self.session_id, self.skill_root, self.memory_root = session_id, skill_root, memory_root
        self.public_roots, self.on_usage = public_roots, on_usage
        self.tools = LearningTools(SkillLibrary(skill_root, public_roots=public_roots), MemoryStore(memory_root))
        self.turns = self.iterations = 0
        self.memory_due = False
        self.initialized = self.closed = False
        self.snapshot: ReviewSnapshot | None = None
        self.run: ReviewRun | None = None
        self.last_actions: list[dict[str, Any]] = []

    def seal(self) -> None:
        """关停同步入口立即禁止新复盘及后续后台写入。"""
        self.closed = True
        self.cancel()

    def cancel(self) -> None:
        """先封存储再取消模型请求；底层线程即使晚退也不能继续批次提交。"""
        if self.run:
            self.run.cancelled.set()
            if self.run.task and not self.run.task.done():
                self.run.task.cancel()

    async def wait_cancelled(self) -> bool:
        """前台只等两秒；超时保留 owner，不能伪称后台已退出。"""
        run = self.run
        self.cancel()
        if run and run.task and not run.task.done():
            await asyncio.wait({run.task}, timeout=2)
            return run.task.done()
        return True

    async def begin(self, messages: list[BaseMessage]) -> None:
        """先让旧复盘退出，再推进真实用户轮次；恢复会话用历史 human 条数初始化。"""
        await self.wait_cancelled()
        if not self.initialized:
            self.turns = max(0, sum(isinstance(message, HumanMessage) and not message.additional_kwargs.get(TOOL_OBSERVATION_MARKER)
                                   for message in messages) - 1) % 10
            self.initialized = True
        self.turns = (self.turns + 1) % 10
        self.memory_due = self.turns == 0
        self.snapshot = None
        self.tools.skills.reset_reads()
        self.tools.memory.reset_turn()

    def observe(self, request: Any, result: Any) -> None:
        """只捕获最终请求和本次返回；工具迭代按模型回合计数，不按并行工具个数计数。"""
        self.snapshot = ReviewSnapshot.capture(request, result)
        response = getattr(result, 'model_response', result)
        for message in response.result:
            calls = getattr(message, 'tool_calls', [])
            if calls:
                self.iterations += 1
            if any(call['name'] == 'skill_manage' for call in calls):
                self.iterations = 0

    def finish(self, *, delivered: bool) -> None:
        """仅在最终回复交付和持久化成功后触发，已有未退出任务时不堆叠复盘。"""
        if not delivered or self.closed or not self.snapshot or self.run:
            return
        skill_due = self.iterations >= 10
        if not self.memory_due and not skill_due:
            return
        if skill_due:
            self.iterations = 0
        run = ReviewRun()
        snapshot = self.snapshot
        self.run = run
        try:
            run.task = get_task_registry().create(self._review(run, snapshot, skill_due), owner='agent.learning.review')
        except RuntimeError:
            self.run = None
            return

        def complete(_task: asyncio.Task[Any]) -> None:
            """即使任务尚未启动就被取消，也按身份清除 owner，不留下永久占位。"""
            if self.run is run:
                self.run = None

        run.task.add_done_callback(complete)

    async def _review(self, run: ReviewRun, snapshot: ReviewSnapshot, skill_due: bool) -> None:
        """后台没有用户输出回调或父图 checkpointer，失败只记录类型且不重放业务操作。"""
        tools = LearningTools(
            SkillLibrary(self.skill_root, public_roots=self.public_roots, background=True,
                         source={'session_id': self.session_id, 'message_ids': [m.id for m in snapshot.messages[-20:] if m.id]},
                         cancelled=run.cancelled.is_set),
            MemoryStore(self.memory_root, background=True, cancelled=run.cancelled.is_set),
        )
        loop = ReviewLoop(snapshot, tools, review_memory=self.memory_due, review_skills=skill_due,
                          cancelled=run.cancelled.is_set, on_usage=self.on_usage)
        try:
            self.last_actions = await loop.run()
            logger.info('Agent 后台复盘结束：会话=%s，实际维护回执=%d，输入token=%d',
                        self.session_id, len(self.last_actions), loop.consumed)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning('Agent 后台复盘未完成：%s', type(error).__name__)
        finally:
            self.last_actions = list(loop.actions)

    async def command(self, text: str) -> str | None:
        """仅识别宿主传入的最新用户原文；模型工具无法自行审批提案或接管技能。"""
        match = re.fullmatch(r'/memory\s+(pending|approve|discard)(?:\s+([a-f0-9]{32}))?\s*', text)
        skill = re.fullmatch(r'/skills\s+(adopt|pin|unpin|restore)\s+([a-z0-9][a-z0-9_-]{0,63})\s*', text)
        if not match and not skill:
            return ('学习命令：/memory pending、/memory approve ID、/memory discard ID；/skills adopt NAME、/skills pin NAME、/skills unpin NAME、/skills restore NAME。'
                    if is_agent_learning_command(text) else None)
        try:
            if skill:
                result = await run_agent_blocking('default', self.tools.skills.control, skill[2], skill[1])
            else:
                assert match is not None
                if match[1] == 'pending':
                    result = await run_agent_blocking('default', self.tools.memory.pending, match[2])
                else:
                    result = await run_agent_blocking('default', self.tools.memory.resolve, match[2] or '', approve=match[1] == 'approve')
        except (OSError, ValueError) as error:
            result = dict(success=False, error=str(error))
        return json.dumps(result, ensure_ascii=False, indent=2)
