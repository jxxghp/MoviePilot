"""按真实会话 owner 管理 Python 内核，空闲 LRU 与后台回收共享同一规则。"""

import asyncio
import contextvars
import sys
import time
import uuid
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

from app.adapters.system.code.kernel import PythonKernel
from app.adapters.system.code.staging import sweep_stale
from app.agent.code.authority import CellAuthority
from app.agent.code.result import render_result
from app.agent.terminal.ownership import TerminalAccessError, TerminalScope
from app.agent.tools.base import run_agent_blocking
from app.foundation.identity import build_user_memory_key
from app.runtime.tasks import get_task_registry

REAPER_MIN_SECONDS = 30.0
REAPER_MAX_SECONDS = 300.0

@dataclass(eq=False)
class KernelEntry:
    """排队中的 cell 也占用 owner 引用，空闲回收不能杀掉尚未取得锁的调用。"""

    scope: TerminalScope
    kernel: PythonKernel
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    attached: set[asyncio.Task[Any]] = field(default_factory=set)
    last_used: float = field(default_factory=time.monotonic)
    retired: bool = False


class CodeSessionManager:
    """每会话变量跨轮保留，默认四个内核的空闲 LRU 和三十分钟空闲过期。"""

    def __init__(self, root: Path, *, max_kernels: int = 4, idle_seconds: float = 1800) -> None:
        """显式注入暂存根目录，不在模块导入时读取配置或启动后台任务。"""
        self.root, self.max_kernels, self.idle_seconds = root, max_kernels, idle_seconds
        self._entries: dict[tuple[Any, ...], KernelEntry] = {}
        self._lock = asyncio.Lock()
        self._reaper: asyncio.Task[Any] | None = None

    def _new_kernel(self, authority: CellAuthority, cwd: Path) -> PythonKernel:
        """内核文件按用户隔离，随机代次不会继承旧会话的变量、令牌或 socket。"""
        user_key = build_user_memory_key(authority.scope.user_id)
        if not user_key:
            raise TerminalAccessError()
        directory = self.root / 'users' / user_key / f'kernel-{uuid.uuid4().hex}'
        schemas = {name: tool.args_schema.model_json_schema() for name, tool in authority.tools.items()}
        return PythonKernel(directory, tuple(sorted(authority.tools)), cwd=cwd,
                            blocking=partial(run_agent_blocking, 'default'), schemas=schemas)

    async def _acquire(self, authority: CellAuthority, cwd: Path) -> tuple[KernelEntry, tuple[Any, ...]]:
        """键包含 owner 对象、工具集合、解释器和工作目录；身份相同的旧代次也不能复用。"""
        scope, tools = authority.scope, tuple(sorted(authority.tools))
        key = (scope, tools, sys.executable, str(cwd))
        async with self._lock:
            if not authority.available():
                raise TerminalAccessError()
            await self._sweep_locked()
            if not authority.available():
                raise TerminalAccessError()
            entry = self._entries.get(key)
            if entry is None or entry.retired:
                entry = KernelEntry(scope, self._new_kernel(authority, cwd))
                self._entries[key] = entry
            current = asyncio.current_task()
            assert current is not None
            entry.attached.add(current)
            if self._reaper is None or self._reaper.done():
                self._reaper = contextvars.Context().run(
                    get_task_registry().create, self._reap_loop(), owner='agent.code.reaper',
                )
            try:
                await self._sweep_locked()
            except BaseException:
                # 准入尚未返回时 execute 的 finally 尚未接管，取消也要释放排队引用。
                entry.attached.discard(current)
                raise
        return entry, key

    async def execute(self, authority: CellAuthority, code: str, *, cwd: Path,
                      reset: bool = False, timeout: float = 300) -> dict[str, Any]:
        """每次执行重新绑定当前 cell 身份，完成后撤销并保留仍有效的 Python 状态。"""
        entry, key = await self._acquire(authority, cwd)
        try:
            async with entry.lock:
                if entry.retired or not authority.available():
                    raise TerminalAccessError()
                return await self._run(entry, authority, code, reset=reset, timeout=timeout)
        finally:
            await authority.retire()
            current = asyncio.current_task()
            if current is not None:
                entry.attached.discard(current)
            entry.last_used = time.monotonic()
            if entry.kernel.closed and not entry.attached and self._entries.get(key) is entry:
                self._entries.pop(key, None)

    async def _run(self, entry: KernelEntry, authority: CellAuthority, code: str,
                   *, reset: bool, timeout: float) -> dict[str, Any]:
        """显式重置、已结束内核或失联会丢失状态；普通脚本异常保留已执行的赋值。"""
        started = time.monotonic()
        kernel = entry.kernel
        state_reset = kernel.process is not None and (reset or kernel.closed or kernel.process.returncode is not None)
        if state_reset:
            await kernel.close()
            kernel = self._new_kernel(authority, kernel.cwd)
            entry.kernel = kernel
        reused = kernel.process is not None
        try:
            if not reused:
                await kernel.start()
            payload = await kernel.execute(code, dispatch=authority.call, timeout=timeout)
            await authority.retire()
            authority.errors.extend(payload.get('rpc_errors', [])[:max(0, 5 - len(authority.errors))])
            result: dict[str, Any] = await run_agent_blocking('default', render_result, payload, directory=kernel.directory,
                                            output_directory=kernel.directory.parent / 'output', reused=reused,
                                            state_reset=state_reset, elapsed=time.monotonic() - started)
            if payload.get('status') == 'exit':
                await kernel.close()
        except TimeoutError:
            result = self._ended('timeout', '执行超时，Python 内核已终止，变量状态已丢失；下次调用重新开始。')
        except asyncio.CancelledError:
            await kernel.close()
            raise
        except Exception:
            await kernel.close()
            result = self._ended('error', 'Python 内核不可用，状态已丢失；请先核对工具回执再决定后续操作。')
        result.update(tool_calls_made=authority.calls, tool_calls=authority.log,
                      success=result.get('status') == 'success' and not authority.errors)
        if authority.errors:
            result['tool_errors'] = authority.errors
        return result

    @staticmethod
    def _ended(status: str, error: str) -> dict[str, Any]:
        """通信和取消边界只报告已确认的状态丢失，不把未知执行情况描述成成功。"""
        return {'status': status, 'error': error, 'output': '', 'exit_code': -1,
                'kernel': {'mode': 'session', 'ended': True, 'state_lost': True}}

    async def _sweep_locked(self) -> None:
        """空闲过期和容量淘汰都跳过活跃或排队中的 cell，完成关闭才移除登记。"""
        now = time.monotonic()
        idle = sorted(((key, entry) for key, entry in self._entries.items() if not entry.attached),
                      key=lambda item: item[1].last_used)
        for key, entry in idle:
            if now - entry.last_used > self.idle_seconds or len(self._entries) > self.max_kernels:
                entry.retired = True
                await entry.kernel.close()
                self._entries.pop(key, None)

    async def reap(self) -> None:
        """低频后台回收不依赖下一次调用，七天暂存残留只在专属目录内清理。"""
        async with self._lock:
            await self._sweep_locked()
            active = {entry.kernel.directory for entry in self._entries.values()}
        await run_agent_blocking('default', sweep_stale, self.root, active)

    async def _reap_loop(self) -> None:
        """登记器关停时回收所有 owner；正常空表退出，下一次使用再惰性启动。"""
        try:
            while self._entries:
                await asyncio.sleep(min(REAPER_MAX_SECONDS, max(REAPER_MIN_SECONDS, self.idle_seconds / 6)))
                await self.reap()
        finally:
            for scope in {entry.scope for entry in self._entries.values()}:
                await self.close_owner(scope)

    async def close_owner(self, scope: TerminalScope) -> bool:
        """先封住对应代次，再取消所有活跃及排队调用；超时保留登记而非假报已回收。"""
        entries = [(key, entry) for key, entry in self._entries.items() if entry.scope is scope]
        current = asyncio.current_task()
        tasks: set[asyncio.Task[Any]] = set()
        for _, entry in entries:
            entry.retired = True
            tasks.update(task for task in entry.attached if task is not current)
        for task in tasks:
            task.cancel()
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=6)
            if pending:
                return False
        for key, entry in entries:
            await entry.kernel.close()
            if self._entries.get(key) is entry:
                self._entries.pop(key, None)
        return True


code_session_manager: CodeSessionManager | None = None


def get_code_session_manager(root: Path) -> CodeSessionManager:
    """由首次已绑定的 Agent 执行初始化；宿主关闭只访问已存在实例。"""
    global code_session_manager
    if code_session_manager is None:
        code_session_manager = CodeSessionManager(root)
    return code_session_manager
