"""Feedback Issue 日志压缩和 Doctor 摘要聚合测试。"""

import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest


SCRIPT_DIR = Path(__file__).parents[1] / "skills" / "feedback-issue" / "scripts"


def _load_module(name: str, path: Path):
    """从脚本路径加载测试模块。"""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def feedback_modules():
    """加载 feedback 脚本，并在测试后恢复进程模块和搜索路径。"""
    old_path = list(sys.path)
    module_names = ["feedback_issue_common", "feedback_issue_collect_quality_test"]
    old_modules = {name: sys.modules.get(name) for name in module_names}
    try:
        sys.path.insert(0, str(SCRIPT_DIR))
        common = _load_module(
            "feedback_issue_common",
            SCRIPT_DIR / "feedback_issue_common.py",
        )
        collect = _load_module(
            "feedback_issue_collect_quality_test",
            SCRIPT_DIR / "collect_feedback_diagnostics.py",
        )
        yield common, collect
    finally:
        sys.path[:] = old_path
        for name, module in old_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def test_filter_lines_compacts_consecutive_repeated_templates(feedback_modules):
    """关键词命中的连续轮询日志应压缩为首条、计数和末条。"""
    _, collect = feedback_modules
    now = datetime.now()
    lines = [
        (
            f"【INFO】{(now - timedelta(seconds=20 - index)).strftime('%Y-%m-%d %H:%M:%S')},000 "
            f"- transfer.py - 等待转存任务完成：{index}/20"
        )
        for index in range(1, 11)
    ]

    filtered, matched_keywords = collect.filter_lines(
        "\n".join(lines),
        keywords=["等待转存"],
        max_lines=80,
        window_start=now - timedelta(minutes=5),
    )

    assert len(filtered) == 3
    assert "1/20" in filtered[0]
    assert "连续重复 10 次" in filtered[1]
    assert "10/20" in filtered[2]
    assert matched_keywords == ["等待转存"]


def test_doctor_summary_groups_legacy_duplicate_advisories(feedback_modules):
    """旧版 Doctor 的重复插件发现也应在反馈摘要中合并展示。"""
    common, _ = feedback_modules
    findings = [
        {
            "severity": "warn",
            "title": "最近日志存在插件异常",
            "recommendation": "检查插件配置。",
            "affects_report_status": False,
            "context": {
                "log_file": f"/config/logs/plugins/plugin-{index}.log",
                "matches": 2,
            },
        }
        for index in range(5)
    ]
    summary = common.format_doctor_summary({
        "success": True,
        "report": {
            "status": "healthy",
            "summary": {
                "total": 5,
                "error": 0,
                "warn": 5,
                "fixed": 0,
            },
            "findings": findings,
        },
    })

    assert summary.count("最近日志存在插件异常") == 1
    assert "advisory=5" in summary
    assert "warn/advisory" in summary
    assert "合并 5 项" in summary
    assert "plugin-0.log" in summary
    assert "命中：10 条" in summary
