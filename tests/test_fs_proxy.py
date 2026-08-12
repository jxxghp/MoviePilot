"""
本地文件系统操作代理测试。

代理存在的唯一理由：FUSE 挂载 block 型故障下，stat/listdir 这类调用永不返回，
而 Python 既不能中断已发出的系统调用、也不能强杀线程。放进子进程后，超时可以
真正 SIGKILL 回收——block 型故障被转换成各层已能处理的 crash 型故障。

这些测试固定三项不变量：语义与直接调用一致、超时可放弃、代理死亡后能自愈。
"""
import errno
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.modules.filemanager.fsproxy import FileSystemProxy, FileSystemTimeout


@pytest.fixture
def proxy():
    """
    每个用例一个独立代理，用完回收进程。
    """
    instance = FileSystemProxy(timeout=30)
    yield instance
    instance.close()


# --------------------------------------------------------------------------- #
# 语义等价：代理调用的结果必须与直接调用一致
# --------------------------------------------------------------------------- #

def test_stat_matches_direct_call(tmp_path, proxy):
    """
    stat 的结果要与直接调用一致，调用方才能无感替换。
    """
    media = tmp_path / "Movie.2024.mkv"
    media.write_bytes(b"x" * 42)

    result = proxy.stat(media)

    assert result["size"] == 42
    assert result["is_file"] is True
    assert result["is_dir"] is False
    assert result["mtime"] == pytest.approx(media.stat().st_mtime, abs=1)


def test_stat_on_directory(tmp_path, proxy):
    """
    目录的 stat 要正确标记类型。
    """
    result = proxy.stat(tmp_path)

    assert result["is_dir"] is True
    assert result["is_file"] is False


def test_stat_missing_raises_file_not_found(tmp_path, proxy):
    """
    errno 必须还原成具体的 OSError 子类，调用方的既有异常分支才能继续生效。
    """
    with pytest.raises(FileNotFoundError):
        proxy.stat(tmp_path / "nope.mkv")


def test_exists_true_and_false(tmp_path, proxy):
    """
    exists 的基本语义。
    """
    media = tmp_path / "a.mkv"
    media.write_bytes(b"x")

    assert proxy.exists(media) is True
    assert proxy.exists(tmp_path / "missing.mkv") is False


def test_listdir_matches_direct_call(tmp_path, proxy):
    """
    listdir 的结果要与直接调用一致。
    """
    for name in ("b.mkv", "a.mkv", "c.srt"):
        (tmp_path / name).write_bytes(b"x")

    assert proxy.listdir(tmp_path) == sorted(os.listdir(tmp_path))


def test_listdir_missing_raises_file_not_found(tmp_path, proxy):
    """
    目录不存在要抛 FileNotFoundError。
    """
    with pytest.raises(FileNotFoundError):
        proxy.listdir(tmp_path / "nodir")


def test_rename_within_same_storage(tmp_path, proxy):
    """
    同存储 rename 是第一版唯一放行的写操作：内核保证原子性，
    强杀后要么完全成功要么完全没发生，不需要恢复语义。
    """
    src = tmp_path / "old.mkv"
    dst = tmp_path / "new.mkv"
    src.write_bytes(b"data")

    assert proxy.rename(src, dst) is True
    assert dst.read_bytes() == b"data"
    assert not src.exists()


# --------------------------------------------------------------------------- #
# 核心价值：超时可放弃
# --------------------------------------------------------------------------- #

def test_timeout_raises_and_kills_worker(tmp_path, monkeypatch):
    """
    这是整个代理存在的意义：挂载不返回时，调用方必须在有限时间内拿到异常，
    而且冻住的进程要被真正杀掉——不能像线程那样永久悬挂。
    """
    import app.modules.filemanager.fsproxy as fsproxy_module

    # 用一个必定挂死的 worker 替身，模拟 stat 永不返回的挂载
    stuck_worker = tmp_path / "stuck_worker.py"
    stuck_worker.write_text("import time\nwhile True:\n    time.sleep(60)\n", encoding="utf-8")
    monkeypatch.setattr(fsproxy_module, "_WORKER_PATH", stuck_worker)

    proxy = FileSystemProxy(timeout=0.5)
    try:
        started = time.monotonic()
        with pytest.raises(FileSystemTimeout) as excinfo:
            proxy.stat(tmp_path / "whatever.mkv")
        elapsed = time.monotonic() - started

        assert elapsed < 10, "超时后没有及时放弃"
        assert excinfo.value.errno == errno.ETIMEDOUT
        # 冻住的代理必须已被回收，否则每次故障都会泄漏一个进程
        assert proxy._process is None
    finally:
        proxy.close()


def test_timeout_is_an_oserror():
    """
    超时必须是 OSError 子类：整理链与监控 watcher 对 OSError 已有完整的退避
    重试与登记逻辑，block 型故障经此转换后可直接复用，无需改动各层调用点。
    """
    assert issubclass(FileSystemTimeout, OSError)


def test_proxy_recovers_after_timeout(tmp_path, monkeypatch):
    """
    超时杀掉代理后，下一次调用要能用新代理正常工作——否则一次挂载抖动
    就会让文件操作永久不可用。
    """
    import app.modules.filemanager.fsproxy as fsproxy_module

    stuck_worker = tmp_path / "stuck_worker.py"
    stuck_worker.write_text("import time\nwhile True:\n    time.sleep(60)\n", encoding="utf-8")
    real_worker = fsproxy_module._WORKER_PATH

    proxy = FileSystemProxy(timeout=0.5)
    try:
        monkeypatch.setattr(fsproxy_module, "_WORKER_PATH", stuck_worker)
        with pytest.raises(FileSystemTimeout):
            proxy.stat(tmp_path)

        # 挂载恢复：换回真正的 worker，代理应自动重启并正常服务
        monkeypatch.setattr(fsproxy_module, "_WORKER_PATH", real_worker)
        media = tmp_path / "recovered.mkv"
        media.write_bytes(b"ok")
        assert proxy.stat(media)["size"] == 2
    finally:
        proxy.close()


def test_proxy_restarts_after_worker_dies(tmp_path, proxy):
    """
    代理进程被外部杀掉（OOM、误杀）后，下一次调用要能自动重启。
    """
    media = tmp_path / "a.mkv"
    media.write_bytes(b"x")
    assert proxy.stat(media)["size"] == 1

    # 模拟代理进程被外部杀死
    proxy._process.kill()
    proxy._process.wait(timeout=5)

    assert proxy.stat(media)["size"] == 1


def test_worker_does_not_import_app_package():
    """
    worker 必须只依赖标准库：一旦触发 app/__init__.py 的导入链，启动成本会从
    毫秒级涨到秒级，代理被强杀后的重启就不再可行。
    """
    worker = Path("app/modules/filemanager/fsworker.py").read_text(encoding="utf-8")

    assert "from app." not in worker
    assert "import app" not in worker


def test_worker_runs_standalone_without_app_on_path(tmp_path):
    """
    直接执行 worker 脚本必须成功，且不依赖项目根在 sys.path 上
    ——这是「绕开 app 导入链」这一设计前提的实证。
    """
    worker_path = Path("app/modules/filemanager/fsworker.py").resolve()
    media = tmp_path / "x.mkv"
    media.write_bytes(b"xyz")

    result = subprocess.run(
        [sys.executable, str(worker_path)],
        input=json.dumps({"op": "stat", "path": str(media)}) + "\n",
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(tmp_path),
    )

    response = json.loads(result.stdout.strip())
    assert response["ok"] is True
    assert response["result"]["size"] == 3
