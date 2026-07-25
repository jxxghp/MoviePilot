import os
from unittest.mock import patch

import pytest

from app.modules.filemanager.storages import local as local_storage_module


def _make_storage():
    """
    绕过 __init__（依赖数据库/配置），仅用于测试不依赖实例状态的方法
    """
    return object.__new__(local_storage_module.LocalStorage)


def test_copy_with_progress_preserves_mtime(tmp_path):
    """
    _copy_with_progress 应保留源文件的修改时间（用 os.utime 而不是 shutil.copystat）
    """
    src = tmp_path / "src.bin"
    dest = tmp_path / "dest.bin"
    src.write_bytes(b"hello world" * 1000)

    old_time = 1_000_000_000  # 2001-09-09，明显区别于当前时间
    os.utime(src, (old_time, old_time))

    storage = _make_storage()
    with patch.object(local_storage_module, "transfer_process", return_value=lambda *a, **k: None):
        result = storage._copy_with_progress(src, dest)

    assert result is True
    assert dest.read_bytes() == src.read_bytes()
    assert dest.stat().st_mtime == pytest.approx(old_time, abs=1)


def test_copy_with_progress_does_not_force_source_mode_onto_dest(tmp_path):
    """
    回归测试：_copy_with_progress 不应把源文件的权限位强制搬到目标文件上。

    这是本次修复的核心行为——之前用 shutil.copystat 会显式 chmod 目标文件，
    在支持 ACL 的文件系统上会连带清空目标文件继承自父目录的 ACL。
    目标文件的权限应由写入进程的 umask 决定（标准 cp 语义），而不是照抄源文件权限。
    """
    src = tmp_path / "src2.bin"
    dest = tmp_path / "dest2.bin"
    src.write_bytes(b"data")

    # 用一个哨兵文件确定当前 umask 下、新建文件本该有的默认权限，
    # 再让源文件权限刻意偏离这个值——不管当前测试环境 umask 是什么，
    # 都能保证"源文件权限"与"默认创建权限"不同，断言不依赖任何硬编码的
    # 具体权限数值，避免在限制性 umask 环境下产生误报。
    sentinel = tmp_path / "sentinel.bin"
    sentinel.write_bytes(b"")
    default_mode = sentinel.stat().st_mode & 0o777
    # 属主始终保留读权限（测试进程自己要能读源文件），只让它与默认权限
    # 不同，具体数值不依赖任何硬编码假设，避免限制性 umask 环境下误报
    source_mode = 0o600 if default_mode != 0o600 else 0o640
    src.chmod(source_mode)

    storage = _make_storage()
    with patch.object(local_storage_module, "transfer_process", return_value=lambda *a, **k: None):
        result = storage._copy_with_progress(src, dest)

    assert result is True
    dest_mode = dest.stat().st_mode & 0o777
    assert dest_mode == default_mode
    assert dest_mode != source_mode
