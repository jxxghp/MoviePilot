"""由宿主持有的终端任务身份及异步调用上下文，不接受模型声明归属。"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Optional

TERMINAL_SCOPE_CLOSE_TIMEOUT_SECONDS = 6.0


class TerminalAccessError(RuntimeError):
    """以相同错误隐藏不存在、失效或不属于当前任务的终端记录。"""

    def __init__(self) -> None:
        """固定消息不携带终端命令、输出、进程号或其他任务身份。"""
        super().__init__("当前任务无法访问此终端；请核对任务实际状态，勿自动重跑命令")


@dataclass(eq=False, frozen=True, slots=True)
class TerminalScope:
    """用宿主对象身份区分同名任务的不同代次，关闭后不能重新激活。"""

    user_id: str
    task_id: str
    kind: str
    _closed: bool = field(default=False, init=False, repr=False)
    changed: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)
    _active_runs: int = field(default=0, init=False, repr=False)
    runs_idle: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)

    def __post_init__(self) -> None:
        """初始化一次性命令的空闲边界，供作用域收口等待真实进程结束。"""
        self.runs_idle.set()

    @property
    def closed(self) -> bool:
        """公开只读封口事实，普通模型参数不能重新打开同一任务。"""
        return self._closed

    def seal(self) -> None:
        """在任何异步清理之前同步封口，并唤醒等待撤销的调用。"""
        object.__setattr__(self, "_closed", True)
        self.changed.set()

    def begin_run(self) -> None:
        """登记一次性命令，即使尚未取得并发槽也不能被作用域清理遗漏。"""
        if self.closed:
            raise TerminalAccessError()
        object.__setattr__(self, "_active_runs", self._active_runs + 1)
        self.runs_idle.clear()

    def finish_run(self) -> None:
        """发布一次性命令已经完成，允许作用域收口返回真实终态。"""
        remaining = max(0, self._active_runs - 1)
        object.__setattr__(self, "_active_runs", remaining)
        if remaining == 0:
            self.runs_idle.set()

    async def wait_runs(self) -> bool:
        """等待作用域下的 run 全部退出，超出有界回收时间则保留未收敛事实。"""
        try:
            await asyncio.wait_for(self.runs_idle.wait(), timeout=TERMINAL_SCOPE_CLOSE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            return False
        return self._active_runs == 0


_terminal_scope: ContextVar[Optional[TerminalScope]] = ContextVar("agent_terminal_scope", default=None)


@contextmanager
def bind_terminal_scope(scope: Optional[TerminalScope]) -> Iterator[None]:
    """只在当前异步调用链绑定宿主身份，异常和嵌套调用始终还原上层身份。"""
    token = _terminal_scope.set(scope)
    try:
        yield
    finally:
        _terminal_scope.reset(token)


def current_terminal_scope() -> Optional[TerminalScope]:
    """供宿主查看当前绑定对象，不创建默认用户或全局任务。"""
    return _terminal_scope.get()


def require_terminal_scope() -> TerminalScope:
    """缺少可信主体、任务身份或已经封口时拒绝进入终端能力。"""
    scope = current_terminal_scope()
    if scope is None or scope.closed or not scope.user_id or not scope.task_id:
        raise TerminalAccessError()
    return scope


async def close_terminal_scope(scope: TerminalScope) -> bool:
    """只收敛已装配的终端管理器，无终端任务封口时不物化进程能力。"""
    scope.seal()
    module = sys.modules.get("app.agent.terminal.manager")
    manager = getattr(module, "terminal_session_manager", None)
    manager_closed = True if manager is None else bool(await manager.close_owner(scope))
    return manager_closed and await scope.wait_runs()
