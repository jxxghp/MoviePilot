"""Python 内核的部署环境继承、父进程死亡通知和整组进程终止。"""

import asyncio
import os
import signal
import subprocess
import sys
from typing import Any


def child_environment(values: dict[str, str]) -> dict[str, str]:
    """管理员内核继承宿主部署配置和连接凭据，保留独立导入路径与 UTF-8 输出。"""
    # 内核与管理员 shell 同属宿主权限；过滤配置或密码会使脚本静默连接默认数据库。
    environment = values.copy()
    environment.pop('PYTHONPATH', None)
    environment.update(PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8', PYTHONUTF8='1')
    return environment


class ParentWatch:
    """POSIX 只继承死亡管道读端；宿主关闭写端或意外退出均让内核结束。"""

    def __init__(self) -> None:
        """Windows 用真实进程对象 handle，避免 PID 重用误认父进程。"""
        self.read_fd: int | None = None
        self.write_fd: int | None = None
        self.handle: Any = None
        self.close_handle: Any = None

    def prepare(self, environment: dict[str, str]) -> dict[str, Any]:
        """返回可直接传给 subprocess 的继承选项，凭据不通过 argv。"""
        if os.name != 'nt':
            self.read_fd, self.write_fd = os.pipe()
            environment['MOVIEPILOT_KERNEL_PARENT_DEATH_FD'] = str(self.read_fd)
            return {'pass_fds': (self.read_fd,), 'start_new_session': True}
        return self._windows(environment)

    def _windows(self, environment: dict[str, str]) -> dict[str, Any]:
        """通过 SYNCHRONIZE handle 建立 Windows 父进程死亡看护。"""
        import ctypes  # pylint: disable=import-outside-toplevel
        from ctypes import wintypes  # pylint: disable=import-outside-toplevel

        kernel32 = getattr(ctypes, 'WinDLL')('kernel32', use_last_error=True)
        kernel32.GetCurrentProcessId.restype = wintypes.DWORD
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = kernel32.OpenProcess(0x00100000, True, kernel32.GetCurrentProcessId())
        if not self.handle:
            raise OSError('无法建立 Python 内核父进程看护')
        self.close_handle = kernel32.CloseHandle
        environment['MOVIEPILOT_KERNEL_PARENT_PROCESS_HANDLE'] = str(int(self.handle))
        startup = getattr(subprocess, 'STARTUPINFO')()
        startup.lpAttributeList = {'handle_list': [int(self.handle)]}
        return {'startupinfo': startup, 'creationflags': getattr(subprocess, 'CREATE_NO_WINDOW')}

    def spawned(self) -> None:
        """子进程启动后宿主不再持有继承端，否则死亡通知无法可靠触发。"""
        if self.read_fd is not None:
            os.close(self.read_fd)
            self.read_fd = None
        if self.handle is not None:
            self.close_handle(self.handle)
            self.handle = None

    def close(self) -> None:
        """同步撤销父存活信号，重复清理不误关其他新建描述符。"""
        self.spawned()
        if self.write_fd is not None:
            os.close(self.write_fd)
            self.write_fd = None


async def kill_tree(process: asyncio.subprocess.Process) -> None:
    """取消与超时都终止整个进程组，不能只杀 Python 而遗留其命令。"""
    if os.name == 'nt':
        if process.returncode is not None:
            return
        killer = await asyncio.create_subprocess_exec('taskkill', '/PID', str(process.pid), '/T', '/F',
                                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        await asyncio.wait_for(killer.wait(), 5)
    else:
        for attempt in range(2):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                break
            except PermissionError as error:
                if sys.platform != 'darwin' or attempt:
                    raise
                # macOS 对只剩未回收僵尸的组返回 EPERM。先让 asyncio 回收根进程，
                # 再发一次组信号，不能因根已退出就漏掉仍存活的后代或吞掉真实权限错误。
                try:
                    await asyncio.wait_for(process.wait(), 5)
                except TimeoutError:
                    raise error from None
            else:
                break
    await asyncio.wait_for(process.wait(), 5)
