"""
只读日志读取工具测试。
"""

import asyncio
import json
from pathlib import Path

import pytest

from app.agent.tools.impl import read_log as read_log_module
from app.agent.tools.impl.read_log import MAX_LOG_RESULT_CHARS, MAX_LOG_TAIL, ReadLogTool


def _record(level: str, message: str) -> str:
    """
    生成带级别前缀的日志记录行。
    """
    return f"【{level}】2026-10-30 10:00:00 [abc123] - {message}"


def _write_log(root: Path, relative: str, lines: list[str]) -> Path:
    """
    在日志根目录下写入测试日志文件。
    """
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture()
def log_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    将只读日志工具指向临时日志目录。
    """
    root = tmp_path / "logs"
    root.mkdir()
    monkeypatch.setattr(read_log_module, "get_runtime_setting", lambda *_args, **_kwargs: str(root))
    return root


def _run(logfile: str, **kwargs: object) -> dict:
    """
    以管理员身份执行日志工具并解析 JSON 结果。
    """
    tool = ReadLogTool(session_id="session-log", user_id="admin")
    tool.set_agent_context({"is_admin": True})
    return json.loads(asyncio.run(tool.run(logfile=logfile, **kwargs)))


def test_read_log_returns_recent_records(log_root: Path) -> None:
    """
    默认应返回最近日志记录并保留级别信息。
    """
    _write_log(log_root, "moviepilot.log", [
        _record("INFO", "ready"),
        _record("ERROR", "boom"),
        _record("WARNING", "slow"),
    ])

    payload = _run("moviepilot.log")

    assert payload["success"] is True
    assert payload["returned_records"] == 3
    assert "【ERROR】" in payload["content"]
    assert "【WARNING】" in payload["content"]


def test_read_log_filters_level_and_keyword(log_root: Path) -> None:
    """
    级别与关键字过滤应同时生效，且级别名不区分大小写。
    """
    _write_log(log_root, "moviepilot.log", [
        _record("ERROR", "request timeout"),
        _record("ERROR", "disk full"),
        _record("INFO", "request timeout"),
    ])

    payload = _run("moviepilot.log", level="error", keyword="TIMEOUT")

    assert payload["success"] is True
    assert payload["matched_records"] == 1
    assert "request timeout" in payload["content"]
    assert "disk full" not in payload["content"]


def test_read_log_keeps_traceback_with_parent_record(log_root: Path) -> None:
    """
    多行堆栈应归属上一条记录，不会在级别过滤时被拆散。
    """
    _write_log(log_root, "moviepilot.log", [
        _record("ERROR", "plugin crashed"),
        "Traceback (most recent call last):",
        '  File "plugin.py", line 1, in run',
        _record("INFO", "recovered"),
    ])

    payload = _run("moviepilot.log", level="ERROR")

    assert payload["returned_records"] == 1
    assert "Traceback (most recent call last)" in payload["content"]
    assert "recovered" not in payload["content"]


def test_read_log_reads_plugin_log_directory(log_root: Path) -> None:
    """
    插件日志位于日志目录的子目录中，也应能按相对路径读取。
    """
    _write_log(log_root, "plugins/demo.log", [_record("ERROR", "plugin failed")])

    payload = _run("plugins/demo.log")

    assert payload["success"] is True
    assert "plugin failed" in payload["content"]
    assert payload["logfile"] == "plugins/demo.log"


@pytest.mark.parametrize(
    "logfile",
    ["", "../outside.log", "moviepilot.txt", "missing.log"],
)
def test_read_log_rejects_invalid_targets(log_root: Path, tmp_path: Path, logfile: str) -> None:
    """
    空路径、越界路径、非 .log 文件和缺失文件都应返回结构化失败。
    """
    (tmp_path / "outside.log").write_text(_record("ERROR", "outside"), encoding="utf-8")
    _write_log(log_root, "moviepilot.log", [_record("INFO", "ready")])
    _write_log(log_root, "moviepilot.txt", [_record("INFO", "ready")])

    payload = _run(logfile)

    assert payload["success"] is False
    assert payload["message"]


def test_read_log_rejects_absolute_path(log_root: Path) -> None:
    """
    绝对路径即使在日志目录内部也不接受，调用方必须使用相对路径。
    """
    path = _write_log(log_root, "moviepilot.log", [_record("INFO", "ready")])

    payload = _run(str(path))

    assert payload["success"] is False
    assert "相对" in payload["message"]


def test_read_log_rejects_symlink_escape(log_root: Path, tmp_path: Path) -> None:
    """
    指向日志目录外的符号链接不能绕过路径校验。
    """
    outside = tmp_path / "secret.log"
    outside.write_text(_record("ERROR", "secret"), encoding="utf-8")
    (log_root / "linked.log").symlink_to(outside)

    payload = _run("linked.log")

    assert payload["success"] is False


def test_read_log_clamps_tail_and_rejects_non_positive(log_root: Path) -> None:
    """
    超过上限的 tail 会被收敛，非正数则直接拒绝。
    """
    _write_log(log_root, "moviepilot.log", [_record("INFO", f"line {index}") for index in range(3)])

    invalid = _run("moviepilot.log", tail=0)
    clamped = _run("moviepilot.log", tail=MAX_LOG_TAIL * 10)

    assert invalid["success"] is False
    assert clamped["success"] is True
    assert clamped["tail"] == MAX_LOG_TAIL


def test_read_log_truncates_oversized_record(log_root: Path) -> None:
    """
    单条记录超过输出上限时只保留尾部并标记截断。
    """
    _write_log(log_root, "moviepilot.log", [_record("ERROR", "x" * (MAX_LOG_RESULT_CHARS * 2))])

    payload = _run("moviepilot.log")

    assert payload["success"] is True
    assert payload["returned_records"] == 1
    assert payload["result_truncated"] is True
    assert len(payload["content"]) == MAX_LOG_RESULT_CHARS


def test_read_log_drops_partial_first_line_of_partial_scan(log_root: Path) -> None:
    """
    从文件中段开始扫描时应丢弃首行残片，避免返回不完整记录。
    """
    filler = "f" * 4096
    lines = [_record("INFO", f"{filler} {index}") for index in range(400)]
    _write_log(log_root, "moviepilot.log", lines)

    payload = _run("moviepilot.log", tail=1)

    assert payload["scan_truncated"] is True
    assert payload["content"].startswith("【INFO】")


def test_read_log_requires_admin() -> None:
    """
    日志可能包含敏感信息，工具必须保持管理员可用限制。
    """
    assert ReadLogTool.model_fields["require_admin"].default is True
