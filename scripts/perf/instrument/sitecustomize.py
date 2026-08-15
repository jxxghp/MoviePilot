"""MoviePilot Docker A/B 测量时使用的最小 ``sys.modules`` 快照探针。"""

import os
import signal
import sys


_OUTPUT_DIR = os.environ.get("MP_PERF_OUTPUT_DIR")
_snapshot_index = 0


def _dump_modules(_signum, _frame) -> None:
    """收到 SIGUSR1 时原子写出当前解释器已经导入的模块名称。"""
    global _snapshot_index
    if not _OUTPUT_DIR:
        return

    _snapshot_index += 1
    os.makedirs(_OUTPUT_DIR, exist_ok=True)
    final_path = os.path.join(
        _OUTPUT_DIR,
        f"modules-{os.getpid()}-{_snapshot_index}.txt",
    )
    temporary_path = f"{final_path}.tmp"
    with open(temporary_path, "w", encoding="utf-8") as output:
        for module_name in sorted(sys.modules):
            output.write(module_name)
            output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary_path, final_path)


if _OUTPUT_DIR and hasattr(signal, "SIGUSR1"):
    signal.signal(signal.SIGUSR1, _dump_modules)
