"""
测试系统引用链接（reflink / 写时复制）功能的跨平台实现与异常处理。
"""

import errno
import os
import shutil
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.adapters.system.host import SystemUtils


def _reflink_available(directory: Path) -> bool:
    """
    探测目录所在文件系统是否支持底层引用链接。

    通过真实克隆一对探测文件判定能力，避免把平台判断当成文件系统判断。

    :param directory: 用于探测的临时目录
    :return: 底层克隆成功返回 True
    """
    probe_source = directory / ".reflink-probe.source"
    probe_target = directory / ".reflink-probe.target"
    probe_source.write_bytes(b"reflink probe")
    try:
        code, _ = SystemUtils.reflink(probe_source, probe_target)
        return code == 0
    finally:
        probe_source.unlink(missing_ok=True)
        probe_target.unlink(missing_ok=True)
        probe_target.with_suffix(probe_target.suffix + ".mp").unlink(missing_ok=True)


def test_reflink_success_on_current_platform(tmp_path: Path):
    """
    测试在当前支持引用链接的文件系统（如 macOS APFS、Linux Btrfs/XFS）上成功创建 reflink，
    验证内容一致、不同 Inode、写时复制独立修改以及文件大小一致。
    """
    if not _reflink_available(tmp_path):
        pytest.skip("当前文件系统不支持引用链接")

    src = tmp_path / "source.bin"
    dest = tmp_path / "cloned.bin"
    test_content = b"MoviePilot reflink test data: " + b"X" * 1024
    src.write_bytes(test_content)

    code, msg = SystemUtils.reflink(src, dest)
    assert code == 0
    assert msg == ""

    # 1. 验证目标文件存在且内容与源文件一致
    assert dest.exists()
    assert dest.read_bytes() == test_content

    # 2. 验证目标文件与源文件大小一致
    assert dest.stat().st_size == src.stat().st_size

    # 3. 验证不是同一个 Inode（不同于硬链接）
    assert not os.path.samefile(src, dest)

    # 4. 验证写时复制（CoW）特性：修改目标文件后源文件保持不变
    dest.write_bytes(b"Modified cloned content")
    assert src.read_bytes() == test_content
    assert dest.read_bytes() == b"Modified cloned content"


def test_reflink_preserves_unowned_legacy_temp_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """
    私有暂存不清理固定旧名称，避免删除其它任务仍在写入的文件。
    """
    src = tmp_path / "source.txt"
    dest = tmp_path / "target.txt"
    temp_file = dest.with_suffix(dest.suffix + ".mp")

    src.write_text("source content", encoding="utf-8")
    temp_file.write_text("old stale temp content", encoding="utf-8")

    def clone(source: Path, destination: Path):
        """
        在真实文件上回放底层克隆，避免依赖当前文件系统的 CoW 支持。
        """
        shutil.copyfile(source, destination)
        return 0, ""

    monkeypatch.setattr(SystemUtils, "_reflink_copy", clone)

    code, msg = SystemUtils.reflink(src, dest)
    assert code == 0
    assert msg == ""
    assert dest.read_text(encoding="utf-8") == "source content"
    assert temp_file.read_text(encoding="utf-8") == "old stale temp content"
    assert not list(tmp_path.glob(".target.txt.*.mp"))


def test_reflink_copy_failure_cleans_temp_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """
    测试当底层 _reflink_copy 失败时，返回错误信息并清理残留的临时 .mp 文件。
    """
    src = tmp_path / "source.txt"
    dest = tmp_path / "target.txt"
    temporary_files = []
    src.write_text("source data", encoding="utf-8")

    def _mock_reflink_copy(_src: Path, _dest: Path):
        """
        记录本次实际暂存路径后回放部分内容写入失败。
        """
        temporary_files.append(_dest)
        # 模拟底层复制过程中生成了临时文件但最终报告失败
        _dest.write_text("partial data", encoding="utf-8")
        return -1, "底层克隆失败模拟错误"

    monkeypatch.setattr(SystemUtils, "_reflink_copy", _mock_reflink_copy)

    code, msg = SystemUtils.reflink(src, dest)
    assert code == -1
    assert "底层克隆失败模拟错误" in msg
    # 验证临时文件已被清理且目标未生成
    assert temporary_files and all(not path.exists() for path in temporary_files)
    assert not dest.exists()


def test_reflink_source_file_not_found(tmp_path: Path):
    """
    测试当源文件不存在时调用 reflink，返回失败状态码且不遗留临时或目标文件。
    """
    src = tmp_path / "nonexistent_source.txt"
    dest = tmp_path / "target.txt"

    code, msg = SystemUtils.reflink(src, dest)
    assert code != 0
    assert msg != ""
    assert not list(tmp_path.glob(".target.txt.*.mp"))
    assert not dest.exists()


def test_reflink_unexpected_exception_handling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """
    测试当执行过程中发生未捕获异常（如 shutil.move 失败）时，妥善清理临时文件并返回错误。
    """
    src = tmp_path / "source.txt"
    dest = tmp_path / "target.txt"
    temporary_files = []
    src.write_text("source data", encoding="utf-8")

    def _mock_reflink_copy(_src: Path, _dest: Path):
        """
        记录实际克隆暂存路径供异常清理断言使用。
        """
        temporary_files.append(_dest)
        _dest.write_text("cloned data", encoding="utf-8")
        return 0, ""

    def _mock_move(_src, _dest):
        """
        模拟提升克隆文件失败。
        """
        raise RuntimeError("模拟 shutil.move 移动文件异常")

    monkeypatch.setattr(SystemUtils, "_reflink_copy", _mock_reflink_copy)
    monkeypatch.setattr(shutil, "move", _mock_move)

    code, msg = SystemUtils.reflink(src, dest)
    assert code == -1
    assert "模拟 shutil.move 移动文件异常" in msg
    assert temporary_files and all(not path.exists() for path in temporary_files)
    assert not dest.exists()


def test_reflink_copy_macos_clonefile_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """
    测试 macOS 环境下 libc.clonefile 返回错误时的处理逻辑。
    """
    monkeypatch.setattr(sys, "platform", "darwin")

    src = tmp_path / "src.txt"
    dest = tmp_path / "dest.txt"
    src.write_text("data", encoding="utf-8")

    mock_libc = MagicMock()
    mock_libc.clonefile.return_value = -1

    import app.adapters.system.host as host_module

    monkeypatch.setattr(host_module.ctypes, "CDLL", lambda *args, **kwargs: mock_libc)
    monkeypatch.setattr(host_module.ctypes, "get_errno", lambda: errno.EEXIST)

    code, msg = SystemUtils._reflink_copy(src, dest)
    assert code == -1
    assert "clonefile 失败" in msg
    assert os.strerror(errno.EEXIST) in msg


def test_reflink_copy_linux_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """
    测试 Linux 环境下使用 ioctl(FICLONE) 成功克隆文件，并验证同步了元数据。
    """
    monkeypatch.setattr(sys, "platform", "linux")

    src = tmp_path / "src.txt"
    dest = tmp_path / "dest.txt"
    src_content = b"linux reflink content"
    src.write_bytes(src_content)

    import app.adapters.system.host as host_module

    mock_fcntl = MagicMock()
    monkeypatch.setattr(host_module, "fcntl", mock_fcntl)

    copystat_called = []

    def _mock_copystat(s, d):
        copystat_called.append((s, d))

    monkeypatch.setattr(shutil, "copystat", _mock_copystat)

    code, msg = SystemUtils._reflink_copy(src, dest)
    assert code == 0
    assert msg == ""
    # 验证调用了 ioctl(..., 0x40049409, ...)
    mock_fcntl.ioctl.assert_called_once()
    assert mock_fcntl.ioctl.call_args[0][1] == 0x40049409
    # 验证调用了 copystat 同步元数据
    assert len(copystat_called) == 1
    assert copystat_called[0] == (src, dest)


def test_reflink_copy_linux_fcntl_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """
    测试 Linux 环境下当 fcntl 模块不可用时的错误处理。
    """
    monkeypatch.setattr(sys, "platform", "linux")

    src = tmp_path / "src.txt"
    dest = tmp_path / "dest.txt"
    src.write_text("data", encoding="utf-8")

    import app.adapters.system.host as host_module

    monkeypatch.setattr(host_module, "fcntl", None)

    code, msg = SystemUtils._reflink_copy(src, dest)
    assert code == -1
    assert msg == "fcntl 不可用"


def test_reflink_copy_linux_ioctl_oserror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """
    测试 Linux 环境下 ioctl(FICLONE) 抛出 OSError 时的错误处理。
    """
    monkeypatch.setattr(sys, "platform", "linux")

    src = tmp_path / "src.txt"
    dest = tmp_path / "dest.txt"
    src.write_text("data", encoding="utf-8")

    import app.adapters.system.host as host_module

    mock_fcntl = MagicMock()
    mock_fcntl.ioctl.side_effect = OSError("Invalid cross-device link")
    monkeypatch.setattr(host_module, "fcntl", mock_fcntl)

    code, msg = SystemUtils._reflink_copy(src, dest)
    assert code == -1
    assert "Linux ioctl(FICLONE) 失败: Invalid cross-device link" in msg


def test_reflink_copy_unsupported_platform(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """
    测试在不支持引用链接的操作系统（如 Windows）上的错误处理。
    """
    monkeypatch.setattr(sys, "platform", "win32")

    src = tmp_path / "src.txt"
    dest = tmp_path / "dest.txt"
    src.write_text("data", encoding="utf-8")

    code, msg = SystemUtils._reflink_copy(src, dest)
    assert code == -1
    assert "当前操作系统 (win32) 不支持引用链接 (reflink)" in msg
