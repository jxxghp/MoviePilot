"""按字节预算保存 Python 输出，超长结果保留可继续读取的文件。"""

import hashlib
import os
import stat
from pathlib import Path
from typing import Any

STDOUT_BYTES = 50_000
STDERR_BYTES = 10_000
SPILL_BYTES = 5_000_000


class OutputBuffer:
    """持续排空进程管道，同时把宿主内存限制在固定字节预算内。"""

    def __init__(self, limit: int) -> None:
        """容量独立于子进程总输出量，保留总字节数以报告缺失。"""
        self.limit, self.total = limit, 0
        self.data = bytearray()

    def append(self, data: bytes) -> None:
        """持续消费超限分片，避免管道堵塞导致正常 cell 无法结束。"""
        self.total += len(data)
        self.data.extend(data[:max(0, self.limit - len(self.data))])

    def drain(self) -> tuple[str, int]:
        """归还本次捕获和总量，跨 cell 的输出不得累加为当前结果。"""
        text, total = self.data.decode('utf-8', errors='replace'), self.total
        self.data.clear()
        self.total = 0
        return text, total


def read_cell_stdout(payload: dict[str, Any], directory: Path) -> str:
    """只续读宿主确定名称的当前 cell spill，拒绝跟随子进程返回的任意路径或链接。"""
    text = str(payload.get('stdout') or '')
    if not payload.get('stdout_clipped') or not payload.get('stdout_spill_path'):
        return text
    path = directory / f"cell_{int(payload['execution_count']):06d}_stdout.txt"
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(descriptor, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('Python stdout spill must be a regular file')
        return stream.read(SPILL_BYTES).decode('utf-8', errors='ignore')


def save_stdout(raw: bytes, directory: Path) -> Path:
    """摘要文件仅复用内容一致的普通文件，既有链接或碰撞不能伪造当前回执。"""
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    path = directory / f'stdout-{hashlib.sha256(raw).hexdigest()}.txt'
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(descriptor, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode) or stream.read(len(raw) + 1) != raw:
                raise ValueError('Python stdout spill does not match its content digest')
    else:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(raw)
    return path


def project_stdout(text: str, directory: Path) -> tuple[str, dict[str, object]]:
    """50KB 内联按40%头/60%尾截取，最多5MB正文以摘要名称去重保存。"""
    raw = text.encode('utf-8', errors='replace')
    captured = min(len(raw), STDOUT_BYTES)
    metadata: dict[str, object] = {
        'stdout_truncated': len(raw) > captured, 'stdout_bytes_total': len(raw),
        'stdout_bytes_captured': captured, 'stdout_bytes_omitted': len(raw) - captured,
    }
    if len(raw) <= STDOUT_BYTES:
        return text, metadata
    kept = raw[:SPILL_BYTES]
    path = save_stdout(kept, directory)
    metadata.update(stdout_spill_path=str(path), stdout_spill_capped=len(raw) > SPILL_BYTES,
                    warning='输出已截断；从 stdout_spill_path 分页读取，不要重复执行代码。')
    head = STDOUT_BYTES * 2 // 5
    preview = (raw[:head].decode('utf-8', errors='replace')
               + f'\n[... omitted {len(raw) - captured} bytes ...]\n'
               + raw[-(STDOUT_BYTES - head):].decode('utf-8', errors='replace'))
    return preview, metadata
