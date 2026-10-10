"""用真实本地 Python 子进程验证持久 cell、IPC、输出与回收边界。"""

import asyncio
import json
import os
import signal
import stat
import threading
from pathlib import Path

import psutil
import pytest
import pytest_asyncio

from app.adapters.system.code.kernel import PythonKernel
from app.adapters.system.code.output import SPILL_BYTES, STDOUT_BYTES, OutputBuffer, project_stdout
from app.adapters.system.code.process import child_environment
from app.runtime.tasks import get_task_registry


async def _dispatch(name, arguments):
    """只返回调用参数，不触发任何真实业务或网络。"""
    return {'name': name, 'value': arguments['value']}


@pytest_asyncio.fixture
async def kernel(tmp_path):
    """每个测试创建并可靠关闭真实内核，失败也不能把进程留给下一用例。"""
    instance = PythonKernel(tmp_path / 'kernel', ('moviepilot_api',), blocking=asyncio.to_thread)
    await instance.start()
    try:
        yield instance
    finally:
        await instance.close()


@pytest.mark.asyncio
async def test_python_state_imports_exceptions_and_fresh_rpc_context(kernel):
    """变量和导入跨 cell 保留，普通异常保留已赋值状态，每次 RPC 使用当前回调。"""
    result = await kernel.execute(
        'import math\nfrom moviepilot_tools import moviepilot_api\n'
        'total = math.factorial(5)\nprint(moviepilot_api(value=total))', dispatch=_dispatch, timeout=5,
    )
    assert result['status'] == 'ok'
    assert "'value': 120" in result['stdout']
    failed = await kernel.execute('total += 1\nraise ValueError("cell failure")', dispatch=_dispatch, timeout=5)
    assert failed['status'] == 'error' and 'ValueError: cell failure' in failed['traceback']

    async def current(name, arguments):
        """替换回调即可改变当前调用身份，不重启 Python 或复用旧连接上下文。"""
        return {'current': True, **await _dispatch(name, arguments)}

    recovered = await kernel.execute('print(moviepilot_api(value=total))', dispatch=current, timeout=5)
    assert recovered['execution_count'] == 3
    assert "'current': True" in recovered['stdout'] and "'value': 121" in recovered['stdout']


@pytest.mark.asyncio
async def test_multithreaded_rpc_does_not_mix_replies(kernel):
    """同一持久连接上的并发 Python 线程仍各自收到匹配的调用结果。"""
    result = await kernel.execute(
        'from concurrent.futures import ThreadPoolExecutor\n'
        'from moviepilot_tools import moviepilot_api\n'
        'with ThreadPoolExecutor(8) as pool:\n'
        '    print(list(pool.map(lambda n: moviepilot_api(value=n)["value"], range(30))))',
        dispatch=_dispatch, timeout=5,
    )
    assert result['status'] == 'ok'
    assert json.loads(result['stdout']) == list(range(30))


@pytest.mark.asyncio
async def test_fd_output_cannot_corrupt_frames_and_large_streams_are_drained(kernel):
    """直接 fd 输出与伪造帧文本不影响随机协议标记，大 stderr 不阻塞进程。"""
    result = await kernel.execute(
        'import os\nos.write(1, b"\\nFAKE_MARKER 999\\nraw output\\n")\n'
        'os.write(2, b"E" * 100000)\nprint("cell result")', dispatch=_dispatch, timeout=5,
    )
    assert result['stdout'] == 'cell result\n'
    assert 'FAKE_MARKER' in result['raw_stdout']
    assert result['raw_stderr_bytes'] == 100000 and len(result['raw_stderr']) == 10000
    next_result = await kernel.execute('print("next")', dispatch=_dispatch, timeout=5)
    assert next_result['raw_stdout'] == next_result['raw_stderr'] == ''


@pytest.mark.asyncio
async def test_token_allowlist_and_expired_cell_are_checked(kernel):
    """请求必须同时通过令牌、工具名和活动 cell 检查；旧派发引用不能跨 cell 使用。"""
    good = {'token': kernel._token, 'tool': 'moviepilot_api', 'args': {'value': 1}}
    assert 'No active' in (await kernel._request(json.dumps(good).encode(), None))['error']
    kernel._dispatch = _dispatch
    assert 'Unauthorized' in (await kernel._request(b'{"token":"wrong"}', _dispatch))['error']
    assert 'Malformed' in (await kernel._request(b'not json', _dispatch))['error']
    assert 'unavailable' in (await kernel._request(json.dumps({**good, 'tool': 'write_file'}).encode(), _dispatch))['error']
    assert (await kernel._request(json.dumps(good).encode(), _dispatch))['value'] == 1
    kernel._dispatch = None
    assert 'No active' in (await kernel._request(json.dumps(good).encode(), _dispatch))['error']


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel', [False, True])
async def test_interrupted_cell_destroys_process_and_tasks(kernel, cancel):
    """超时及宿主取消都等到进程结束并释放端点，不允许继续使用不完整的状态。"""
    task = get_task_registry().create(
        kernel.execute('import time\ntime.sleep(60)', dispatch=_dispatch, timeout=0.1 if not cancel else 30),
        owner='test.code.cell',
    )
    if cancel:
        await asyncio.sleep(0.05)
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
        await task
    assert kernel.closed and kernel.process.returncode is not None
    assert not kernel.directory.exists()
    assert not [record for record in get_task_registry().records if record.owner.startswith('agent.code.')]
    with pytest.raises(RuntimeError, match='unavailable'):
        await kernel.execute('print("must not run")', dispatch=_dispatch, timeout=5)


@pytest.mark.asyncio
async def test_cancellation_during_file_creation_cleans_late_work(tmp_path, monkeypatch):
    """线程创建不能被取消；宿主必须等创建真正结束，再回收迟到文件。"""
    instance = PythonKernel(tmp_path / 'late', (), blocking=asyncio.to_thread)
    entered, release = threading.Event(), threading.Event()
    original = instance._files

    def delayed():
        """在创建线程持有一个可由测试控制的有限等待点。"""
        entered.set()
        assert release.wait(5)
        original()

    monkeypatch.setattr(instance, '_files', delayed)
    task = get_task_registry().create(instance.start(), owner='test.code.start')
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not instance.directory.exists()
        assert instance.process is None and instance.server is None
    finally:
        release.set()
        await instance.close()


@pytest.mark.asyncio
async def test_private_files_and_parent_death_signal(kernel):
    """私有文件不可被其他系统用户读取，父死亡管道在正在执行时也能终止内核。"""
    if os.name == 'nt':
        pytest.skip('POSIX 文件权限和死亡管道；Windows 使用进程句柄')
    assert stat.S_IMODE(kernel.directory.stat().st_mode) == 0o700
    assert stat.S_IMODE((kernel.directory / 'moviepilot_tools.py').stat().st_mode) == 0o600
    assert stat.S_IMODE(Path(kernel.endpoint).stat().st_mode) == 0o600
    kernel.process.stdin.write(b'{"id":"death", "code":"import time; time.sleep(60)"}\n')
    await kernel.process.stdin.drain()
    kernel._watch.close()
    await asyncio.wait_for(kernel.process.wait(), 5)
    assert kernel.process.returncode == -signal.SIGKILL


def test_environment_preserves_deployment_configuration_without_mutating_parent():
    """管理员内核继承完整部署配置，但不继承宿主导入路径或改写父环境。"""
    parent = {'PATH': '/bin', 'HOME': '/home/example', 'HOME_TOKEN': 'example-token',
              'LANG': 'en_US.UTF-8', 'OPENAI_API_KEY': 'example-key', 'PYTHONPATH': '/host',
              'CONFIG_DIR': '/config', 'DB_TYPE': 'postgresql',
              'DB_POSTGRESQL_PASSWORD': 'example-password', 'CUSTOM_SERVICE_URL': 'http://localhost',
              'PYTHONUTF8': '0', 'SystemRoot': 'C:\\Windows'}
    original = parent.copy()
    environment = child_environment(parent)
    assert environment == {**{key: value for key, value in parent.items() if key != 'PYTHONPATH'},
                           'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONIOENCODING': 'utf-8', 'PYTHONUTF8': '1'}
    assert parent == original
    assert environment['PYTHONUTF8'] == '1'


@pytest.mark.asyncio
@pytest.mark.parametrize('database_type', ['postgresql', 'sqlite'])
async def test_kernel_and_nested_script_use_host_database_configuration(tmp_path, monkeypatch, database_type):
    """遗留 SQLite 存在时，内核和后续脚本仍按宿主配置选择数据库且不输出凭据。"""
    config_path = tmp_path / 'config'
    config_path.mkdir()
    (config_path / 'user.db').touch()
    project_path = Path(__file__).resolve().parents[1]
    deployment = {'CONFIG_DIR': str(config_path), 'MOVIEPILOT_ROOT': str(project_path),
                  'DB_TYPE': database_type, 'DB_POSTGRESQL_HOST': 'database.invalid',
                  'DB_POSTGRESQL_PORT': '15432', 'DB_POSTGRESQL_DATABASE': 'kernel_test',
                  'DB_POSTGRESQL_USERNAME': 'kernel_user', 'DB_POSTGRESQL_PASSWORD': 'test-pg-password'}
    for key, value in deployment.items():
        monkeypatch.setenv(key, value)
    instance = PythonKernel(tmp_path / 'deployment-kernel', (), blocking=asyncio.to_thread, cwd=project_path)
    script = (
        'import os, runpy\n'
        'from pathlib import Path\n'
        'from sqlalchemy.engine import make_url\n'
        'script = runpy.run_path(str(Path(os.environ["MOVIEPILOT_ROOT"]) / '
        '"skills/database-operation/scripts/mp-db.py"))\n'
        'settings = script["_load_settings"]()\n'
        'assert settings.DB_TYPE == os.environ["DB_TYPE"]\n'
        'assert settings.CONFIG_PATH == Path(os.environ["CONFIG_DIR"])\n'
        'assert settings.DB_POSTGRESQL_PASSWORD == os.environ["DB_POSTGRESQL_PASSWORD"]\n'
        'script["_build_engine"].__globals__["create_engine"] = lambda url, **kwargs: make_url(url)\n'
        'url = script["_build_engine"]()\n'
        'assert url.get_backend_name() == os.environ["DB_TYPE"]\n'
        'if settings.DB_TYPE == "postgresql":\n'
        '    assert url.host == "database.invalid" and url.port == 15432\n'
        '    assert url.database == "kernel_test" and url.username == "kernel_user"\n'
        '    assert url.password == os.environ["DB_POSTGRESQL_PASSWORD"]\n'
        'else:\n'
        '    assert Path(url.database) == settings.CONFIG_PATH / "user.db"\n'
        'print("database configuration matched")\n'
    )
    try:
        await instance.start()
        result = await instance.execute(script, dispatch=_dispatch, timeout=15)
        assert result['status'] == 'ok', result.get('traceback')
        assert result['stdout'] == 'database configuration matched\n'
        nested = await instance.execute(
            'import subprocess, sys\n'
            f'child = subprocess.run([sys.executable, "-c", {script!r}], '
            'capture_output=True, text=True, timeout=15)\n'
            'assert child.returncode == 0, child.stderr\nprint(child.stdout, end="")',
            dispatch=_dispatch, timeout=20,
        )
        assert nested['status'] == 'ok', nested.get('traceback')
        assert nested['stdout'] == 'database configuration matched\n'
    finally:
        await instance.close()


def test_bounded_output_preserves_readable_spill(tmp_path):
    """预览按字节预算保留首尾，正文可续读且超过5MB时明确报告上限。"""
    text = 'A' * STDOUT_BYTES + '中' * SPILL_BYTES + 'THE_END'
    preview, metadata = project_stdout(text, tmp_path)
    assert preview.startswith('AAAA') and preview.endswith('THE_END')
    assert metadata['stdout_truncated'] and metadata['stdout_spill_capped']
    assert Path(metadata['stdout_spill_path']).stat().st_size == SPILL_BYTES
    assert metadata['stdout_bytes_total'] == len(text.encode())
    buffer = OutputBuffer(10)
    buffer.append(b'x' * 100)
    assert buffer.drain() == ('x' * 10, 100)
    assert buffer.drain() == ('', 0)


@pytest.mark.asyncio
async def test_parent_death_also_retires_inherited_descendants(kernel):
    """父进程死亡不能留下继承输出管道的孙进程，使宿主一直等不到 EOF。"""
    if os.name == 'nt':
        pytest.skip('真实 POSIX 进程组；Windows 由 taskkill 树回收')
    result = await kernel.execute('import subprocess, sys\n'
                                  'child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])\n'
                                  'print(child.pid)', dispatch=_dispatch, timeout=5)
    child_pid = int(result['stdout'])
    kernel._watch.close()
    await asyncio.wait_for(kernel.process.wait(), 5)
    assert not psutil.pid_exists(child_pid) or psutil.Process(child_pid).status() == psutil.STATUS_ZOMBIE
