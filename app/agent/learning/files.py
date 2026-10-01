"""个人学习目录的有界文件事务；锁位于技能外，删除技能不会拆掉写入屏障。"""

import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator
from uuid import uuid4


def contained_path(root: Path, relative: str) -> Path:
    """拒绝绝对路径、父级跳转与路径内符号链接，模型不能扩大个人学习根。"""
    part = Path(relative)
    if not relative or part.is_absolute() or '..' in part.parts or '\\' in relative:
        raise ValueError('学习文件路径必须位于当前用户目录内')
    target = root / part
    if any(path.is_symlink() or path.is_junction() for path in [target, *target.parents] if path == root or root in path.parents):
        raise ValueError('学习文件不能经过符号链接')
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError('学习文件超出当前用户目录')
    return target


def read_text(path: Path, *, limit: int = 512 * 1024) -> str:
    """先限制实际读取字节数，拒绝将被截断的内容登记成完整写前读取。"""
    with path.open('rb') as handle:
        content = handle.read(limit + 1)
    if len(content) > limit:
        raise ValueError('学习文件超过读取预算')
    return content.decode('utf-8-sig')


def write_text(path: Path, content: str) -> None:
    """同目录临时文件落盘后原子替换，避免并发读者看到半份技能。"""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f'.{path.name}.{uuid4().hex}.tmp')
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _lock(handle: BinaryIO, *, release: bool = False) -> None:
    """使用平台文件锁跨进程串行化，不依赖技能内可被删除的文件。"""
    if sys.platform == 'win32':
        import msvcrt  # pylint: disable=import-outside-toplevel,import-error
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK if release else msvcrt.LK_NBLCK, 1)
    else:
        import fcntl  # pylint: disable=import-outside-toplevel
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN if release else fcntl.LOCK_EX | fcntl.LOCK_NB)


@contextmanager
def mutation_lock(root: Path) -> Iterator[None]:
    """个人库一次只执行一个批次，最多等候五秒；平台锁不可用时拒绝写入。"""
    path = contained_path(root, '.mutation.lock')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open('a+b') as handle:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b' ')
            handle.flush()
        deadline = time.monotonic() + 5
        while True:
            try:
                _lock(handle)
                break
            except (BlockingIOError, PermissionError):
                if time.monotonic() >= deadline:
                    raise TimeoutError('个人学习库正被其他任务更新') from None
                time.sleep(0.05)
        try:
            yield
        finally:
            _lock(handle, release=True)
