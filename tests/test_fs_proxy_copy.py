"""
文件系统代理的流式复制测试。

复制大文件可能持续几小时，固定超时无法区分「正常但慢」和「已经挂死」。
worker 每秒上报一次进度作为心跳，父进程判定的是**两次上报之间的间隔**：
超过 stall 阈值收不到任何一行，才认定挂载无响应并强杀 worker。

这些测试固定的不变量：内容与时间戳正确、进度可回调、慢传输不被误杀、
挂死能被判定并回收、取消可即时生效。
"""
import os
import time
from pathlib import Path

import pytest

from app.modules.filemanager.fsproxy import FileSystemProxy, FileSystemTimeout


@pytest.fixture
def proxy():
    """
    每个用例一个独立代理，用完回收进程。
    """
    instance = FileSystemProxy(timeout=30, stall_timeout=30)
    yield instance
    instance.close()


def test_copy_preserves_content_and_mtime(tmp_path, proxy):
    """
    复制结果的内容与修改时间必须与源一致。

    时间戳要保留但权限不能覆盖：目标目录的默认权限与继承 ACL 是媒体库的访问
    策略，用源文件权限盖掉会清除已继承的 ACL。
    """
    src = tmp_path / "Movie.2024.mkv"
    src.write_bytes(b"payload" * 5000)
    old = time.time() - 86400
    os.utime(src, (old, old))
    dst = tmp_path / "out.mkv"
    size = src.stat().st_size

    assert proxy.copy(src, dst) == {"copied": size, "total": size}

    assert dst.read_bytes() == src.read_bytes()
    assert dst.stat().st_mtime == pytest.approx(src.stat().st_mtime, abs=1)


def test_copy_empty_file(tmp_path, proxy):
    """
    空文件不能因为没有任何数据块就走进异常分支。
    """
    src = tmp_path / "empty.mkv"
    src.write_bytes(b"")
    dst = tmp_path / "out.mkv"

    assert proxy.copy(src, dst)["total"] == 0
    assert dst.exists()


def test_copy_reports_progress(tmp_path, proxy):
    """
    进度必须被回调出来，且单调递增、不超过 100。
    """
    src = tmp_path / "big.mkv"
    src.write_bytes(b"x" * (3 * 1024 * 1024))
    dst = tmp_path / "out.mkv"

    seen = []
    # 用小 chunk 让传输持续足够久，确保能观察到进度上报
    assert proxy.copy(src, dst, progress_cb=seen.append, chunk_size=4096)

    assert seen == sorted(seen), "进度不是单调递增的"
    assert all(0 <= value <= 100 for value in seen)


def test_copy_missing_source_raises_file_not_found(tmp_path, proxy):
    """
    源文件不存在要还原成 FileNotFoundError，调用方的既有分支才能生效。
    """
    with pytest.raises(FileNotFoundError):
        proxy.copy(tmp_path / "nope.mkv", tmp_path / "out.mkv")


def test_slow_transfer_is_not_killed(tmp_path):
    """
    关键区分之一：传输很慢但仍在推进时，绝不能被判定为挂死。

    stall 阈值取得远小于可能的总耗时——只要心跳不断，就必须一路跑完。
    """
    src = tmp_path / "slow.mkv"
    src.write_bytes(b"x" * (2 * 1024 * 1024))
    dst = tmp_path / "out.mkv"

    proxy = FileSystemProxy(timeout=30, stall_timeout=3)
    try:
        assert proxy.copy(src, dst, chunk_size=8192)
        assert dst.read_bytes() == src.read_bytes()
    finally:
        proxy.close()


def test_stalled_transfer_is_detected_and_killed(tmp_path, monkeypatch):
    """
    关键区分之二：传输彻底不推进时必须被判定并强杀，而不是永久等待。
    """
    import app.modules.filemanager.fsproxy as fsproxy_module

    # worker 替身：读到请求后完全不响应，模拟卡死在挂载上的传输
    stuck = tmp_path / "stuck_worker.py"
    stuck.write_text(
        "import sys, time\nsys.stdin.readline()\nwhile True:\n    time.sleep(60)\n",
        encoding="utf-8"
    )
    monkeypatch.setattr(fsproxy_module, "_WORKER_PATH", stuck)

    proxy = FileSystemProxy(timeout=30, stall_timeout=0.5)
    try:
        started = time.monotonic()
        with pytest.raises(FileSystemTimeout):
            proxy.copy(tmp_path / "a.mkv", tmp_path / "b.mkv")
        assert time.monotonic() - started < 15, "挂死的传输没有被及时放弃"
        assert proxy._process is None, "冻住的代理进程没有被回收"
    finally:
        proxy.close()


def test_copy_can_be_cancelled_midway(tmp_path):
    """
    取消要即时生效：父进程收到进度就检查取消标记，命中即杀掉 worker 中断传输，
    不必等它把整个文件读完。
    """
    src = tmp_path / "big.mkv"
    src.write_bytes(b"x" * (4 * 1024 * 1024))
    dst = tmp_path / "out.mkv"

    proxy = FileSystemProxy(timeout=30, stall_timeout=30)
    try:
        assert proxy.copy(src, dst, cancel_cb=lambda: True, chunk_size=4096) is False
        # 取消后代理已被回收，下一次调用会重启一个干净的 worker
        assert proxy._process is None
    finally:
        proxy.close()


def test_copy_falls_back_to_direct_when_disabled(tmp_path, monkeypatch):
    """
    代理关闭时复制退回进程内直接执行，行为与引入代理之前一致。
    """
    import app.modules.filemanager.fsproxy as fsproxy_module

    monkeypatch.setattr(fsproxy_module.settings, "FS_PROXY_ENABLED", False, raising=False)
    src = tmp_path / "a.mkv"
    src.write_bytes(b"direct" * 100)
    dst = tmp_path / "b.mkv"

    proxy = FileSystemProxy()
    try:
        assert proxy.copy(src, dst) is True
        assert dst.read_bytes() == src.read_bytes()
        # 关闭时不应启动任何子进程
        assert proxy._process is None
    finally:
        proxy.close()


def test_direct_copy_honours_cancel(tmp_path, monkeypatch):
    """
    关闭代理时取消同样要生效，否则关掉开关就丢了取消能力。
    """
    import app.modules.filemanager.fsproxy as fsproxy_module

    monkeypatch.setattr(fsproxy_module.settings, "FS_PROXY_ENABLED", False, raising=False)
    src = tmp_path / "a.mkv"
    src.write_bytes(b"x" * 8192)
    dst = tmp_path / "b.mkv"

    proxy = FileSystemProxy()
    try:
        assert proxy.copy(src, dst, cancel_cb=lambda: True, chunk_size=1024) is False
    finally:
        proxy.close()


def test_worker_still_standalone_after_streaming_support():
    """
    加了流式协议之后 worker 仍须只依赖标准库——一旦引入 app 导入链，
    强杀后的重启成本会从毫秒级涨到秒级，整个代理方案就不成立了。
    """
    worker = Path("app/modules/filemanager/fsworker.py").read_text(encoding="utf-8")

    assert "from app." not in worker
    assert "import app" not in worker
