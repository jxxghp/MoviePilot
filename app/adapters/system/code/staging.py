"""持久内核暂存目录与过期输出文件的有界生命周期。"""

import shutil
import time
from pathlib import Path

STAGING_RETENTION_SECONDS = 7 * 86400


def sweep_stale(root: Path, active: set[Path]) -> None:
    """只清理固定目录布局中超过七天的残留，符号链接和当前活跃目录都跳过。"""
    cutoff = time.time() - STAGING_RETENTION_SECONDS
    for pattern, is_directory in (('users/*/kernel-*', True), ('users/*/output/stdout-*.txt', False)):
        for path in root.glob(pattern):
            try:
                if path in active or path.is_symlink() or path.stat().st_mtime >= cutoff:
                    continue
                if is_directory and path.is_dir():
                    shutil.rmtree(path)
                elif not is_directory and path.is_file():
                    path.unlink()
            except OSError:
                continue
