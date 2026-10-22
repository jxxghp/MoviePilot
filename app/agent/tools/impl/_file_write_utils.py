"""
Agent 文件写入工具的共享辅助函数。
"""

import hashlib
import os
import tempfile
import threading
from pathlib import Path
from weakref import WeakValueDictionary

_write_locks: WeakValueDictionary[str, threading.Lock] = WeakValueDictionary()
_write_locks_guard = threading.Lock()


def _write_lock(path: Path) -> threading.Lock:
    """
    为同一规范目标保留进程内互斥，闲置后自动回收路径锁。
    """
    key = os.path.normcase(str(path.resolve()))
    with _write_locks_guard:
        lock = _write_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _write_locks[key] = lock
        return lock


class FileVersionConflictError(RuntimeError):
    """
    目标文件在准备写入期间发生变化。
    """


def calculate_file_sha256(path: Path) -> str:
    """
    计算文件原始字节的 SHA-256，用于检测陈旧写入。
    """
    digest = hashlib.sha256()
    with path.open("rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_text(
    path: Path,
    content: str,
    expected_sha256: str | None = None,
    *,
    overwrite: bool = True,
) -> str:
    """
    在同目标写锁内校验版本并原子替换，返回本次提交内容的SHA256。

    :param path: 已通过权限检查的目标路径
    :param content: 本次完整文本内容
    :param expected_sha256: 当前版本摘要；None表示未提供版本前提
    :param overwrite: False使用原子排他创建，已有目标不得替换
    :return: 本次实际提交文本的SHA256，不重新读取可能被后续调用修改的文件
    :raises FileVersionConflictError: 版本变化或排他创建时目标已存在
    """
    with _write_lock(path):
        return _write_text_locked(path, content, expected_sha256, overwrite=overwrite)


def _write_text_locked(path: Path, content: str, expected_sha256: str | None, *, overwrite: bool) -> str:
    """
    在已持有目标锁时写入临时文件，禁止覆盖使用原子link拒绝已存在目标。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as file_handle:
            file_handle.write(content)
            file_handle.flush()
            os.fsync(file_handle.fileno())

        if expected_sha256:
            if (
                not path.is_file()
                or calculate_file_sha256(path).casefold()
                != expected_sha256.casefold()
            ):
                raise FileVersionConflictError(str(path))
        if path.exists():
            os.chmod(temp_path, path.stat().st_mode)
        if overwrite:
            os.replace(temp_path, path)
        else:
            try:
                os.link(temp_path, path)
            except FileExistsError as error:
                raise FileVersionConflictError(str(path)) from error
        return hashlib.sha256(content.encode("utf-8")).hexdigest()
    finally:
        if temp_path.exists():
            temp_path.unlink()
