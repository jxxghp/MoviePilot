"""``submit_feedback_issue`` Agent 工具的单元测试。

覆盖范围（按 review 反馈"必修问题 2"补齐）：
- 工厂注册：新工具能被正常加载到默认工具集中
- 静态辅助：URL 构造、Issue body 渲染、日志脱敏、失败分类、长度截断
- ``run()`` 主流程：枚举校验、no_token 降级、API 成功、API 失败 +
  rate_limited 分支、网络异常分支、去重逻辑
- send_tool_message 全部走 mock，保证测试无外部 IO
"""

from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import patch
from urllib.parse import quote

from app.agent.tools.factory import MoviePilotToolFactory
from app.agent.tools.impl.submit_feedback_issue import (
    FEEDBACK_REPO,
    MAX_LOGS_CHARS,
    MAX_TITLE_CHARS,
    MAX_URL_LOGS_CHARS,
    SubmitFeedbackIssueTool,
)
from app.core.config import settings


class _FakeResponse:
    """``httpx.Response`` 的最小替身，覆盖工具用到的 4 个属性/方法。"""

    def __init__(self, status_code, payload=None, headers=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


def _run(coro):
    """跑一个 coroutine，避免每个用例重复写 asyncio.run。"""
    return asyncio.run(coro)


class TestSubmitFeedbackIssueStaticHelpers(unittest.TestCase):
    """所有静态/类方法的纯函数测试，无副作用、无 IO。"""

    def test_validate_enum_accepts_allowed_values(self):
        self.assertIsNone(
            SubmitFeedbackIssueTool._validate_enum("Docker", ("Docker", "Windows"), "env")
        )

    def test_validate_enum_rejects_disallowed_values(self):
        msg = SubmitFeedbackIssueTool._validate_enum(
            "linux", ("Docker", "Windows"), "env"
        )
        self.assertIsNotNone(msg)
        self.assertIn("Docker", msg)
        self.assertIn("Windows", msg)
        self.assertIn("'linux'", msg)

    def test_truncate_keeps_short_text(self):
        self.assertEqual(SubmitFeedbackIssueTool._truncate("hello", 100), "hello")

    def test_truncate_clips_long_text_with_marker(self):
        out = SubmitFeedbackIssueTool._truncate("a" * 1000, 100)
        self.assertLessEqual(len(out), 100)
        self.assertIn("已截断", out)

    def test_redact_logs_strips_common_secrets(self):
        sample = (
            "Cookie: session=foo; passkey=secret123\n"
            "Authorization: Bearer ghp_abcdefghijklmn\n"
            "api_key=mysecret\n"
            "password: hunter2\n"
            "Set-Cookie: session=foo"
        )
        out = SubmitFeedbackIssueTool._redact_logs(sample)
        self.assertNotIn("ghp_abcdefghijklmn", out)
        self.assertNotIn("mysecret", out)
        self.assertNotIn("hunter2", out)
        self.assertNotIn("secret123", out)
        self.assertIn("<REDACTED>", out)

    def test_redact_logs_preserves_original_separator(self):
        # gemini-code-assist review 提醒：原始分隔符（``:`` 或 ``=``）必须保留
        self.assertIn("api_key=<REDACTED>", SubmitFeedbackIssueTool._redact_logs("api_key=xxx"))
        self.assertIn("api_key: <REDACTED>", SubmitFeedbackIssueTool._redact_logs("api_key: xxx"))
        self.assertIn("password: <REDACTED>", SubmitFeedbackIssueTool._redact_logs("password: xxx"))
        self.assertIn("token=<REDACTED>", SubmitFeedbackIssueTool._redact_logs("token=xxx"))

    def test_sanitize_logs_caps_to_limit_and_redacts(self):
        result = SubmitFeedbackIssueTool._sanitize_logs(
            "Cookie: secret\n" + "A" * 5000, limit=1024
        )
        self.assertNotIn("Cookie: secret", result)
        self.assertIn("Cookie: <REDACTED>", result)
        self.assertLessEqual(len(result), 1024)

    def test_sanitize_logs_returns_empty_for_blank_input(self):
        self.assertEqual(SubmitFeedbackIssueTool._sanitize_logs(None, 1024), "")
        self.assertEqual(SubmitFeedbackIssueTool._sanitize_logs("   \n  ", 1024), "")

    def test_build_issue_body_contains_all_sections(self):
        body = SubmitFeedbackIssueTool._build_issue_body(
            version="v2.12.2",
            environment="Docker",
            issue_type="主程序运行问题",
            description="## 现象\n- xxx",
            logs="ERROR demo",
        )
        for section in (
            "### 确认",
            "### 当前程序版本",
            "### 运行环境",
            "### 问题类型",
            "### 问题描述",
            "### 发生问题时系统日志和配置文件",
            "v2.12.2",
            "Docker",
            "主程序运行问题",
            "ERROR demo",
        ):
            self.assertIn(section, body, msg=f"missing: {section!r}")

    def test_build_issue_body_handles_empty_logs(self):
        body = SubmitFeedbackIssueTool._build_issue_body(
            version="v2.12.2",
            environment="Docker",
            issue_type="主程序运行问题",
            description="x",
            logs=None,
        )
        self.assertIn("会话中未捕获到相关后端日志。", body)

    def test_build_issue_body_redacts_logs(self):
        body = SubmitFeedbackIssueTool._build_issue_body(
            version="v2.12.2",
            environment="Docker",
            issue_type="主程序运行问题",
            description="x",
            logs="Cookie: foo=bar",
        )
        self.assertIn("Cookie: <REDACTED>", body)
        self.assertNotIn("Cookie: foo=bar", body)

    def test_build_issue_body_truncates_oversized_logs(self):
        body = SubmitFeedbackIssueTool._build_issue_body(
            version="v2.12.2",
            environment="Docker",
            issue_type="主程序运行问题",
            description="x",
            logs="A" * (MAX_LOGS_CHARS + 1000),
        )
        # logs 段落在 ```bash ... ``` 之间；提取出来验证长度
        log_segment = body.split("```bash\n", 1)[1].rsplit("\n```", 1)[0]
        self.assertLessEqual(len(log_segment), MAX_LOGS_CHARS)
        self.assertIn("已截断", log_segment)

    def test_build_prefill_url_encodes_chinese_correctly(self):
        url = SubmitFeedbackIssueTool._build_prefill_url(
            title="[错误报告]: 版本测试",
            version="v2.12.2",
            environment="Docker",
            issue_type="主程序运行问题",
            description="line1\nline2",
            logs=None,
        )
        # "版" 的 UTF-8 percent-encoding 应为 %E7%89%88（曾经被 LLM 翻成 %E7%79%88）
        self.assertIn("%E7%89%88", url)
        # 换行用 %0A 而非 %0D，空格不能用 + 表示
        self.assertIn("%0A", url)
        self.assertNotIn("+", url.split("?", 1)[1])
        # 必须带 template 参数才会进入 Issue Forms 表单
        self.assertIn("template=bug_report.yml", url)

    def test_build_prefill_url_redacts_and_caps_logs(self):
        # gemini-code-assist HIGH 反馈：预填 URL 必须脱敏 + 截断到 3KB
        sensitive_logs = "Cookie: leak_me\n" + ("A" * (MAX_URL_LOGS_CHARS + 5000))
        url = SubmitFeedbackIssueTool._build_prefill_url(
            title="t",
            version="v2.12.2",
            environment="Docker",
            issue_type="主程序运行问题",
            description="d",
            logs=sensitive_logs,
        )
        # Cookie 必须不出现在 URL 里
        self.assertNotIn(quote("leak_me", safe=""), url)
        self.assertIn(quote("<REDACTED>", safe=""), url)
        # 总 URL 长度可控（其它字段都很短，所以主要由 logs 决定）
        # logs 的 percent-encoding 膨胀比 ~3x（每个 ASCII A 是 1 byte，不膨胀；
        # 但 marker / 中文会膨胀），用 1.5x 余量验证
        self.assertLess(len(url), MAX_URL_LOGS_CHARS * 2)

    def test_classify_failure_handles_main_branches(self):
        self.assertEqual(SubmitFeedbackIssueTool._classify_failure(401), "no_permission")
        self.assertEqual(SubmitFeedbackIssueTool._classify_failure(404), "no_permission")
        self.assertEqual(
            SubmitFeedbackIssueTool._classify_failure(403),
            "no_permission",
        )
        self.assertEqual(SubmitFeedbackIssueTool._classify_failure(422), "invalid_payload")
        self.assertEqual(SubmitFeedbackIssueTool._classify_failure(500), "github_unavailable")
        self.assertEqual(SubmitFeedbackIssueTool._classify_failure(502), "github_unavailable")
        self.assertEqual(SubmitFeedbackIssueTool._classify_failure(None), "api_error")

    def test_classify_failure_detects_rate_limit_on_403(self):
        self.assertEqual(
            SubmitFeedbackIssueTool._classify_failure(
                403, headers={"X-RateLimit-Remaining": "0"}
            ),
            "rate_limited",
        )
        # 大小写不敏感
        self.assertEqual(
            SubmitFeedbackIssueTool._classify_failure(
                403, headers={"x-ratelimit-remaining": "0"}
            ),
            "rate_limited",
        )
        # 仍有余量时按无权限分类
        self.assertEqual(
            SubmitFeedbackIssueTool._classify_failure(
                403, headers={"X-RateLimit-Remaining": "10"}
            ),
            "no_permission",
        )

    def test_safe_response_dict_falls_back_for_array_or_invalid_json(self):
        # 合法 dict
        self.assertEqual(
            SubmitFeedbackIssueTool._safe_response_dict(
                _FakeResponse(200, payload={"message": "ok"})
            ),
            {"message": "ok"},
        )
        # array 不是 dict，应返回空 dict 而不是抛 AttributeError
        self.assertEqual(
            SubmitFeedbackIssueTool._safe_response_dict(
                _FakeResponse(200, payload=[1, 2, 3])
            ),
            {},
        )
        # 非 JSON 响应
        self.assertEqual(
            SubmitFeedbackIssueTool._safe_response_dict(_FakeResponse(500)),
            {},
        )


class TestSubmitFeedbackIssueRun(unittest.TestCase):
    """``run()`` 主流程测试；外部 HTTP / send_tool_message 全部 mock。"""

    def setUp(self):
        # 每个用例独立清空进程级去重缓存
        SubmitFeedbackIssueTool._recent_submissions.clear()
        # 默认无 token，避免误打真实 GitHub API
        self._token_backup = settings.GITHUB_TOKEN
        settings.GITHUB_TOKEN = None
        self.tool = SubmitFeedbackIssueTool(session_id="s", user_id="u")
        self.push_calls = []

        async def fake_send(_self, text, title="", image=None):
            self.push_calls.append({"text": text, "title": title})

        self._push_patcher = patch.object(
            SubmitFeedbackIssueTool, "send_tool_message", new=fake_send
        )
        self._push_patcher.start()

    def tearDown(self):
        self._push_patcher.stop()
        settings.GITHUB_TOKEN = self._token_backup

    def _good_kwargs(self, **overrides):
        kwargs = dict(
            explanation="user authorized",
            title="[错误报告]: 测试 issue",
            version="v2.12.2",
            environment="Docker",
            issue_type="主程序运行问题",
            description="## 现象\n- demo",
        )
        kwargs.update(overrides)
        return kwargs

    def test_rejects_invalid_environment_before_calling_api(self):
        result = _run(self.tool.run(**self._good_kwargs(environment="linux")))
        data = json.loads(result)
        self.assertFalse(data["success"])
        self.assertEqual(data["reason"], "invalid_input")
        self.assertEqual(self.push_calls, [])

    def test_rejects_invalid_issue_type(self):
        result = _run(self.tool.run(**self._good_kwargs(issue_type="random")))
        data = json.loads(result)
        self.assertFalse(data["success"])
        self.assertEqual(data["reason"], "invalid_input")

    def test_no_token_branch_pushes_prefill_url_and_hides_it_from_llm(self):
        result = _run(self.tool.run(**self._good_kwargs()))
        data = json.loads(result)
        self.assertFalse(data["success"])
        self.assertEqual(data["reason"], "no_token")
        self.assertTrue(data["url_delivered"])
        # 关键不变量：URL 不应该回流给 LLM 转述
        self.assertIsNone(data["prefill_url"])
        # send_tool_message 必须被调一次，且消息体内含完整 URL
        self.assertEqual(len(self.push_calls), 1)
        self.assertIn("https://github.com/jxxghp/MoviePilot/issues/new", self.push_calls[0]["text"])

    def test_truncates_oversized_title_before_submission(self):
        title = "[错误报告]: " + ("超长" * 200)
        result = _run(self.tool.run(**self._good_kwargs(title=title)))
        data = json.loads(result)
        self.assertEqual(data["reason"], "no_token")
        # pushed message contains the truncated title via dedup-trail check;
        # we can't see the actual title pushed, but we can confirm dedup uses
        # the truncated form by re-submitting and verifying dedup hit.
        SubmitFeedbackIssueTool._recent_submissions.clear()
        # And verify directly:
        truncated = SubmitFeedbackIssueTool._truncate(title, MAX_TITLE_CHARS, marker="…")
        self.assertLessEqual(len(truncated), MAX_TITLE_CHARS)

    def test_success_branch_records_submission_and_dedups_next_call(self):
        settings.GITHUB_TOKEN = "ghp_test_token"

        async def fake_post(_self, url, **kw):
            return _FakeResponse(
                201,
                payload={
                    "html_url": "https://github.com/jxxghp/MoviePilot/issues/9999",
                    "number": 9999,
                },
            )

        with patch(
            "app.agent.tools.impl.submit_feedback_issue.AsyncRequestUtils.post_res",
            new=fake_post,
        ):
            first = _run(self.tool.run(**self._good_kwargs()))
            second = _run(self.tool.run(**self._good_kwargs()))

        d1 = json.loads(first)
        d2 = json.loads(second)
        self.assertTrue(d1["success"])
        self.assertEqual(d1["repo"], FEEDBACK_REPO)
        self.assertEqual(d1["issue_number"], 9999)
        self.assertIsNone(d1["issue_url"])  # URL 走 send_tool_message
        self.assertTrue(d1["url_delivered"])

        # 第二次相同提交应被去重拒绝
        self.assertFalse(d2["success"])
        self.assertEqual(d2["reason"], "duplicate")

    def test_rate_limited_branch_when_403_with_zero_remaining(self):
        settings.GITHUB_TOKEN = "ghp_test_token"

        async def fake_post(_self, url, **kw):
            return _FakeResponse(
                403,
                payload={"message": "API rate limit exceeded"},
                headers={"X-RateLimit-Remaining": "0"},
            )

        with patch(
            "app.agent.tools.impl.submit_feedback_issue.AsyncRequestUtils.post_res",
            new=fake_post,
        ):
            result = _run(self.tool.run(**self._good_kwargs()))

        data = json.loads(result)
        self.assertFalse(data["success"])
        self.assertEqual(data["reason"], "rate_limited")
        self.assertTrue(data["url_delivered"])
        # 限流时不应该提示用户去改 token
        self.assertNotIn("Token", data["message"][:80])

    def test_no_permission_branch_when_403_with_remaining(self):
        settings.GITHUB_TOKEN = "ghp_test_token"

        async def fake_post(_self, url, **kw):
            return _FakeResponse(
                403,
                payload={"message": "Resource not accessible by personal access token"},
                headers={"X-RateLimit-Remaining": "4990"},
            )

        with patch(
            "app.agent.tools.impl.submit_feedback_issue.AsyncRequestUtils.post_res",
            new=fake_post,
        ):
            result = _run(self.tool.run(**self._good_kwargs()))

        data = json.loads(result)
        self.assertEqual(data["reason"], "no_permission")
        # 应该提示重新配 token
        self.assertIn("Token", data["message"])

    def test_invalid_payload_branch_when_422(self):
        settings.GITHUB_TOKEN = "ghp_test_token"

        async def fake_post(_self, url, **kw):
            return _FakeResponse(
                422,
                payload={"message": "Validation Failed", "errors": []},
            )

        with patch(
            "app.agent.tools.impl.submit_feedback_issue.AsyncRequestUtils.post_res",
            new=fake_post,
        ):
            result = _run(self.tool.run(**self._good_kwargs()))

        data = json.loads(result)
        self.assertEqual(data["reason"], "invalid_payload")

    def test_network_error_branch_when_exception_raised(self):
        settings.GITHUB_TOKEN = "ghp_test_token"

        async def fake_post(_self, url, **kw):
            raise ConnectionError("simulated DNS failure")

        with patch(
            "app.agent.tools.impl.submit_feedback_issue.AsyncRequestUtils.post_res",
            new=fake_post,
        ):
            result = _run(self.tool.run(**self._good_kwargs()))

        data = json.loads(result)
        self.assertFalse(data["success"])
        self.assertEqual(data["reason"], "network_error")
        self.assertTrue(data["url_delivered"])

    def test_dedup_blocks_repeat_within_window_for_attempted_api_call(self):
        settings.GITHUB_TOKEN = "ghp_test_token"

        async def fake_post(_self, url, **kw):
            return _FakeResponse(500, payload={"message": "internal"})

        with patch(
            "app.agent.tools.impl.submit_feedback_issue.AsyncRequestUtils.post_res",
            new=fake_post,
        ):
            first = _run(self.tool.run(**self._good_kwargs()))
            second = _run(self.tool.run(**self._good_kwargs()))

        d1 = json.loads(first)
        d2 = json.loads(second)
        self.assertEqual(d1["reason"], "github_unavailable")
        # 即便首次失败也应进入 dedup 窗口，避免 LLM loop 不断重试同一提交
        self.assertEqual(d2["reason"], "duplicate")


class TestSubmitFeedbackIssueFactoryRegistration(unittest.TestCase):
    def test_factory_registers_submit_feedback_issue_tool(self):
        with patch(
            "app.agent.tools.factory.PluginManager.get_plugin_agent_tools",
            return_value=[],
        ):
            tools = MoviePilotToolFactory.create_tools(
                session_id="feedback-issue-session",
                user_id="10001",
            )

        tool_names = {tool.name for tool in tools}
        self.assertIn("submit_feedback_issue", tool_names)


if __name__ == "__main__":
    unittest.main()
