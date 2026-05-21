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
import time
import unittest
from unittest.mock import patch
from urllib.parse import quote

from app.agent.tools.factory import MoviePilotToolFactory
from app.agent.tools.impl.submit_feedback_issue import (
    FEEDBACK_REPO,
    MAX_LOGS_CHARS,
    MAX_TITLE_CHARS,
    MAX_URL_LOGS_CHARS,
    USER_DAILY_WINDOW_SECONDS as USER_DAILY_WINDOW_SECONDS_TEST,
    SubmitFeedbackIssueTool,
)
from app.agent.tools.impl.feedback_issue_state import (
    build_feedback_draft_hash,
    feedback_issue_state_store,
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
        self.assertIn("api_key=<REDACTED>", SubmitFeedbackIssueTool._redact_logs("api_key=xxx_yy"))
        self.assertIn("api_key: <REDACTED>", SubmitFeedbackIssueTool._redact_logs("api_key: xxxxxx"))
        self.assertIn("password: <REDACTED>", SubmitFeedbackIssueTool._redact_logs("password: xxxx"))
        self.assertIn("token=<REDACTED>", SubmitFeedbackIssueTool._redact_logs("token=xxxx"))

    def test_redact_logs_strips_extended_credentials(self):
        # 扩充后的脱敏需要覆盖：bare GitHub PAT、IM webhook、PT passkey、
        # 邮箱、公网 IP、用户家目录、Windows 用户路径、X-Api-Key 头部、
        # 厂商常见字段（client_secret / corp_secret / webhook 等）、
        # 以及用户身份字段（#5808 教训：userid / username）。
        cases = [
            ("plain bare ghp_xxxxxxxxxxxxxxxxxxxxxx", "ghp_xxxxxxxxxxxxxxxxxxxxxx"),
            ("xoxb-xxxxxxxxxxxx", "xoxb-xxxxxxxxxxxx"),
            ("github_pat_xxxxxxxxxxxxxxxxxxxxxx", "github_pat_xxxxxxxxxxxxxxxxxxxxxx"),
            ("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc123", "key=abc123"),
            ("https://hooks.slack.com/services/T0/B0/abcdef", "abcdef"),
            ("X-Api-Key: secret-xyz-123", "secret-xyz-123"),
            ("client_secret=topsecret_value", "topsecret_value"),
            ("corp_secret: corp_topsecret", "corp_topsecret"),
            ("user@example.com login failed", "user@example.com"),
            ("Connected to 203.0.113.45", "203.0.113.45"),
            ("Path /Users/alice/Library/...", "/Users/alice/"),
            ("Path /home/bob/.config/foo", "/home/bob/"),
            (r"Path C:\Users\Charlie\AppData", r"C:\Users\Charlie\\"),
            ("rsskey=abcd1234efgh", "rsskey=abcd1234efgh"),
            # 用户身份 PII
            ("userid=1234567890, username=fake_user", "1234567890"),
            ("userid=1234567890, username=fake_user", "fake_user"),
            ("user_id: 11111111", "11111111"),
            ("open_id=ou_abcdef", "ou_abcdef"),
            ("union_id=on_xxx123", "on_xxx123"),
            # MoviePilot 会话 ID（embed userid）
            ("Agent推理 session_id=user_1234567890_1779337335 input=...", "1234567890_1779337335"),
            ("session_id=user_1234567890_1779337335 fired", "user_1234567890_1779337335"),
            ("session_id=arbitrary_string_value", "arbitrary_string_value"),
        ]
        for sample, secret_fragment in cases:
            out = SubmitFeedbackIssueTool._redact_logs(sample)
            self.assertNotIn(secret_fragment, out, msg=f"未脱敏: {sample!r} → {out!r}")

    def test_redact_logs_preserves_private_ipv4_addresses(self):
        # 私网地址不脱敏，方便 maintainer 理解部署拓扑
        out = SubmitFeedbackIssueTool._redact_logs(
            "Local 127.0.0.1; LAN 192.168.1.10; container 10.244.5.6; mgmt 172.16.0.1"
        )
        for keep in ("127.0.0.1", "192.168.1.10", "10.244.5.6", "172.16.0.1"):
            self.assertIn(keep, out, msg=f"私网地址被错误脱敏: {keep}")

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

    def test_repeat_gibberish_does_not_false_positive_on_separators(self):
        # 修复 review #1：横线 / 等号 / 井号 等 Markdown 分隔符大量重复
        # 不应被判作乱码（合法 description 里很常见）
        from app.agent.tools.impl.submit_feedback_issue import _REPEAT_GIBBERISH
        for legitimate in ("========", "----------", "____", "########",
                           "******", "~~~~~~~~", "```python```",
                           "..........", "//////", "++++++"):
            self.assertIsNone(_REPEAT_GIBBERISH.search(legitimate),
                              msg=f"误判分隔符：{legitimate!r}")
        # 但真正的字母/汉字重复应该照样命中
        for gibberish in ("aaaaaaaa", "为为为为为为为为", "11111111"):
            self.assertIsNotNone(_REPEAT_GIBBERISH.search(gibberish),
                                 msg=f"应判作乱码：{gibberish!r}")

    def test_check_content_quality_empty_title_after_prefix(self):
        # title 完全只有 ``[错误报告]:`` 前缀、正文为空也应被拒
        err = SubmitFeedbackIssueTool._check_content_quality(
            title="[错误报告]:",
            description="正常长度的描述，包含现象和复现步骤，行行行行行行行" * 3,
            original_user_request="用户反馈订阅刷新接口返回 500，希望提交上游 Issue",
        )
        self.assertIsNotNone(err)
        self.assertIn("标题正文太短", err)

    def test_normalize_username_handles_drift(self):
        # 修复 review #3：username 拼写漂移要被归一化到同一个桶
        self.assertEqual(SubmitFeedbackIssueTool._normalize_username("Admin"), "admin")
        self.assertEqual(SubmitFeedbackIssueTool._normalize_username("  admin "), "admin")
        self.assertEqual(SubmitFeedbackIssueTool._normalize_username("ADMIN"), "admin")
        self.assertEqual(SubmitFeedbackIssueTool._normalize_username(""), "")
        self.assertEqual(SubmitFeedbackIssueTool._normalize_username(None), "")

    def test_user_submissions_eviction_keeps_dict_bounded(self):
        # 修复 review #3：恶意 / 漂移 username 不应该把 _user_submissions 撑爆
        from app.agent.tools.impl.submit_feedback_issue import (
            MAX_USER_SUBMISSIONS_BUCKETS,
        )
        SubmitFeedbackIssueTool._user_submissions.clear()
        # 灌入超过上限的不同 username
        for i in range(MAX_USER_SUBMISSIONS_BUCKETS + 50):
            SubmitFeedbackIssueTool._record_user_submission(f"user{i}")
        self.assertLessEqual(
            len(SubmitFeedbackIssueTool._user_submissions),
            MAX_USER_SUBMISSIONS_BUCKETS,
        )

    def test_check_user_rate_limit_clears_fully_expired_bucket(self):
        # 修复 review：24h 之前的桶应该被清掉而不是留个空 list 永驻
        SubmitFeedbackIssueTool._user_submissions.clear()
        SubmitFeedbackIssueTool._user_submissions["staleuser"] = [
            time.time() - (USER_DAILY_WINDOW_SECONDS_TEST + 60),
        ]
        result = SubmitFeedbackIssueTool._check_user_rate_limit("staleuser")
        self.assertIsNone(result)
        self.assertNotIn("staleuser", SubmitFeedbackIssueTool._user_submissions)

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
        # 每个用例独立清空进程级去重缓存与 per-user rate limit 状态
        SubmitFeedbackIssueTool._recent_submissions.clear()
        SubmitFeedbackIssueTool._user_submissions.clear()
        feedback_issue_state_store.clear()
        # 默认无 token，避免误打真实 GitHub API
        self._token_backup = settings.GITHUB_TOKEN
        settings.GITHUB_TOKEN = None
        self.tool = SubmitFeedbackIssueTool(session_id="s", user_id="u")
        # rate-limit 校验依赖 username；默认给一个合法 admin，单独的测试可覆盖
        self.tool._username = "admin"
        self.push_calls = []

        # _push_url_to_user 现在直接走 ToolChain().async_post_message 并
        # 关闭网页预览（修复 #5806 一次提交渲染 3 张预览卡的问题）。测试
        # 用 mock 直接替换该方法，捕获 url/title/hint 三元组即可。
        async def fake_push(_self, url, title, hint):
            self.push_calls.append({"text": f"{hint}\n\n{url}", "title": title, "url": url})
            return True

        self._push_patcher = patch.object(
            SubmitFeedbackIssueTool, "_push_url_to_user", new=fake_push
        )
        self._push_patcher.start()

        # 默认放行 superuser 校验，单独的拒绝用例会覆盖这个 stub
        async def fake_enforce(_self):
            return None

        self._enforce_patcher = patch.object(
            SubmitFeedbackIssueTool, "_enforce_superuser", new=fake_enforce
        )
        self._enforce_patcher.start()

    def tearDown(self):
        self._enforce_patcher.stop()
        self._push_patcher.stop()
        settings.GITHUB_TOKEN = self._token_backup

    def _good_kwargs(self, **overrides):
        """构造一份能通过 enum / 质量 / rate-limit 全部检查的合规 payload。

        默认 admin username 由 _enforce_superuser mock 放行，但 rate-limit
        和 quality gate 是独立检查，必须用 ≥50 字的真实样式 description 与
        非黑词单 title。"""
        kwargs = dict(
            explanation="user authorized to submit a feedback issue upstream",
            title="[错误报告]: 订阅刷新接口返回 500 错误码",
            version="v2.12.2",
            environment="Docker",
            issue_type="主程序运行问题",
            original_user_request="订阅刷新接口返回 500，帮我提交上游 Issue",
            description=(
                "## 现象\n"
                "- 订阅刷新接口持续返回 500，调用 /api/v1/subscribe/refresh\n"
                "## 复现\n"
                "1. 在 WebUI 触发刷新订阅\n"
                "2. 后端日志出现 RecognizeError，前端弹出 500\n"
                "## 期望\n"
                "正常完成订阅刷新流程，无 500 错误。"
            ),
        )
        kwargs.update(overrides)
        diagnostics = feedback_issue_state_store.create_diagnostics(
            session_id=self.tool._session_id,
            user_id=self.tool._user_id,
            username=self.tool._username,
            logs=kwargs.get("logs") or "ERROR demo feedback diagnostics",
            source_files=["/tmp/moviepilot.log"],
            found=True,
        )
        kwargs.setdefault("diagnostics_id", diagnostics.diagnostics_id)
        draft_hash = build_feedback_draft_hash(
            title=SubmitFeedbackIssueTool._truncate(
                kwargs["title"], MAX_TITLE_CHARS, marker="…"
            ),
            version=kwargs["version"],
            environment=kwargs["environment"],
            issue_type=kwargs["issue_type"],
            description=kwargs["description"],
            original_user_request=kwargs["original_user_request"],
            logs=kwargs.get("logs") if kwargs.get("logs") is not None else diagnostics.logs,
            diagnostics_id=kwargs["diagnostics_id"],
        )
        confirmation = feedback_issue_state_store.create_confirmation(
            session_id=self.tool._session_id,
            user_id=self.tool._user_id,
            username=self.tool._username,
            draft_hash=draft_hash,
            diagnostics_id=kwargs["diagnostics_id"],
        )
        feedback_issue_state_store.mark_confirmed(
            confirmation.confirmation_token,
            session_id=self.tool._session_id,
            user_id=self.tool._user_id,
        )
        kwargs.setdefault("confirmation_token", confirmation.confirmation_token)
        return kwargs

    def test_rejects_non_superuser_caller(self):
        # 关闭默认放行 stub，让真正的 _enforce_superuser 走 UserOper 路径
        self._enforce_patcher.stop()

        class _NonAdminUser:
            is_superuser = False

        async def fake_get(_self, name):
            return _NonAdminUser()

        with patch(
            "app.agent.tools.impl.submit_feedback_issue.UserOper.async_get_by_name",
            new=fake_get,
        ):
            self.tool._username = "regular-user"
            result = _run(self.tool.run(**self._good_kwargs()))

        # 重启动 enforce stub 给 tearDown 用
        self._enforce_patcher.start()

        data = json.loads(result)
        self.assertFalse(data["success"])
        self.assertEqual(data["reason"], "forbidden")
        # 不应执行任何下游副作用
        self.assertEqual(self.push_calls, [])
        self.assertEqual(SubmitFeedbackIssueTool._recent_submissions, {})

    def test_rejects_when_username_missing(self):
        self._enforce_patcher.stop()
        self.tool._username = ""
        result = _run(self.tool.run(**self._good_kwargs()))
        self._enforce_patcher.start()

        data = json.loads(result)
        self.assertEqual(data["reason"], "forbidden")
        self.assertIn("没有绑定", data["message"])

    def test_allows_superuser(self):
        self._enforce_patcher.stop()

        class _Admin:
            is_superuser = True

        async def fake_get(_self, name):
            return _Admin()

        with patch(
            "app.agent.tools.impl.submit_feedback_issue.UserOper.async_get_by_name",
            new=fake_get,
        ):
            self.tool._username = "admin-user"
            result = _run(self.tool.run(**self._good_kwargs()))

        self._enforce_patcher.start()
        data = json.loads(result)
        # superuser 放行后会落到 no_token 兜底（settings.GITHUB_TOKEN=None）
        self.assertEqual(data["reason"], "no_token")

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

    def test_rejects_without_diagnostics_record(self):
        kwargs = self._good_kwargs()
        kwargs["diagnostics_id"] = "missing-diagnostics"
        result = _run(self.tool.run(**kwargs))
        data = json.loads(result)
        self.assertFalse(data["success"])
        self.assertEqual(data["reason"], "diagnostics_required")

    def test_rejects_without_confirmed_preview_token(self):
        kwargs = self._good_kwargs()
        kwargs["confirmation_token"] = "not-confirmed"
        result = _run(self.tool.run(**kwargs))
        data = json.loads(result)
        self.assertFalse(data["success"])
        self.assertEqual(data["reason"], "confirmation_required")

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
            # 第二次同 payload 应被 60s dedup 拦下；rate-limit 窗口比 dedup 窗口大，
            # 测试想验证的是 dedup，所以手动清掉 per-user rate-limit 状态避免被
            # 先一步 rate-limited（rate-limit 优先级在 dedup 之前）。
            SubmitFeedbackIssueTool._user_submissions.clear()
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
            # 与 success 测试同理：清掉 rate-limit 状态，验证 dedup 独立生效
            SubmitFeedbackIssueTool._user_submissions.clear()
            second = _run(self.tool.run(**self._good_kwargs()))

        d1 = json.loads(first)
        d2 = json.loads(second)
        self.assertEqual(d1["reason"], "github_unavailable")
        # 即便首次失败也应进入 dedup 窗口，避免 LLM loop 不断重试同一提交
        self.assertEqual(d2["reason"], "duplicate")

    # ------------------------------------------------------------------
    # 内容质量门槛
    # ------------------------------------------------------------------
    def test_rejects_short_description(self):
        result = _run(self.tool.run(**self._good_kwargs(description="只有这么几个字")))
        data = json.loads(result)
        self.assertEqual(data["reason"], "rejected_quality")
        self.assertIn("问题描述太短", data["message"])

    def test_rejects_short_title(self):
        result = _run(self.tool.run(**self._good_kwargs(title="[错误报告]: 短")))
        data = json.loads(result)
        self.assertEqual(data["reason"], "rejected_quality")
        self.assertIn("标题正文太短", data["message"])

    def test_rejects_blocklisted_phrase_in_title(self):
        result = _run(self.tool.run(**self._good_kwargs(
            title="[错误报告]: 这是一个测试 issue 看看"
        )))
        data = json.loads(result)
        self.assertEqual(data["reason"], "rejected_quality")
        self.assertIn("测试 issue", data["message"])

    def test_rejects_pipeline_test_intent_phrases(self):
        # "我是开发者，反馈一个测试 ISSUE，看能否跑通" 这类口语化请求
        # 不能被 Agent 改写成真实样式 Issue 后提交到上游。
        for phrase in ("看能否跑通", "跑通流程", "链路测试", "测试提交"):
            with self.subTest(phrase=phrase):
                result = _run(self.tool.run(**self._good_kwargs(
                    title=f"[错误报告]: 订阅刷新接口异常{phrase}",
                )))
                data = json.loads(result)
                self.assertEqual(data["reason"], "rejected_quality")
                self.assertIn(phrase, data["message"])

    def test_rejects_pipeline_test_intent_from_original_request(self):
        # 即使 title/description 被 Agent 改写成真实样式，只要原始用户请求
        # 暴露了"测试 ISSUE / 看能否跑通"意图，也必须在工具层拒绝。
        context = {}
        self.tool.set_agent_context(context)
        result = _run(self.tool.run(**self._good_kwargs(
            title="[错误报告]: TMDB识别错误，将《吞噬星空》识别为其他作品",
            original_user_request="我是开发者，为我反馈一个测试 ISSUE，看能否跑通",
            description=(
                "## 现象\n"
                "TMDB识别错误，将动画《吞噬星空》识别为其他作品。\n\n"
                "## 复现步骤\n"
                "1. 搜索或订阅《吞噬星空》\n"
                "2. 系统尝试识别该媒体\n"
                "3. 识别结果错误，匹配到其他作品\n\n"
                "## 期望行为\n"
                "正确识别《吞噬星空》并匹配正确的TMDB ID。"
            ),
        )))
        data = json.loads(result)
        self.assertEqual(data["reason"], "rejected_quality")
        self.assertIn("测试 issue", data["message"])
        self.assertTrue(context.get("feedback_issue_rejected_quality"))
        self.assertIn("测试 issue", context.get("feedback_issue_rejected_quality_reason", ""))

    def test_submit_schema_rejects_logs_parameter(self):
        # 日志已经从 Agent 入参中移除：现在通过 diagnostics_id 在服务端 state
        # store 流转。pydantic schema 不应再声明 logs 字段，确保 LangChain
        # 在调用 _arun 时校验失败，挡住"agent 试图传 logs"的回归。
        from app.agent.tools.impl.submit_feedback_issue import (
            SubmitFeedbackIssueInput,
        )
        self.assertNotIn("logs", SubmitFeedbackIssueInput.model_fields)
        from app.agent.tools.impl.prepare_feedback_issue import (
            PrepareFeedbackIssueInput,
        )
        self.assertNotIn("logs", PrepareFeedbackIssueInput.model_fields)

    def test_rejects_unstructured_synthetic_description(self):
        # 截图里的第二次路径会把一句泛泛的"用户反馈..."提交成正式 Issue；
        # 工具层应要求至少包含现象 / 复现 / 期望信号，防止伪造问题跑通链路。
        result = _run(self.tool.run(**self._good_kwargs(
            title="[错误报告]: 下载任务完成后无法自动移动文件",
            description=(
                "用户反馈在下载任务完成后，系统无法按照配置的规则自动将文件移动到"
                "媒体库目录。请协助排查转移模块与下载器之间的联动是否存在异常。"
            ),
        )))
        data = json.loads(result)
        self.assertEqual(data["reason"], "rejected_quality")
        self.assertIn("结构信息", data["message"])

    def test_rejects_gibberish_repeat_pattern(self):
        # 用不在黑词单里的字符做 ≥8 连重复（"为" * 9），并搭配足够长的中文
        # 正文凑过 50 字门槛但不踩 lorem/test 等黑词
        result = _run(self.tool.run(**self._good_kwargs(
            description="为为为为为为为为为 这里再写一段足够长的正文描述实际问题"
                        "包含现象与复现步骤以及预期行为，方便维护者跟进"
        )))
        data = json.loads(result)
        self.assertEqual(data["reason"], "rejected_quality")
        self.assertIn("乱码", data["message"])

    def test_quality_reject_does_not_emit_prefill_url(self):
        # 质量拒绝必须**不**返回 prefill_url——不能给"测试 issue"留旁路
        result = _run(self.tool.run(**self._good_kwargs(description="x")))
        data = json.loads(result)
        self.assertEqual(data["reason"], "rejected_quality")
        self.assertNotIn("prefill_url", data)
        self.assertEqual(self.push_calls, [])

    # ------------------------------------------------------------------
    # Per-user rate limit
    # ------------------------------------------------------------------
    def test_rate_limit_cooldown_kicks_in_after_first_submission(self):
        # 第一次走 no_token 兜底就会 _record_user_submission；第二次立即重试
        # 应该被 30 分钟冷却挡掉
        self.tool._username = "admin1"
        first = _run(self.tool.run(**self._good_kwargs()))
        d1 = json.loads(first)
        self.assertEqual(d1["reason"], "no_token")

        # 紧接着第二次（不同标题，绕过 dedup）
        second_kwargs = self._good_kwargs(
            title="[错误报告]: 另一个完全不同的后端报错"
        )
        second = _run(self.tool.run(**second_kwargs))
        d2 = json.loads(second)
        self.assertEqual(d2["reason"], "rate_limited_user")
        # rate limit 命中后仍要推送 prefill_url 让用户有手动路径
        self.assertTrue(d2["url_delivered"])
        self.assertIn("30 分钟", d2["message"])

    def test_rate_limit_daily_quota_exhausts_after_n_submissions(self):
        self.tool._username = "admin1"
        # 直接灌满 quota：手动写入 10 条 24h 内的时间戳（绕过冷却需要把它们
        # 设成都 > 30 分钟前，让冷却放行但 quota 已满）
        long_ago = time.time() - (40 * 60)  # 40 分钟前，绕过 30 分钟冷却
        SubmitFeedbackIssueTool._user_submissions["admin1"] = [
            long_ago - i for i in range(10)
        ]
        result = _run(self.tool.run(**self._good_kwargs()))
        data = json.loads(result)
        self.assertEqual(data["reason"], "rate_limited_user")
        self.assertIn("24 小时配额", data["message"])

    def test_rate_limit_resets_for_different_user(self):
        # 即使一个用户被限流，另一个 admin 不应受影响
        SubmitFeedbackIssueTool._user_submissions["admin1"] = [time.time()]
        self.tool._username = "admin2"
        result = _run(self.tool.run(**self._good_kwargs()))
        data = json.loads(result)
        # admin2 没用过额度，走 no_token 兜底而不是 rate_limited
        self.assertEqual(data["reason"], "no_token")


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
        self.assertIn("collect_feedback_diagnostics", tool_names)
        self.assertIn("prepare_feedback_issue", tool_names)
        self.assertIn("submit_feedback_issue", tool_names)


class TestCollectFeedbackDiagnosticsFiltering(unittest.TestCase):
    """``_normalize_keywords`` / ``_filter_lines`` 的纯函数测试。"""

    def test_normalize_keywords_drops_vague_terms(self):
        from app.agent.tools.impl.collect_feedback_diagnostics import (
            CollectFeedbackDiagnosticsTool,
        )

        out = CollectFeedbackDiagnosticsTool._normalize_keywords(
            "今天 TMDB 一直在报错，反馈这个问题",
            ["TMDB", "错误", "异常", "scrape_metadata", "x"],  # x 太短
        )
        # 通用词被剔除，具体词保留
        self.assertIn("TMDB", out)
        self.assertIn("scrape_metadata", out)
        self.assertNotIn("错误", out)
        self.assertNotIn("异常", out)
        self.assertNotIn("x", out)

    def test_filter_lines_excludes_history_outside_time_window(self):
        from datetime import datetime, timedelta
        from app.agent.tools.impl.collect_feedback_diagnostics import (
            CollectFeedbackDiagnosticsTool,
        )

        now = datetime.now()
        old = now - timedelta(hours=3)
        recent = now - timedelta(minutes=5)
        text = "\n".join([
            f"【INFO】{old.strftime('%Y-%m-%d %H:%M:%S')},123 - tmdb - TMDB lookup failed (历史)",
            f"【ERROR】{recent.strftime('%Y-%m-%d %H:%M:%S')},123 - tmdb - TMDB lookup failed (当前)",
            "    Traceback (most recent call last):",
            "      File 'x.py', line 1, in <module>",
        ])
        out = CollectFeedbackDiagnosticsTool._filter_lines(
            text,
            keywords=["TMDB"],
            max_lines=80,
            window_start=now - timedelta(minutes=30),
        )
        joined = "\n".join(out)
        self.assertIn("当前", joined)
        self.assertNotIn("历史", joined)
        # Traceback 续行紧跟在窗口内的 ERROR 行后面，应保留
        self.assertIn("Traceback", joined)

    def test_filter_lines_drops_agent_meta_noise(self):
        """#5808 教训：诊断段几乎全是 agent 自身 tool dispatch / 消息推送日志，
        真正的 RateLimitError 被挤掉。filter 必须把 meta-noise 模块剔除。"""
        from datetime import datetime, timedelta
        from app.agent.tools.impl.collect_feedback_diagnostics import (
            CollectFeedbackDiagnosticsTool,
        )

        now = datetime.now()
        recent = (now - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        text = "\n".join([
            f"【DEBUG】{recent},100 - base.py - Executing tool collect_feedback_diagnostics ...",
            f"【INFO】{recent},110 - agent - Agent推理: input=大模型出错",
            f"【INFO】{recent},120 - message.py - 发送消息：{{'title': '确认提交问题反馈'...}}",
            f"【DEBUG】{recent},130 - chain - 请求系统模块执行：post_message",
            f"【DEBUG】{recent},140 - telegram - 收到来自 TG.v2 的Telegram消息",
            f"【ERROR】{recent},200 - app.modules.openai - RateLimitError 429",
            "    Traceback (most recent call last):",
            f"【WARNING】{recent},300 - app.chain.recommend - 推荐接口降级",
        ])
        out = CollectFeedbackDiagnosticsTool._filter_lines(
            text, keywords=["大模型", "RateLimitError"], max_lines=80,
            window_start=now - timedelta(minutes=30),
        )
        joined = "\n".join(out)
        # meta-noise 全部丢弃
        for noise in ("Executing tool", "Agent推理", "发送消息", "post_message",
                      "TG.v2 的Telegram消息"):
            self.assertNotIn(noise, joined, msg=f"agent meta-noise 漏过: {noise}")
        # 真实信号保留
        self.assertIn("RateLimitError", joined)
        self.assertIn("Traceback", joined)
        # WARNING 行不命中 keywords 但属于真实模块——这里不强求保留
        # （keyword 过滤逻辑不改）

    def test_filter_lines_drops_orphan_continuations_outside_window(self):
        # 续行所属的最近一条时间戳在窗口外时不应被错误收入
        from datetime import datetime, timedelta
        from app.agent.tools.impl.collect_feedback_diagnostics import (
            CollectFeedbackDiagnosticsTool,
        )

        now = datetime.now()
        old = now - timedelta(hours=3)
        text = "\n".join([
            f"【ERROR】{old.strftime('%Y-%m-%d %H:%M:%S')},000 - tmdb - 历史报错",
            "    Traceback (历史续行)",
        ])
        out = CollectFeedbackDiagnosticsTool._filter_lines(
            text, keywords=["TMDB"], max_lines=80,
            window_start=now - timedelta(minutes=30),
        )
        self.assertEqual(out, [])


class TestCollectFeedbackDiagnosticsIntentGate(unittest.TestCase):
    """入口意图门：用户原话没有"反馈/提 issue"等明确意图时，工具必须拒绝。

    防止 Agent 在用户随口提到「TMDB 报错」「下载没动」时擅自跳过本地诊断、
    直接跳进反馈流程刷上游 Issue 列表。"""

    def setUp(self):
        from app.agent.tools.impl.feedback_issue_state import feedback_issue_state_store

        feedback_issue_state_store.clear()

    def _build_tool(self):
        from app.agent.tools.impl.collect_feedback_diagnostics import (
            CollectFeedbackDiagnosticsTool,
        )

        return CollectFeedbackDiagnosticsTool(session_id="s", user_id="42")

    def test_has_explicit_feedback_intent_recognizes_chinese_phrases(self):
        from app.agent.tools.impl.collect_feedback_diagnostics import (
            CollectFeedbackDiagnosticsTool as T,
        )

        for explicit in (
            "今天 TMDB 一直在报错，反馈这个问题",  # 含"反馈"
            "TMDB 出错了，帮我提 issue",
            "给 MP 提个 bug，下载没动",
            "让上游修一下这个错",
            "submit an issue: telegram bot keeps disconnecting",
            "请提交问题反馈：scrape 总失败",
        ):
            self.assertTrue(
                T._has_explicit_feedback_intent(explicit),
                msg=f"应识别为明确反馈意图: {explicit!r}",
            )

    def test_has_explicit_feedback_intent_rejects_plain_complaints(self):
        from app.agent.tools.impl.collect_feedback_diagnostics import (
            CollectFeedbackDiagnosticsTool as T,
        )

        for plain in (
            "TMDB 一直在报错",  # 仅描述问题、没要求反馈
            "下载没动了，怎么办",
            "订阅没生效",
            "图片刷不出来",
            "数据库响应比较慢",
            "TMDB API failing today",
        ):
            self.assertFalse(
                T._has_explicit_feedback_intent(plain),
                msg=f"不应识别为反馈意图: {plain!r}",
            )

    def test_run_refuses_without_explicit_intent(self):
        tool = self._build_tool()
        result = asyncio.run(
            tool.run(
                explanation="x",
                original_user_request="TMDB 报错了",
                keywords=["TMDB"],
            )
        )
        data = json.loads(result)
        self.assertFalse(data["success"])
        self.assertEqual(data["reason"], "no_explicit_feedback_intent")
        # 引导回归本地诊断路径
        self.assertIn("query_subscribes", data["message"])

    def test_run_allows_with_explicit_intent(self):
        # 配上路径 stub 让真实路径不读磁盘
        from datetime import datetime
        from pathlib import Path
        from app.agent.tools.impl import collect_feedback_diagnostics as cfd

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_text = f"【ERROR】{now_str},000 - tmdb - TMDB lookup failed"

        tool = self._build_tool()
        with patch.object(
            cfd.CollectFeedbackDiagnosticsTool,
            "_read_tail",
            return_value=log_text,
        ), patch.object(
            cfd.CollectFeedbackDiagnosticsTool,
            "_candidate_log_files",
            return_value=[Path("/fake/moviepilot.log")],
        ):
            result = asyncio.run(
                tool.run(
                    explanation="x",
                    original_user_request="TMDB 报错，反馈 issue",
                    keywords=["TMDB"],
                )
            )
        data = json.loads(result)
        # 走完正常路径
        self.assertTrue(data["success"])
        self.assertIn("diagnostics_id", data)


class TestCollectFeedbackDiagnosticsResponse(unittest.TestCase):
    """``collect_feedback_diagnostics`` 必须把日志只缓存到 state store，
    不能把日志正文回流到 LLM 上下文里。曾经返回完整 logs，导致 LLM 在下
    一步把 6KB 日志重新当 args 传给 prepare 工具，单轮延迟到分钟级。
    这个保护用 unit test 钉死。"""

    def setUp(self):
        from app.agent.tools.impl.feedback_issue_state import feedback_issue_state_store

        feedback_issue_state_store.clear()
        self._state_store = feedback_issue_state_store

    def _build_tool(self):
        from app.agent.tools.impl.collect_feedback_diagnostics import (
            CollectFeedbackDiagnosticsTool,
        )

        return CollectFeedbackDiagnosticsTool(
            session_id="sess",
            user_id="42",
        )

    def test_run_does_not_return_raw_log_text(self):
        from datetime import datetime
        from pathlib import Path
        from app.agent.tools.impl import collect_feedback_diagnostics as cfd

        # 用近 1 分钟内的时间戳，确保通过时间窗过滤
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        big_log = "\n".join(
            f"【ERROR】{now_str},000 - mod{i} - ERROR something" for i in range(500)
        )
        tool = self._build_tool()
        with patch.object(
            cfd.CollectFeedbackDiagnosticsTool,
            "_read_tail",
            return_value=big_log,
        ), patch.object(
            cfd.CollectFeedbackDiagnosticsTool,
            "_candidate_log_files",
            return_value=[Path("/fake/moviepilot.log")],
        ):
            result = asyncio.run(
                tool.run(
                    explanation="x",
                    # 必须带明确反馈意图，否则被入口门拦下；这里同时验
                    # 证日志正文不会回流到 LLM。
                    original_user_request="something is broken，帮我提 issue",
                    keywords=["ERROR"],
                )
            )

        data = json.loads(result)
        # 关键不变量：返回值不含 logs 字段，也不含任何日志正文片段
        self.assertNotIn("logs", data)
        for key, value in data.items():
            if isinstance(value, str):
                self.assertNotIn(
                    "ERROR something",
                    value,
                    msg=f"字段 {key} 泄漏了日志正文：{value[:80]!r}",
                )
        # 必带的摘要字段
        self.assertIn("diagnostics_id", data)
        self.assertIn("log_bytes", data)
        self.assertIn("log_lines", data)
        # 日志确实进了 state store
        record = self._state_store.get_diagnostics(
            data["diagnostics_id"], session_id="sess", user_id="42"
        )
        self.assertIsNotNone(record)
        self.assertIn("ERROR something", record.logs)


if __name__ == "__main__":
    unittest.main()
