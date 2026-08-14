"""feedback-issue 目标仓库路由测试。"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from app.runtime.config import settings


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "skills" / "feedback-issue" / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import feedback_issue_common as common  # noqa: E402
import prepare_feedback_issue as prepare_script  # noqa: E402
import submit_feedback_issue as submit_script  # noqa: E402


class _FakeResponse:
    """提交脚本使用的最小响应替身。"""

    def __init__(self, status_code: int, payload: dict | None = None):
        """保存响应状态码和 JSON 数据。"""
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = {}
        self.text = ""

    def json(self) -> dict:
        """返回预设 JSON 数据。"""
        return self._payload


@pytest.fixture
def isolated_feedback_runtime():
    """隔离 feedback-issue 脚本运行目录和 GitHub Token。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_backup = settings.CONFIG_DIR
        token_backup = settings.GITHUB_TOKEN
        settings.CONFIG_DIR = tmpdir
        settings.GITHUB_TOKEN = None
        settings.LOG_PATH.mkdir(parents=True, exist_ok=True)
        try:
            yield
        finally:
            settings.CONFIG_DIR = config_backup
            settings.GITHUB_TOKEN = token_backup


@pytest.fixture
def diagnostics_file(isolated_feedback_runtime) -> Path:
    """创建一份可复用的脚本诊断文件。"""
    file_path = common.runtime_file("diagnostics", ".json")
    common.write_json_file(
        file_path,
        {
            "original_user_request": "插件执行时报错，帮我提交 issue",
            "found": True,
            "logs": "ERROR plugin failed",
            "doctor": {
                "success": True,
                "report": {
                    "status": "ok",
                    "summary": {"total": 1, "error": 0, "warn": 0, "fixed": 0},
                    "environment": {"runtime": "Docker"},
                    "findings": [],
                },
            },
            "source_files": [str(settings.LOG_PATH / "plugins" / "demo.log")],
        },
    )
    return file_path


def _valid_plugin_draft(diagnostics_file: Path, target_repo: str = "owner/MoviePilot-Plugins") -> dict:
    """构造一份插件问题草稿。"""
    return {
        "title": "[错误报告]: 插件定时任务执行时报错退出",
        "version": "v2.12.2",
        "environment": "Docker",
        "issue_type": "插件问题",
        "target_repo": target_repo,
        "original_user_request": "插件执行时报错，帮我提交 issue",
        "diagnostics_file": str(diagnostics_file),
        "description": (
            "## 现象\n"
            "- DemoPlugin 的定时任务执行时报错退出。\n\n"
            "## 复现步骤\n"
            "1. 启用 DemoPlugin。\n"
            "2. 等待定时任务触发。\n"
            "3. 插件日志出现异常并停止执行。\n\n"
            "## 期望行为\n"
            "- 插件定时任务应正常执行，不影响主程序运行。\n\n"
            "## 已定位 / 推测\n"
            "- 仅插件日志出现异常，主程序 doctor 未发现错误。\n\n"
            "## 已尝试的处理\n"
            "- 重启插件后仍可复现。"
        ),
    }


def _valid_feature_draft(diagnostics_file: Path) -> dict:
    """构造一份功能请求草稿。"""
    return {
        "title": "[功能请求]: 支持按插件来源仓库批量筛选",
        "version": "v2.12.2",
        "environment": "Docker",
        "issue_type": "功能请求",
        "target_repo": common.FEEDBACK_REPO,
        "original_user_request": "希望支持按插件来源仓库批量筛选，帮我提功能请求",
        "diagnostics_file": str(diagnostics_file),
        "description": (
            "## 需求背景\n"
            "- 插件较多时，当前列表难以快速区分来源仓库。\n\n"
            "## 使用场景\n"
            "1. 管理员打开插件列表。\n"
            "2. 希望只查看某个来源仓库安装的插件。\n"
            "3. 需要快速定位同一仓库下的插件更新状态。\n\n"
            "## 期望能力\n"
            "- 支持按插件来源仓库筛选和批量查看插件。"
        ),
    }


def test_plugin_issue_requires_non_main_target_repo(diagnostics_file):
    """插件问题没有指定插件仓库时应拒绝，避免误投主仓库。"""
    draft = _valid_plugin_draft(diagnostics_file, target_repo=common.FEEDBACK_REPO)
    draft_file = common.runtime_file("draft", ".json")
    common.write_json_file(draft_file, draft)

    result = prepare_script.prepare_issue(draft_file)

    assert result["success"] is False
    assert result["reason"] == "invalid_draft"
    assert "插件所属 GitHub 仓库" in result["message"]


def test_plugin_prefill_url_targets_plugin_repository(diagnostics_file):
    """插件问题的手动预填链接应指向插件仓库。"""
    draft_file = common.runtime_file("draft", ".json")
    common.write_json_file(draft_file, _valid_plugin_draft(diagnostics_file))
    prepared = prepare_script.prepare_issue(draft_file)

    result = submit_script.submit_issue(prepared["payload_file"], username="admin")

    assert result["success"] is False
    assert result["reason"] == "no_token"
    assert result["repo"] == "owner/MoviePilot-Plugins"
    assert result["prefill_url"].startswith("https://github.com/owner/MoviePilot-Plugins/issues/new")


def test_plugin_api_submit_targets_plugin_repository(diagnostics_file):
    """自动提交插件问题时 GitHub API 应调用插件仓库地址。"""
    settings.GITHUB_TOKEN = "ghp_test_token"
    draft_file = common.runtime_file("draft", ".json")
    common.write_json_file(draft_file, _valid_plugin_draft(diagnostics_file))
    prepared = prepare_script.prepare_issue(draft_file)

    with patch(
        "submit_feedback_issue.RequestUtils.post",
        return_value=_FakeResponse(
            201,
            {
                "number": 12,
                "html_url": "https://github.com/owner/MoviePilot-Plugins/issues/12",
            },
        ),
    ) as post:
        result = submit_script.submit_issue(prepared["payload_file"], username="admin")

    assert result["success"] is True
    assert result["repo"] == "owner/MoviePilot-Plugins"
    assert post.call_args.args[0] == "https://api.github.com/repos/owner/MoviePilot-Plugins/issues"
    assert "labels" not in post.call_args.kwargs["json"]


def test_feature_request_uses_feature_label(diagnostics_file):
    """功能请求自动提交时应使用 feature request 标签而不是 bug。"""
    settings.GITHUB_TOKEN = "ghp_test_token"
    draft_file = common.runtime_file("draft", ".json")
    common.write_json_file(draft_file, _valid_feature_draft(diagnostics_file))
    prepared = prepare_script.prepare_issue(draft_file)

    with patch(
        "submit_feedback_issue.RequestUtils.post",
        return_value=_FakeResponse(
            201,
            {
                "number": 13,
                "html_url": "https://github.com/jxxghp/MoviePilot/issues/13",
            },
        ),
    ) as post:
        result = submit_script.submit_issue(prepared["payload_file"], username="admin")

    assert result["success"] is True
    assert post.call_args.kwargs["json"]["labels"] == ["feature request"]
