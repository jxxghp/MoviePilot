"""
测试系统引用链接（reflink / 写时复制）功能的跨平台实现与异常处理。
"""

import errno
import os
import shutil
import sys
import warnings
from pathlib import Path
from unittest.mock import MagicMock

import psutil
import pytest

from app.adapters.system.host import SystemUtils


def _get_filesystem_type(path: Path) -> str:
    """
    获取指定路径所在挂载点文件系统的格式类型。

    :param path: 目标路径
    :return: 小写文件系统名称（例如 apfs, btrfs, xfs, ext4）
    """
    resolved = path.resolve()
    best_mount = ""
    best_fstype = ""
    try:
        for part in psutil.disk_partitions(all=True):
            try:
                mount = Path(part.mountpoint).resolve()
                if resolved == mount or mount in resolved.parents:
                    if len(str(mount)) > len(best_mount):
                        best_mount = str(mount)
                        best_fstype = (part.fstype or "").lower()
            except Exception:
                continue
    except Exception:
        pass
    if best_fstype:
        return best_fstype

    if sys.platform.startswith("linux"):
        try:
            with open("/proc/mounts", "r", encoding="utf-8") as stream:
                for line in stream:
                    parts = line.split()
                    if len(parts) >= 3:
                        mount = Path(parts[1]).resolve()
                        if resolved == mount or mount in resolved.parents:
                            if len(str(mount)) > len(best_mount):
                                best_mount = str(mount)
                                best_fstype = parts[2].lower()
        except Exception:
            pass
    if best_fstype:
        return best_fstype

    if sys.platform == "darwin":
        return "apfs"
    return "unknown"


def _probe_reflink_support(path: Path) -> bool:
    """
    真实探测文件系统是否支持写时复制。

    :param path: 探测临时目录
    :return: 支持写时复制时为 True
    """
    src = path / ".probe_reflink_src"
    dest = path / ".probe_reflink_dest"
    try:
        src.write_bytes(b"probe")
        code, _ = SystemUtils.reflink(src, dest)
        return code == 0
    except Exception:
        return False
    finally:
        src.unlink(missing_ok=True)
        dest.unlink(missing_ok=True)


def _require_reflink_filesystem(tmp_path: Path) -> str:
    """
    检查测试目录文件系统格式，非写时复制格式发出警告并跳过测试。

    :param tmp_path: pytest 临时测试目录
    :return: 当前被确认支持写时复制的文件系统格式
    """
    fstype = _get_filesystem_type(tmp_path)
    supported_types = {"apfs", "btrfs", "xfs", "zfs"}
    if fstype not in supported_types or not _probe_reflink_support(tmp_path):
        warning_message = (
            f"当前测试目录所在文件系统格式为 [{fstype}]，"
            f"非写时复制格式（需为 {supported_types} 且启用 CoW），已跳过对应引用链接测试"
        )
        warnings.warn(warning_message, UserWarning, stacklevel=2)
        pytest.skip(warning_message)
    return fstype



def test_reflink_success_on_current_platform(tmp_path: Path):
    """
    测试在当前支持引用链接的系统（如 macOS APFS）上成功创建 reflink，
    验证内容一致、不同 Inode、写时复制独立修改以及文件大小一致。
    """
    _require_reflink_filesystem(tmp_path)

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


def test_reflink_cleans_preexisting_temp_file(tmp_path: Path):
    """
    测试当目标临时 .mp 文件预先存在时，reflink 能先清理临时文件并成功完成克隆。
    """
    _require_reflink_filesystem(tmp_path)

    src = tmp_path / "source.txt"
    dest = tmp_path / "target.txt"
    temp_file = dest.with_suffix(dest.suffix + ".mp")

    src.write_text("source content", encoding="utf-8")
    temp_file.write_text("old stale temp content", encoding="utf-8")

    code, msg = SystemUtils.reflink(src, dest)
    assert code == 0
    assert msg == ""
    assert dest.read_text(encoding="utf-8") == "source content"
    assert not temp_file.exists()


def test_reflink_copy_failure_cleans_temp_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """
    测试当底层 _reflink_copy 失败时，返回错误信息并清理残留的临时 .mp 文件。
    """
    src = tmp_path / "source.txt"
    dest = tmp_path / "target.txt"
    temp_file = dest.with_suffix(dest.suffix + ".mp")
    src.write_text("source data", encoding="utf-8")

    def _mock_reflink_copy(_src: Path, _dest: Path):
        # 模拟底层复制过程中生成了临时文件但最终报告失败
        _dest.write_text("partial data", encoding="utf-8")
        return -1, "底层克隆失败模拟错误"

    monkeypatch.setattr(SystemUtils, "_reflink_copy", _mock_reflink_copy)

    code, msg = SystemUtils.reflink(src, dest)
    assert code == -1
    assert "底层克隆失败模拟错误" in msg
    # 验证临时文件已被清理且目标未生成
    assert not temp_file.exists()
    assert not dest.exists()


def test_reflink_source_file_not_found(tmp_path: Path):
    """
    测试当源文件不存在时调用 reflink，返回失败状态码且不遗留临时或目标文件。
    """
    src = tmp_path / "nonexistent_source.txt"
    dest = tmp_path / "target.txt"
    temp_file = dest.with_suffix(dest.suffix + ".mp")

    code, msg = SystemUtils.reflink(src, dest)
    assert code != 0
    assert msg != ""
    assert not temp_file.exists()
    assert not dest.exists()


def test_reflink_unexpected_exception_handling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """
    测试当执行过程中发生未捕获异常（如 shutil.move 失败）时，妥善清理临时文件并返回错误。
    """
    src = tmp_path / "source.txt"
    dest = tmp_path / "target.txt"
    temp_file = dest.with_suffix(dest.suffix + ".mp")
    src.write_text("source data", encoding="utf-8")

    def _mock_reflink_copy(_src: Path, _dest: Path):
        _dest.write_text("cloned data", encoding="utf-8")
        return 0, ""

    def _mock_move(_src, _dest):
        raise RuntimeError("模拟 shutil.move 移动文件异常")

    monkeypatch.setattr(SystemUtils, "_reflink_copy", _mock_reflink_copy)
    monkeypatch.setattr(shutil, "move", _mock_move)

    code, msg = SystemUtils.reflink(src, dest)
    assert code == -1
    assert "模拟 shutil.move 移动文件异常" in msg
    assert not temp_file.exists()
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
