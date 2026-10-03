"""本地持久 Python cell 协议的异步进程与 RPC 适配。"""

import asyncio
import contextvars
import json
import os
import secrets
import shutil
import sys
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from app.adapters.system.code.client import build_client
from app.adapters.system.code.output import STDERR_BYTES, STDOUT_BYTES, OutputBuffer
from app.adapters.system.code.process import ParentWatch, child_environment, kill_tree
from app.adapters.system.code.runner import RUNNER_SOURCE
from app.runtime.tasks import get_task_registry

Dispatch = Callable[[str, dict[str, Any]], Awaitable[Any]]
MAX_FRAME_BYTES = 16 * 1024 * 1024


class PythonKernel:
    """一个串行 cell 内核；变量持久，但每个 cell 的工具授权必须由调用方重新绑定。"""

    def __init__(self, directory: Path, tools: tuple[str, ...], *, blocking: Callable[..., Awaitable[Any]],
                 cwd: Path | None = None, schemas: dict[str, dict[str, Any]] | None = None) -> None:
        """运行目录属于单个宿主 owner，工具集变化必须创建新内核。"""
        self.directory, self.tools = directory, tools
        self._blocking = blocking
        self.schemas = schemas
        self.cwd = cwd if cwd is not None else directory
        self.process: asyncio.subprocess.Process | None = None
        self.server: asyncio.AbstractServer | None = None
        self.endpoint = ''
        self._socket_directory = ''
        self._token, self._sentinel = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        self._watch = ParentWatch()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._responses: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=2)
        self._raw, self._stderr = OutputBuffer(STDOUT_BYTES), OutputBuffer(STDERR_BYTES)
        self._dispatch: Dispatch | None = None
        self._rpc_errors: list[dict[str, str]] = []
        self._rpc_lock = asyncio.Lock()
        self._cell_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._prepare: asyncio.Task[Any] | None = None
        self._startup: asyncio.Task[Any] | None = None
        self._spawn: asyncio.Task[Any] | None = None
        self.closed = False
        self._cleaned = False
        self.execution_count = 0

    def _own(self, task: asyncio.Task[Any]) -> asyncio.Task[Any]:
        """读取与 RPC 连接都交给宿主登记器，不创建无人收口的后台任务。"""
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _files(self) -> None:
        """启动前以私有权限生成 runner/stubs，用户代码不进入命令行或环境变量。"""
        self.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(self.directory, 0o700)
        if os.name != 'nt':
            self._socket_directory = tempfile.mkdtemp(prefix='mp-code-', dir='/tmp')
        for name, source in (('moviepilot_tools.py', build_client(self.tools, self.schemas)), ('kernel.py', RUNNER_SOURCE)):
            descriptor = os.open(self.directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                stream.write(source)

    def _connected(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """同步接收回调显式登记连接 owner，关闭期间立即拒绝新连接。"""
        if self.closed:
            writer.close()
            return
        try:
            self._own(get_task_registry().create(self._serve(reader, writer), owner='agent.code.rpc.connection'))
        except RuntimeError:
            writer.close()

    async def start(self) -> None:
        """先建立私有本地端点，再启动可被回收的子进程；失败沿同一关闭路径收口。"""
        try:
            async with self._lifecycle_lock:
                # 长驻传输任务不能保留首个 cell 的图状态、完整消息或旧身份上下文。
                self._startup = self._own(contextvars.Context().run(
                    get_task_registry().create, self._start_process(), owner='agent.code.startup', cancel_on_shutdown=False,
                ))
                await asyncio.shield(self._startup)
        except BaseException:
            await self.close()
            raise

    async def _start_process(self) -> None:
        """创建步骤与关闭互斥，外部封口不能在端点建立途中漏掉新创建的资源。"""
        if self.closed or self._prepare is not None:
            raise RuntimeError('Python kernel has already been started or closed')
        self._prepare = self._own(get_task_registry().create(self._prepare_files(), owner='agent.code.prepare', cancel_on_shutdown=False))
        await asyncio.shield(self._prepare)
        if self.closed:
            raise RuntimeError('Python kernel closed during preparation')
        await self._listen()
        environment = child_environment(dict(os.environ))
        environment.update(MOVIEPILOT_RPC_SOCKET=self.endpoint, MOVIEPILOT_RPC_TOKEN=self._token,
                           MOVIEPILOT_RPC_PERSISTENT='1', MOVIEPILOT_KERNEL_SENTINEL=self._sentinel,
                           MOVIEPILOT_KERNEL_SPILL_DIR=str(self.directory))
        options = self._watch.prepare(environment)
        self._spawn = self._own(get_task_registry().create(asyncio.create_subprocess_exec(
            sys.executable, str(self.directory / 'kernel.py'), cwd=self.cwd, env=environment,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            **options), owner='agent.code.spawn', cancel_on_shutdown=False))
        self.process = await asyncio.shield(self._spawn)
        self._watch.spawned()
        self._own(get_task_registry().create(self._stdout(), owner='agent.code.stdout'))
        self._own(get_task_registry().create(self._read_stderr(), owner='agent.code.stderr'))

    async def _prepare_files(self) -> None:
        """登记器接管可等待的文件准备过程，具体阻塞执行由宿主注入。"""
        await self._blocking(self._files)

    async def _listen(self) -> None:
        """POSIX 用短路径0600 UDS，Windows 用随机端口回环 TCP 加随机令牌。"""
        if os.name == 'nt':
            self.server = await asyncio.start_server(self._connected, '127.0.0.1', 0, limit=1024 * 1024)
            self.endpoint = f'tcp://127.0.0.1:{self.server.sockets[0].getsockname()[1]}'
        else:
            self.endpoint = str(Path(self._socket_directory) / 'rpc.sock')
            self.server = await asyncio.start_unix_server(self._connected, self.endpoint, limit=1024 * 1024)
            os.chmod(self.endpoint, 0o600)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """逐行认证且串行派发；连接本身不携带上一个 cell 的认证上下文。"""
        try:
            while not self.closed:
                line = await asyncio.wait_for(reader.readline(), 300)
                if not line:
                    break
                dispatch = self._dispatch
                async with self._rpc_lock:
                    result = await self._request(line, dispatch)
                writer.write((json.dumps(result, ensure_ascii=False) + '\n').encode('utf-8'))
                await writer.drain()
        except (TimeoutError, ConnectionError, ValueError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    async def _request(self, line: bytes, dispatch: Dispatch | None) -> Any:
        """令牌、结构和活动 cell 都通过后才把请求交给业务授权回调。"""
        try:
            request = json.loads(line)
        except (ValueError, UnicodeError):
            return {'error': 'Malformed RPC request'}
        if not isinstance(request, dict) or not secrets.compare_digest(
            str(request.get('token') or '').encode(), self._token.encode()
        ):
            return {'error': 'Unauthorized RPC request'}
        if self.closed or dispatch is None or dispatch is not self._dispatch:
            return {'error': 'No active execute_code cell'}
        name, arguments = request.get('tool'), request.get('args')
        if name not in self.tools or not isinstance(arguments, dict):
            error = 'Tool or arguments unavailable in execute_code'
            if len(self._rpc_errors) < 5:
                self._rpc_errors.append({'tool': str(name)[:96], 'error': error})
            return {'error': error}
        return await dispatch(name, arguments)

    async def _stdout(self) -> None:
        """按随机标记和字节长度分帧，直接 fd 输出作为当前 cell 原始输出。"""
        assert self.process is not None and self.process.stdout is not None
        stream = self.process.stdout
        marker = ('\n' + self._sentinel + ' ').encode()
        buffer = b''
        try:
            while chunk := await stream.read(4096):
                buffer += chunk
                buffer = await self._frames(buffer, marker, stream)
        except (ValueError, asyncio.IncompleteReadError):
            await self._responses.put({'status': 'protocol-error'})
        finally:
            self._raw.append(buffer)
            if not self.closed:
                await self._responses.put({'status': 'kernel-eof'})

    async def _frames(self, buffer: bytes, marker: bytes, stream: asyncio.StreamReader) -> bytes:
        """有界处理可能跨读取分片的头和正文，损坏长度不能导致宿主无限分配内存。"""
        while True:
            index = buffer.find(marker)
            if index < 0:
                keep = min(len(buffer), len(marker))
                self._raw.append(buffer[:-keep] if keep else buffer)
                return buffer[-keep:] if keep else b''
            self._raw.append(buffer[:index])
            remaining = buffer[index + len(marker):]
            newline = remaining.find(b'\n')
            if newline < 0:
                if len(remaining) > 32:
                    raise ValueError('Invalid kernel frame header')
                return buffer[index:]
            length = int(remaining[:newline])
            if not 0 <= length <= MAX_FRAME_BYTES:
                raise ValueError('Kernel frame exceeds budget')
            body = remaining[newline + 1:]
            if len(body) < length:
                body += await stream.readexactly(length - len(body))
            payload = json.loads(body[:length])
            if not isinstance(payload, dict):
                raise ValueError('Invalid kernel payload')
            await self._responses.put(payload)
            buffer = body[length:]

    async def _read_stderr(self) -> None:
        """即使错误流超出10KB也持续排空，避免子进程阻塞在未读取的管道。"""
        assert self.process is not None and self.process.stderr is not None
        while chunk := await self.process.stderr.read(4096):
            self._stderr.append(chunk)

    async def execute(self, code: str, *, dispatch: Dispatch, timeout: float) -> dict[str, Any]:
        """脚本异常保留变量；超时、取消或通信失败销毁内核，不能复用半次执行。"""
        async with self._cell_lock:
            try:
                return await self._execute_cell(code, dispatch=dispatch, timeout=timeout)
            except BaseException:
                await self.close()
                raise

    async def _execute_cell(self, code: str, *, dispatch: Dispatch, timeout: float) -> dict[str, Any]:
        """只有当前串行 cell 可以消费响应；排队等待不复用前一次的派发回调。"""
        if self.closed or self.process is None or self.process.stdin is None:
            raise RuntimeError('Python kernel is unavailable')
        self._raw.drain()
        self._stderr.drain()
        self._rpc_errors = []
        self._dispatch = dispatch
        identifier = uuid.uuid4().hex
        try:
            async with asyncio.timeout(timeout):
                self.process.stdin.write((json.dumps({'id': identifier, 'code': code}) + '\n').encode())
                await self.process.stdin.drain()
                payload = await self._responses.get()
                if payload.get('id') != identifier:
                    raise RuntimeError('Python kernel ended without a matching cell result')
                raw, raw_total = self._raw.drain()
                stderr, stderr_total = self._stderr.drain()
                payload.update(raw_stdout=raw, raw_stdout_bytes=raw_total,
                               raw_stderr=stderr, raw_stderr_bytes=stderr_total, rpc_errors=list(self._rpc_errors))
                self.execution_count = int(payload['execution_count'])
                return payload
        finally:
            self._dispatch = None

    async def close(self) -> bool:
        """先封 RPC 再终止进程，最后回收任务、描述符和私有临时文件。"""
        self.closed, self._dispatch = True, None
        async with self._lifecycle_lock:
            if not self._cleaned:
                await self._cleanup()
                self._cleaned = True
        return True

    async def _cleanup(self) -> None:
        """等待不可撤销的创建步骤结算后再删除文件，避免迟到创建留下孤儿资源。"""
        if self._startup is not None:
            await asyncio.gather(asyncio.shield(self._startup), return_exceptions=True)
        if self._prepare is not None:
            await asyncio.gather(asyncio.shield(self._prepare), return_exceptions=True)
        if self.server is not None:
            # wait_closed 会等待持久连接，必须在进程和连接任务退出后再等待。
            self.server.close()
        if self._spawn is not None:
            spawned = await asyncio.gather(asyncio.shield(self._spawn), return_exceptions=True)
            if isinstance(spawned[0], asyncio.subprocess.Process):
                self.process = spawned[0]
        if self.process is not None:
            await kill_tree(self.process)
        self._watch.close()
        tasks = [task for task in self._tasks if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.server is not None:
            await self.server.wait_closed()
        await self._blocking(shutil.rmtree, self.directory, True)
        if self._socket_directory:
            await self._blocking(shutil.rmtree, self._socket_directory, True)
