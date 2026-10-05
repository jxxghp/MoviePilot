"""进程组回收兼容 macOS 僵尸状态，同时保留真实权限失败。"""

import errno
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.adapters.system.code import process as code_process

pytestmark = pytest.mark.skipif(os.name == 'nt', reason='仅验证 POSIX 进程组信号')


@pytest.mark.asyncio
@pytest.mark.parametrize('retry_error', [None, ProcessLookupError(errno.ESRCH, 'group gone')])
async def test_macos_reaps_root_then_signals_remaining_group(monkeypatch, retry_error):
    """根回收后仍须发送组信号，兼顾存活后代和已消失的进程组。"""
    process = SimpleNamespace(pid=12345, wait=AsyncMock(return_value=-9))
    denied = PermissionError(errno.EPERM, 'zombie group')

    def signal_group(_pid, _signal):
        if process.wait.await_count == 0:
            raise denied
        if retry_error is not None:
            raise retry_error

    signal = Mock(side_effect=signal_group)
    monkeypatch.setattr(code_process, 'sys', SimpleNamespace(platform='darwin'))
    monkeypatch.setattr(code_process.os, 'killpg', signal)

    await code_process.kill_tree(process)

    assert signal.call_count == 2
    process.wait.assert_awaited()


@pytest.mark.asyncio
async def test_macos_repeated_permission_error_is_not_swallowed(monkeypatch):
    """即使根已退出，组内其他进程的权限失败仍必须交回调用方。"""
    process = SimpleNamespace(pid=12345, wait=AsyncMock(return_value=0))
    denied = PermissionError(errno.EPERM, 'live descendant denied')
    signal = Mock(side_effect=denied)
    monkeypatch.setattr(code_process, 'sys', SimpleNamespace(platform='darwin'))
    monkeypatch.setattr(code_process.os, 'killpg', signal)

    with pytest.raises(PermissionError) as raised:
        await code_process.kill_tree(process)

    assert raised.value is denied
    assert signal.call_count == 2


@pytest.mark.asyncio
async def test_macos_unreaped_root_keeps_original_permission_error(monkeypatch):
    """根无法回收时不能把清理权限失败伪装成脚本执行超时。"""
    process = SimpleNamespace(pid=12345, wait=AsyncMock(side_effect=TimeoutError))
    denied = PermissionError(errno.EPERM, 'live root denied')
    signal = Mock(side_effect=denied)
    monkeypatch.setattr(code_process, 'sys', SimpleNamespace(platform='darwin'))
    monkeypatch.setattr(code_process.os, 'killpg', signal)

    with pytest.raises(PermissionError) as raised:
        await code_process.kill_tree(process)

    assert raised.value is denied
    signal.assert_called_once()


@pytest.mark.asyncio
async def test_other_platform_permission_error_is_not_retried(monkeypatch):
    """macOS 兼容分支不改变其他 POSIX 平台的权限错误处理。"""
    process = SimpleNamespace(pid=12345, wait=AsyncMock())
    denied = PermissionError(errno.EPERM, 'permission denied')
    signal = Mock(side_effect=denied)
    monkeypatch.setattr(code_process, 'sys', SimpleNamespace(platform='linux'))
    monkeypatch.setattr(code_process.os, 'killpg', signal)

    with pytest.raises(PermissionError) as raised:
        await code_process.kill_tree(process)

    assert raised.value is denied
    signal.assert_called_once()
    process.wait.assert_not_awaited()
