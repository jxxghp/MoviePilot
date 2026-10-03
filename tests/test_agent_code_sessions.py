"""代码内核的真实会话归属、当前身份、预算、回收和输出续读。"""

import asyncio
import contextvars
import os
import time
from pathlib import Path

import psutil
import pytest
import pytest_asyncio

from app.adapters.system.code.staging import STAGING_RETENTION_SECONDS, sweep_stale
from app.agent.code import manager as code_manager
from app.agent.code.authority import CellAuthority, allowed_tools
from app.agent.code.manager import CodeSessionManager
from app.agent.policy.contracts import AuthSource, PrincipalType, ToolOrigin, ToolPolicyContext
from app.agent.terminal.ownership import TerminalScope, close_terminal_scope
from app.agent.tools import base
from app.agent.tools.impl.api import MoviePilotApiTool
from app.agent.tools.impl.read_file import ReadFileTool
from app.foundation.identity import build_user_memory_key
from app.runtime.tasks import get_task_registry


@pytest_asyncio.fixture
async def manager(tmp_path):
    """每项用例使用独立管理器，真实内核与后台回收任务都在结束时收口。"""
    instance = CodeSessionManager(tmp_path / 'code', max_kernels=2)
    try:
        yield instance
    finally:
        for scope in {entry.scope for entry in instance._entries.values()}:
            await instance.close_owner(scope)
        if instance._reaper is not None:
            instance._reaper.cancel()
            await asyncio.gather(instance._reaper, return_exceptions=True)


def _context(scope, *, admin=True):
    """提供宿主实际持有的可变角色引用，不从脚本参数生成身份。"""
    return ToolPolicyContext(session_id=scope.task_id, user_id=scope.user_id, origin=ToolOrigin.AGENT_INTERACTIVE,
                             principal_type=PrincipalType.HUMAN, auth_source=AuthSource.WEB_SESSION,
                             agent_context={'is_admin': admin})


def _authority(scope, dispatch, context=None):
    """使用真实 API 工具类，只有实际业务执行被替换为无出站回执。"""
    api = MoviePilotApiTool(session_id=scope.task_id, user_id=scope.user_id)
    object.__setattr__(api, '_agent_tool_source', 'builtin')
    return CellAuthority(scope, context or _context(scope), allowed_tools([api]), dispatch)


async def _dispatch(_tool, arguments):
    """回放只读数据，供真实 Python 子进程循环调用。"""
    return {'success': True, 'data': arguments.get('query', {})}


@pytest.mark.asyncio
async def test_persistence_reset_and_object_identity_isolation(manager, tmp_path):
    """同 owner 跨轮保持变量；reset 清空；同名会话的新 owner 对象不能继承。"""
    scope = TerminalScope('alice', 'conversation', 'interactive')
    first = await manager.execute(_authority(scope, _dispatch), 'value = 42', cwd=tmp_path)
    second = await manager.execute(_authority(scope, _dispatch), 'print(value)', cwd=tmp_path)
    assert first['kernel']['reused'] is False and second['kernel']['reused'] is True
    assert second['output'] == '42\n' and second['kernel']['execution_count'] == 2
    reset = await manager.execute(_authority(scope, _dispatch), 'print("value" in globals())', cwd=tmp_path, reset=True)
    assert reset['output'] == 'False\n' and reset['kernel']['state_reset']
    fresh = TerminalScope('alice', 'conversation', 'interactive')
    isolated = await manager.execute(_authority(fresh, _dispatch), 'print("value" in globals())', cwd=tmp_path)
    assert isolated['output'] == 'False\n' and isolated['kernel']['execution_count'] == 1


@pytest.mark.asyncio
async def test_rpc_rebinds_context_every_cell_and_does_not_freeze_first_role(manager, tmp_path):
    """持久 RPC 连接每个 cell 进入当前上下文，角色撤销立即阻止后续工具调用。"""
    scope = TerminalScope('alice', 'live-role', 'interactive')
    context = _context(scope)
    marker = contextvars.ContextVar('test_code_marker', default='missing')

    async def dispatch(_tool, _arguments):
        """读取当前调用上下文，并在宿主模拟角色撤销。"""
        if marker.get() == 'second':
            context.agent_context['is_admin'] = False
        return {'marker': marker.get()}

    source = 'from moviepilot_tools import moviepilot_api\nprint(moviepilot_api(operation_id="subscription.list"))'
    token = marker.set('first')
    try:
        first = await manager.execute(_authority(scope, dispatch, context), source, cwd=tmp_path)
        marker.set('second')
        second = await manager.execute(_authority(scope, dispatch, context), source + '\n' + source, cwd=tmp_path)
    finally:
        marker.reset(token)
    assert "'marker': 'first'" in first['output']
    assert "'marker': 'second'" in second['output']
    assert second['tool_calls_made'] == 1 and second['success'] is False
    assert '权限已变化' in second['tool_errors'][0]['error']


@pytest.mark.asyncio
async def test_budget_and_denied_calls_cannot_be_hidden_by_exit_zero(manager, tmp_path):
    """第51次调用不派发，写操作和敏感读取都返回外层失败，即使程序本身成功退出。"""
    scope = TerminalScope('alice', 'budget', 'interactive')
    dispatched = []

    async def dispatch(tool, arguments):
        """记录实际执行次数，而不是仅检查计数字段。"""
        dispatched.append(arguments)
        return await _dispatch(tool, arguments)

    result = await manager.execute(_authority(scope, dispatch),
                                   'from moviepilot_tools import moviepilot_api\n'
                                   'for _ in range(51):\n    moviepilot_api(operation_id="subscription.list")', cwd=tmp_path)
    assert len(dispatched) == result['tool_calls_made'] == 50
    assert result['exit_code'] == 0 and result['success'] is False
    assert '50' in result['tool_errors'][0]['error']
    denied = await manager.execute(_authority(scope, dispatch),
                                   'moviepilot_api(operation_id="subscription.delete", path_params={"subscribe_id": 1})\n'
                                   'moviepilot_api(operation_id="config.system.get", query={"show_secrets": True})', cwd=tmp_path)
    assert len(dispatched) == 50 and len(denied['tool_errors']) == 2
    assert denied['exit_code'] == 0 and denied['success'] is False


@pytest.mark.asyncio
async def test_timeout_discards_state_and_next_call_starts_fresh(manager, tmp_path):
    """超时明确状态丢失，随后执行不能误用半次 cell 留下的变量。"""
    scope = TerminalScope('alice', 'timeout', 'interactive')
    timed_out = await manager.execute(_authority(scope, _dispatch), 'value = 10\nimport time\ntime.sleep(60)',
                                      cwd=tmp_path, timeout=0.05)
    assert timed_out['status'] == 'timeout' and timed_out['kernel']['state_lost']
    assert not manager._entries
    next_result = await manager.execute(_authority(scope, _dispatch), 'print("value" in globals())', cwd=tmp_path)
    assert next_result['output'] == 'False\n' and not next_result['kernel']['reused']


@pytest.mark.asyncio
async def test_host_scope_close_cancels_active_python(manager, tmp_path, monkeypatch):
    """真实宿主作用域关闭同时收口正在运行的 Python 和全部派发任务。"""
    monkeypatch.setattr(code_manager, 'code_session_manager', manager)
    scope = TerminalScope('alice', 'cancel', 'interactive')
    entered = asyncio.Event()

    async def dispatch(tool, arguments):
        """等待子进程已到达 RPC 边界，再触发宿主作用域关闭。"""
        entered.set()
        return await _dispatch(tool, arguments)

    task = get_task_registry().create(manager.execute(_authority(scope, dispatch),
        'from moviepilot_tools import moviepilot_api\nmoviepilot_api(operation_id="subscription.list")\n'
        'import time\ntime.sleep(60)', cwd=tmp_path), owner='test.code.scope')
    await asyncio.wait_for(entered.wait(), 5)
    assert await close_terminal_scope(scope)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not manager._entries and scope.closed


@pytest.mark.asyncio
async def test_idle_lru_and_autonomous_reaper(manager, tmp_path, monkeypatch):
    """容量淘汰最旧空闲会话，停止新调用后后台仍会回收过期内核。"""
    scopes = [TerminalScope('alice', str(index), 'interactive') for index in range(3)]
    for scope in scopes:
        await manager.execute(_authority(scope, _dispatch), 'pass', cwd=tmp_path)
    assert {entry.scope for entry in manager._entries.values()} == set(scopes[1:])
    manager.idle_seconds = 0.01
    monkeypatch.setattr(code_manager, 'REAPER_MIN_SECONDS', 0.01)
    manager._reaper.cancel()
    await asyncio.gather(manager._reaper, return_exceptions=True)
    await manager.execute(_authority(scopes[0], _dispatch), 'pass', cwd=tmp_path)
    await asyncio.wait_for(asyncio.shield(manager._reaper), 2)
    assert not manager._entries


@pytest.mark.asyncio
async def test_output_spill_survives_kernel_exit_and_is_sanitized(manager, tmp_path):
    """超过内联上限的正文保存在独立目录，退出内核后仍可续读且不泄漏常见凭据。"""
    scope = TerminalScope('alice', 'spill', 'interactive')
    result = await manager.execute(_authority(scope, _dispatch),
        'print("A" * 1100000)\nprint("api_key=sk-secret0123456789")\nraise SystemExit(0)', cwd=tmp_path)
    assert result['kernel']['ended'] and not manager._entries
    path = Path(result['stdout_spill_path'])
    body = path.read_text()
    assert len(body) > 1000000 and 'sk-secret0123456789' not in body
    assert path.parent.name == 'output' and 'sk-secret0123456789' not in result['output']
    assert result['stdout_capture_incomplete'] is False


@pytest.mark.asyncio
@pytest.mark.parametrize('ending', ['raise SystemExit(0)', 'import time; time.sleep(60)'])
async def test_exit_and_timeout_reap_descendants(manager, tmp_path, ending):
    """根进程主动退出与超时都收口继承管道的子进程，不能因根已退出而跳过进程组。"""
    if os.name == 'nt':
        pytest.skip('真实 POSIX 进程组回收')
    scope = TerminalScope('alice', 'tree', 'interactive')
    pid_file = tmp_path / 'child.pid'
    code = ('import subprocess, sys\nfrom pathlib import Path\n'
            'child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])\n'
            f'Path({str(pid_file)!r}).write_text(str(child.pid))\n{ending}')
    result = await manager.execute(_authority(scope, _dispatch), code, cwd=tmp_path, timeout=0.5)
    assert result['kernel']['ended'] and not manager._entries
    child_pid = int(pid_file.read_text())
    assert not psutil.pid_exists(child_pid) or psutil.Process(child_pid).status() == psutil.STATUS_ZOMBIE


def test_stale_staging_cleanup_does_not_follow_links_or_remove_live_paths(tmp_path):
    """七天清理只处理固定布局中的过期路径，不跟随链接或删除当前内核。"""
    parent = tmp_path / 'users' / 'alice'
    parent.mkdir(parents=True)
    stale, active = parent / 'kernel-stale', parent / 'kernel-active'
    stale.mkdir()
    active.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    link = parent / 'kernel-link'
    link.symlink_to(outside, target_is_directory=True)
    before = time.time() - STAGING_RETENTION_SECONDS - 1
    for path in (stale, active, outside):
        os.utime(path, (before, before))
    sweep_stale(tmp_path, {active})
    assert not stale.exists() and active.exists() and outside.exists() and link.is_symlink()


@pytest.mark.asyncio
async def test_other_user_output_cannot_be_read_as_ordinary_agent(manager, tmp_path, monkeypatch):
    """普通用户不能借通用文件工具读取其他用户的 Python 输出缓存。"""
    monkeypatch.setattr(base, 'get_runtime_setting', lambda _key: tmp_path)
    alice = ReadFileTool(session_id='read', user_id='alice')
    alice.set_agent_context({'is_admin': False})
    other = tmp_path / 'agent' / 'runtime' / 'code' / 'users' / build_user_memory_key('bob') / 'output' / 'stdout-test.txt'
    _, error = await alice._check_local_file_access(str(other), operation='读取')
    assert error and '其他用户' in error


@pytest.mark.asyncio
async def test_unavailable_rpc_is_visible_even_when_script_ignores_it(manager, tmp_path):
    """底层 allowlist 拒绝也进入当前 cell 的失败摘要，脚本忽略返回值不能掩盖。"""
    scope = TerminalScope('alice', 'unknown', 'interactive')
    result = await manager.execute(_authority(scope, _dispatch),
                                   'from moviepilot_tools import _call\n_call("write_file", {})', cwd=tmp_path)
    assert result['exit_code'] == 0 and not result['success']
    assert result['tool_calls_made'] == 0 and result['tool_errors'][0]['tool'] == 'write_file'


@pytest.mark.asyncio
async def test_cell_end_cancels_lingering_rpc_before_reusing_kernel(manager, tmp_path):
    """Python 后台线程不能延长已结束 cell 的调用窗口，遗留调用被取消并公开未完成。"""
    scope = TerminalScope('alice', 'lingering', 'interactive')
    ready = tmp_path / 'ready'
    canceled = asyncio.Event()

    async def dispatch(_tool, _arguments):
        """通知脚本真实 RPC 已经进入宿主，并模拟尚未结束的只读查询。"""
        ready.touch()
        try:
            await asyncio.Event().wait()
        finally:
            canceled.set()

    result = await manager.execute(_authority(scope, dispatch),
        'import threading, time\nfrom pathlib import Path\nfrom moviepilot_tools import moviepilot_api\n'
        'threading.Thread(target=lambda: moviepilot_api(operation_id="subscription.list"), daemon=True).start()\n'
        f'while not Path({str(ready)!r}).exists():\n    time.sleep(0.005)\nprint("cell ended")', cwd=tmp_path, timeout=5)
    assert canceled.is_set() and not result['success']
    assert '未完成' in result['tool_errors'][0]['error']
    next_result = await manager.execute(_authority(scope, _dispatch), 'print("fresh cell")', cwd=tmp_path)
    assert next_result['success'] and next_result['kernel']['reused']


@pytest.mark.asyncio
async def test_queued_cell_after_exit_keeps_new_process_registered(manager, tmp_path):
    """前一个 cell 退出时已有排队引用，后一个重建的进程仍必须被 owner 和回收器持有。"""
    scope = TerminalScope('alice', 'queued', 'interactive')
    entered, release = asyncio.Event(), asyncio.Event()

    async def dispatch(_tool, _arguments):
        """让第二次调用有机会完成准入并排队。"""
        entered.set()
        await release.wait()
        return {'success': True}

    first = get_task_registry().create(manager.execute(_authority(scope, dispatch),
        'from moviepilot_tools import moviepilot_api\nmoviepilot_api("subscription.list")\nraise SystemExit(0)',
        cwd=tmp_path), owner='test.code.first')
    await asyncio.wait_for(entered.wait(), 5)
    second = get_task_registry().create(manager.execute(_authority(scope, _dispatch), 'print("new kernel")',
                                                       cwd=tmp_path), owner='test.code.second')
    try:
        async with asyncio.timeout(5):
            while len(next(iter(manager._entries.values())).attached) != 2:
                await asyncio.sleep(0.001)
        release.set()
        results = await asyncio.gather(first, second)
        assert results[0]['kernel']['ended']
        assert results[1]['output'] == 'new kernel\n' and results[1]['kernel']['state_reset']
        assert len(manager._entries) == 1
        process = next(iter(manager._entries.values())).kernel.process
        assert process.returncode is None
        assert await manager.close_owner(scope)
        assert process.returncode is not None
    finally:
        release.set()
        for task in (first, second):
            if not task.done():
                task.cancel()
        await asyncio.gather(first, second, return_exceptions=True)


@pytest.mark.asyncio
async def test_long_lived_tasks_do_not_retain_first_request_context(manager, tmp_path):
    """持久 socket、管道读取和进程回收任务不能保存首轮历史上下文。"""
    scope = TerminalScope('alice', 'context', 'interactive')
    history = contextvars.ContextVar('test_large_history')
    token = history.set(object())
    try:
        result = await manager.execute(_authority(scope, _dispatch), 'print("ready")', cwd=tmp_path)
        assert result['success']
        records = [record for record in get_task_registry().records if record.owner.startswith('agent.code.')]
        assert records and all(record.task.get_context().get(history) is None for record in records)
    finally:
        history.reset(token)


@pytest.mark.asyncio
async def test_cancel_during_admission_releases_queued_reference(manager, tmp_path, monkeypatch):
    """准入期间等待旧内核回收时被取消，也不能留下永久占用的排队引用。"""
    scope = TerminalScope('alice', 'admission', 'interactive')
    entered = asyncio.Event()
    original = manager._sweep_locked

    async def sweep():
        """只在新 entry 已登记后的容量清理边界暂停。"""
        if manager._entries:
            entered.set()
            await asyncio.Event().wait()
        await original()

    monkeypatch.setattr(manager, '_sweep_locked', sweep)
    task = get_task_registry().create(manager.execute(_authority(scope, _dispatch), 'print(1)', cwd=tmp_path),
                                     owner='test.code.admission')
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert manager._entries and manager._reaper is not None
    assert all(not entry.attached and entry.kernel.process is None for entry in manager._entries.values())
    assert await manager.close_owner(scope)
    assert not manager._entries
