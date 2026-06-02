"""feedback-issue skill 内部脚本的单元测试。"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

from app.agent.tools.factory import MoviePilotToolFactory
from app.core.config import settings


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "skills" / "feedback-issue" / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import collect_feedback_diagnostics as collect_script  # noqa: E402
import feedback_issue_common as common  # noqa: E402
import prepare_feedback_issue as prepare_script  # noqa: E402
import submit_feedback_issue as submit_script  # noqa: E402


class _FakeResponse:
    """``requests.Response`` 的最小替身，覆盖提交脚本使用的属性和方法。"""

    def __init__(self, status_code, payload=None, headers=None, text=""):
        """保存响应状态、JSON 数据、响应头和文本。"""
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.text = text

    def json(self):
        """返回预设 JSON；没有 JSON 时模拟解析失败。"""
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


class FeedbackIssueScriptTestCase(unittest.TestCase):
    """为脚本测试提供隔离的 CONFIG_DIR。"""

    def setUp(self):
        """创建临时配置目录，避免测试读写真实 config。"""
        self._tmp = tempfile.TemporaryDirectory()
        self._config_backup = settings.CONFIG_DIR
        self._token_backup = settings.GITHUB_TOKEN
        settings.CONFIG_DIR = self._tmp.name
        settings.GITHUB_TOKEN = None
        settings.LOG_PATH.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        """恢复全局 settings 并清理临时目录。"""
        settings.CONFIG_DIR = self._config_backup
        settings.GITHUB_TOKEN = self._token_backup
        self._tmp.cleanup()

    def _write_log(self, text: str) -> Path:
        """写入临时 moviepilot.log 并返回路径。"""
        log_path = settings.LOG_PATH / "moviepilot.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(text, encoding="utf-8")
        return log_path

    def _valid_draft(self, diagnostics_file: str) -> dict:
        """构造一份可通过质量校验的 Issue 草稿。"""
        return {
            "title": "[错误报告]: 订阅刷新接口返回 500 错误码",
            "version": "v2.12.2",
            "environment": "Docker",
            "issue_type": "主程序运行问题",
            "original_user_request": "订阅刷新接口返回 500，帮我提交上游 Issue",
            "diagnostics_file": diagnostics_file,
            "description": (
                "## 现象\n"
                "- 订阅刷新接口持续返回 500，调用 /api/v1/subscribe/refresh 后失败。\n\n"
                "## 复现步骤\n"
                "1. 在 WebUI 触发刷新订阅。\n"
                "2. 后端日志出现 RecognizeError。\n"
                "3. 前端弹出 500。\n\n"
                "## 期望行为\n"
                "- 正常完成订阅刷新流程，无 500 错误。\n\n"
                "## 已定位 / 推测\n"
                "- 仅为推测：订阅刷新链路的识别异常未被正确处理。\n\n"
                "## 已尝试的处理\n"
                "- 重启后仍可复现。"
            ),
        }

    def _create_diagnostics_file(self, logs: str = "ERROR demo") -> Path:
        """创建脚本运行时诊断文件并返回路径。"""
        diagnostics_file = common.runtime_file("diagnostics", ".json")
        common.write_json_file(
            diagnostics_file,
            {
                "original_user_request": "订阅刷新接口返回 500，帮我提交上游 Issue",
                "found": bool(logs),
                "logs": logs,
                "source_files": [str(settings.LOG_PATH / "moviepilot.log")],
            },
        )
        return diagnostics_file


class TestFeedbackIssueCommon(FeedbackIssueScriptTestCase):
    """共享函数测试。"""

    def test_redact_logs_strips_common_secrets(self):
        """日志脱敏应覆盖 token、Cookie、PII 和本机用户路径。"""
        sample = (
            "Cookie: session=foo; passkey=secret123\n"
            "Authorization: Bearer ghp_abcdefghijklmnopqrstuvwx\n"
            "api_key=mysecret\n"
            "password: hunter2\n"
            "user@example.com\n"
            "/Users/alice/Library"
        )
        out = common.redact_logs(sample)
        for secret in ("secret123", "ghp_abcdefghijklmnopqrstuvwx", "mysecret",
                       "hunter2", "user@example.com", "/Users/alice/"):
            self.assertNotIn(secret, out)
        self.assertIn("<REDACTED>", out)

    def test_build_prefill_url_encodes_and_redacts(self):
        """预填 URL 应正确编码中文并脱敏日志。"""
        url = common.build_prefill_url(
            title="[错误报告]: 版本测试",
            version="v2.12.2",
            environment="Docker",
            issue_type="主程序运行问题",
            description="line1\nline2",
            logs="Cookie: leak_me",
        )
        self.assertIn("%E7%89%88", url)
        self.assertIn("%0A", url)
        self.assertIn("template=bug_report.yml", url)
        self.assertNotIn(quote("leak_me", safe=""), url)

    def test_check_content_quality_rejects_test_intent(self):
        """原始请求暴露测试链路意图时必须拒绝。"""
        error = common.check_content_quality(
            title="[错误报告]: TMDB识别错误，将动画识别为其他作品",
            original_user_request="我是开发者，为我反馈一个测试 ISSUE，看能否跑通",
            description=(
                "## 现象\nTMDB识别错误。\n\n"
                "## 复现步骤\n1. 搜索动画。\n2. 识别结果错误。\n\n"
                "## 期望行为\n正确识别。"
            ),
            logs="ERROR demo",
        )
        self.assertIsNotNone(error)
        self.assertIn("测试 issue", error.lower())

    def test_factory_no_longer_registers_feedback_issue_tools(self):
        """Agent 工厂不应再注册 feedback-issue 专用工具。"""
        with patch(
            "app.agent.tools.factory.PluginManager.get_plugin_agent_tools",
            return_value=[],
        ):
            tools = MoviePilotToolFactory.create_tools(
                session_id="feedback-issue-session",
                user_id="10001",
            )
        tool_names = {tool.name for tool in tools}
        self.assertNotIn("collect_feedback_diagnostics", tool_names)
        self.assertNotIn("prepare_feedback_issue", tool_names)
        self.assertNotIn("submit_feedback_issue", tool_names)


class TestCollectFeedbackDiagnosticsScript(FeedbackIssueScriptTestCase):
    """诊断收集脚本测试。"""

    def test_normalize_keywords_drops_vague_terms(self):
        """关键词过滤应丢弃错误、异常等泛词。"""
        out = collect_script.normalize_keywords(["TMDB", "错误", "异常", "scrape_metadata", "x"])
        self.assertEqual(out, ["TMDB", "scrape_metadata"])

    def test_has_explicit_feedback_intent(self):
        """入口意图门只放行明确提 Issue 的请求。"""
        self.assertTrue(collect_script.has_explicit_feedback_intent("TMDB 出错了，帮我提 issue"))
        self.assertFalse(collect_script.has_explicit_feedback_intent("TMDB 一直在报错"))

    def test_filter_lines_drops_history_and_meta_noise(self):
        """筛选日志时应丢掉历史行和 Agent 自身噪音。"""
        now = datetime.now()
        old = now - timedelta(hours=3)
        recent = now - timedelta(minutes=5)
        text = "\n".join([
            f"【INFO】{old.strftime('%Y-%m-%d %H:%M:%S')},123 - tmdb - TMDB failed 历史",
            f"【DEBUG】{recent.strftime('%Y-%m-%d %H:%M:%S')},100 - base.py - Executing tool",
            f"【ERROR】{recent.strftime('%Y-%m-%d %H:%M:%S')},123 - tmdb - TMDB failed 当前",
            "    Traceback (most recent call last):",
        ])
        out = collect_script.filter_lines(
            text,
            keywords=["TMDB"],
            max_lines=80,
            window_start=now - timedelta(minutes=30),
        )
        joined = "\n".join(out)
        self.assertIn("当前", joined)
        self.assertIn("Traceback", joined)
        self.assertNotIn("历史", joined)
        self.assertNotIn("Executing tool", joined)

    def test_collect_writes_diagnostics_file_without_returning_logs(self):
        """collect 脚本结果应返回文件句柄和统计，不直接返回日志正文。"""
        recent = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._write_log(f"【ERROR】{recent},000 - tmdb - TMDB lookup failed Cookie: secret")
        result = collect_script.collect_diagnostics(
            original_user_request="TMDB 报错，帮我反馈 issue",
            keywords=["TMDB"],
            max_lines=80,
            time_window_minutes=30,
        )
        self.assertTrue(result["success"])
        self.assertIn("diagnostics_file", result)
        self.assertNotIn("logs", result)
        diagnostics = common.read_json_file(result["diagnostics_file"])
        self.assertIn("TMDB lookup failed", diagnostics["logs"])
        self.assertIn("Cookie: <REDACTED>", diagnostics["logs"])
        self.assertNotIn("secret", diagnostics["logs"])


class TestPrepareAndSubmitScripts(FeedbackIssueScriptTestCase):
    """预览与提交脚本测试。"""

    def test_prepare_generates_payload_and_preview_files(self):
        """prepare 脚本应生成 payload_file 和包含脱敏日志的 preview_file。"""
        diagnostics_file = self._create_diagnostics_file("ERROR demo Cookie: secret")
        draft_file = common.runtime_file("draft", ".json")
        common.write_json_file(draft_file, self._valid_draft(str(diagnostics_file)))

        result = prepare_script.prepare_issue(draft_file)

        self.assertTrue(result["success"])
        self.assertTrue(Path(result["payload_file"]).exists())
        preview = Path(result["preview_file"]).read_text(encoding="utf-8")
        self.assertIn("请确认是否提交以下问题反馈", preview)
        self.assertIn("Cookie: <REDACTED>", preview)
        self.assertNotIn("secret", preview)

    def test_prepare_rejects_invalid_draft(self):
        """prepare 脚本应拒绝缺少结构信息的草稿。"""
        diagnostics_file = self._create_diagnostics_file()
        draft = self._valid_draft(str(diagnostics_file))
        draft["description"] = (
            "用户反馈下载任务完成后无法移动文件，系统看起来没有按照配置执行"
            "媒体库转移，请协助排查下载器联动和转移模块之间是否存在后端异常。"
        )
        draft_file = common.runtime_file("draft", ".json")
        common.write_json_file(draft_file, draft)

        result = prepare_script.prepare_issue(draft_file)

        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "invalid_draft")
        self.assertIn("结构信息", result["message"])

    def test_submit_returns_prefill_url_without_token(self):
        """未配置 GITHUB_TOKEN 时 submit 脚本应返回预填 URL。"""
        diagnostics_file = self._create_diagnostics_file("ERROR demo")
        draft_file = common.runtime_file("draft", ".json")
        common.write_json_file(draft_file, self._valid_draft(str(diagnostics_file)))
        prepared = prepare_script.prepare_issue(draft_file)

        result = submit_script.submit_issue(prepared["payload_file"], username="admin")

        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "no_token")
        self.assertIn("https://github.com/jxxghp/MoviePilot/issues/new", result["prefill_url"])

    def test_submit_success_with_github_token(self):
        """配置 GITHUB_TOKEN 且 API 返回 201 时 submit 脚本应报告成功。"""
        settings.GITHUB_TOKEN = "ghp_test_token"
        diagnostics_file = self._create_diagnostics_file("ERROR demo")
        draft_file = common.runtime_file("draft", ".json")
        common.write_json_file(draft_file, self._valid_draft(str(diagnostics_file)))
        prepared = prepare_script.prepare_issue(draft_file)

        with patch(
            "submit_feedback_issue.RequestUtils.post",
            return_value=_FakeResponse(
                201,
                payload={
                    "number": 9999,
                    "html_url": "https://github.com/jxxghp/MoviePilot/issues/9999",
                },
            ),
        ):
            result = submit_script.submit_issue(prepared["payload_file"], username="admin")

        self.assertTrue(result["success"])
        self.assertEqual(result["issue_number"], 9999)
        self.assertIn("/9999", result["issue_url"])

    def test_submit_user_rate_limit(self):
        """同一管理员连续提交应被脚本级冷却限制挡住。"""
        state = common.load_submission_state()
        state["user_submissions"] = {"admin": [time.time()]}
        common.save_submission_state(state)
        diagnostics_file = self._create_diagnostics_file("ERROR demo")
        draft_file = common.runtime_file("draft", ".json")
        draft = self._valid_draft(str(diagnostics_file))
        draft["title"] = "[错误报告]: 另一个完全不同的后端报错"
        common.write_json_file(draft_file, draft)
        prepared = prepare_script.prepare_issue(draft_file)

        result = submit_script.submit_issue(prepared["payload_file"], username="admin")

        self.assertEqual(result["reason"], "rate_limited_user")
        self.assertIn("30 分钟", result["message"])
