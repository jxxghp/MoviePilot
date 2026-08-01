"""Agent 文件写入工具的共享辅助函数。"""

import hashlib
import os
import tempfile
from pathlib import Path


class FileVersionConflictError(RuntimeError):
    """目标文件在准备写入期间发生变化。"""


def calculate_file_sha256(path: Path) -> str:
    """计算文件原始字节的 SHA-256，用于检测陈旧写入。"""
    digest = hashlib.sha256()
    with path.open("rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_text(
    path: Path,
    content: str,
    expected_sha256: str | None = None,
) -> None:
    """校验目标版本后，在同目录写入临时文件并原子替换文本。"""
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
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()
