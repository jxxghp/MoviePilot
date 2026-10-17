"""
标准输出滚动日志流的边界行为测试。
"""

from __future__ import annotations

import sys
from pathlib import Path

from app.adapters.system import stdio


def test_rotating_line_stream_buffers_lines_and_flushes_partial_message(
    tmp_path: Path,
) -> None:
    """
    滚动日志流应按行写入，并在 flush 时落下未结束的半行消息。
    """
    log_file = tmp_path / "logs" / "stdout.log"
    log_file.parent.mkdir()
    stream = stdio.RotatingLineStream(log_file, max_bytes=1024, backup_count=1)
    try:
        assert stream.writable() is True
        assert stream.isatty() is False
        assert stream.write("") == 0
        assert stream.write("first\r\nsecond\npartial") == len("first\r\nsecond\npartial")
        stream.flush()
        assert log_file.read_text(encoding="utf-8").splitlines() == [
            "first",
            "second",
            "partial",
        ]
    finally:
        for handler in stream._logger.handlers:
            handler.close()
            stream._logger.removeHandler(handler)


def test_configure_rotating_stdio_creates_parent_and_replaces_process_streams(
    tmp_path: Path,
) -> None:
    """
    配置入口应创建父目录并同时替换 stdout 与 stderr。
    """
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    stream = stdio.configure_rotating_stdio(
        log_file=tmp_path / "nested" / "stdout.log",
        max_bytes=1024,
        backup_count=1,
    )
    try:
        assert sys.stdout is stream
        assert sys.stderr is stream
        assert stream._logger.handlers
    finally:
        sys.stdout = original_stdout
        sys.stderr = original_stderr
        for handler in stream._logger.handlers:
            handler.close()
            stream._logger.removeHandler(handler)
