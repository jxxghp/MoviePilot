"""Python 内核的环境白名单、父进程死亡通知和整组进程终止。"""

import asyncio
import os
import signal
import subprocess
from typing import Any

_SECRET_PARTS = ('KEY', 'TOKEN', 'SECRET', 'PASSWORD', 'CREDENTIAL', 'PASSWD', 'AUTH', 'DSN',
                 'WEBHOOK', 'CREDS', 'BEARER', 'APIKEY')
_SAFE_PREFIXES = ('PATH', 'HOME', 'USER', 'LANG', 'LC_', 'TERM', 'TMPDIR', 'TMP', 'TEMP', 'SHELL',
                  'LOGNAME', 'XDG_', 'VIRTUAL_ENV', 'CONDA')
_WINDOWS_NAMES = frozenset({'SYSTEMROOT', 'SYSTEMDRIVE', 'WINDIR', 'COMSPEC', 'PATHEXT', 'OS',
                          'APPDATA', 'LOCALAPPDATA', 'USERPROFILE', 'HOMEDRIVE', 'HOMEPATH'})


def child_environment(values: dict[str, str]) -> dict[str, str]:
    """先拒绝敏感变量名，再允许必要的运行环境变量，不继承宿主 PYTHONPATH。"""
    environment = {
        key: value for key, value in values.items()
        if not any(part in key.upper() for part in _SECRET_PARTS)
        and (key.startswith(_SAFE_PREFIXES) or (os.name == 'nt' and key.upper() in _WINDOWS_NAMES))
    }
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
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    await asyncio.wait_for(process.wait(), 5)
