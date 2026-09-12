from __future__ import annotations

import asyncio
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from types import ModuleType
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from app.adapters.network.browser import (
    BrowserSessionHelper,
    PlaywrightHelper,
    _BrowserSessionState,
    launch_browser_context,
    launch_browser_context_async,
)
from app.agent.terminal.ownership import TerminalScope, close_terminal_scope
from app.agent.tools.impl.browse_webpage import BrowserAction, BrowseWebpageTool
from app.runtime.correlation import correlation_scope, get_correlation_id


class _FakeResponse:
    """模拟浏览器导航响应。"""

    status = 200


class _FakeElement:
    """模拟页面元素。"""

    def is_visible(self) -> bool:
        """返回元素可见状态。"""
        return True

    def fill(self, value: str) -> None:
        """记录输入值。"""
        self.value = value

    def inner_text(self) -> str:
        """返回元素文本。"""
        return "元素文本"


class _FakePage:
    """模拟 CloakBrowser 页面对象。"""

    def __init__(self, page_id: str = "page-1") -> None:
        self.page_id = page_id
        self.headers = None
        self.loaded_url = ""
        self.url = "about:blank"
        self.closed = False
        self.timeout = None
        self.clicks = []
        self.fills = []
        self.selects = []
        self.close_thread_id = None

    def set_extra_http_headers(self, headers: dict[str, str]) -> None:
        """记录额外请求头。"""
        self.headers = headers

    def set_default_timeout(self, timeout: int) -> None:
        """记录默认超时时间。"""
        self.timeout = timeout

    def goto(self, url: str, *args, **kwargs) -> _FakeResponse:
        """记录导航目标。"""
        self.loaded_url = url
        self.url = url
        return _FakeResponse()

    def wait_for_load_state(self, _state: str, timeout: int) -> None:
        """记录页面等待超时。"""
        self.timeout = timeout

    def wait_for_selector(self, selector: str, *args, **kwargs) -> _FakeElement:
        """返回模拟元素。"""
        self.waited_selector = selector
        return _FakeElement()

    def fill(self, selector: str, value: str, *args, **kwargs) -> None:
        """记录表单输入。"""
        self.fills.append((selector, value))

    def click(self, selector: str, *args, **kwargs) -> None:
        """记录点击选择器。"""
        self.clicks.append(selector)

    def select_option(self, selector: str, *args, **kwargs) -> None:
        """记录下拉选择。"""
        self.selects.append((selector, kwargs.get("value")))

    def query_selector(self, selector: str) -> _FakeElement:
        """返回模拟元素。"""
        self.queried_selector = selector
        return _FakeElement()

    def title(self) -> str:
        """返回页面标题。"""
        return f"标题 {self.page_id}"

    def inner_text(self, selector: str) -> str:
        """返回页面文本。"""
        return f"正文 {self.page_id}"

    def content(self) -> str:
        """返回页面源码。"""
        return "<html>ok</html>"

    def evaluate(self, expression: str, *args, **kwargs):
        """返回可交互元素或脚本结果。"""
        if "data-moviepilot-agent-ref" in expression:
            return [
                {
                    "ref": "e1",
                    "tag": "button",
                    "type": "button",
                    "text": "保存",
                    "name": "",
                    "id": "save",
                    "role": "",
                    "placeholder": "",
                    "href": "",
                    "value": "",
                    "selector": '[data-moviepilot-agent-ref="e1"]',
                }
            ]
        return {"ok": True}

    def screenshot(self, *args, **kwargs) -> bytes:
        """返回模拟截图内容。"""
        return b"image"

    def close(self) -> None:
        """记录页面关闭状态。"""
        self.close_thread_id = threading.get_ident()
        self.closed = True


class _FakeContext:
    """模拟 CloakBrowser 上下文。"""

    def __init__(
        self,
        pages: Optional[list[_FakePage]] = None,
        cookies: Optional[list[dict]] = None,
    ) -> None:
        self.pages = pages or [_FakePage()]
        self.cookie_values = cookies or []
        self.closed = False
        self.close_thread_id = None

    def new_page(self) -> _FakePage:
        """返回或创建模拟页面。"""
        if self.pages:
            return self.pages.pop(0)
        return _FakePage("extra")

    def cookies(self) -> list[dict]:
        """返回预设 Cookie 列表。"""
        return list(self.cookie_values)

    def close(self) -> None:
        """记录上下文关闭状态。"""
        self.close_thread_id = threading.get_ident()
        self.closed = True


@pytest.fixture(autouse=True)
def browser_sessions_cleanup():
    """确保每个测试后清理浏览器会话。"""
    BrowserSessionHelper.close_all_sessions()
    yield
    BrowserSessionHelper.close_all_sessions()


def test_default_emulation_uses_cloakbrowser_context():
    """默认浏览器仿真应使用 CloakBrowser 上下文。"""
    page = _FakePage()
    context = _FakeContext([page])

    with patch(
        "app.adapters.network.browser.get_runtime_setting",
        return_value="cloakbrowser",
    ), patch.object(
        PlaywrightHelper,
        "_PlaywrightHelper__launch_cloakbrowser_context",
        return_value=context,
    ) as launch_context:
        source = PlaywrightHelper().get_page_source(
            url="https://example.com",
            cookies="uid=1",
            ua="UA",
            timeout=3,
        )

    assert source == "<html>ok</html>"
    launch_context.assert_called_once_with(
        headless=False,
        user_agent="UA",
        proxies=None,
    )
    assert page.headers == {"cookie": "uid=1"}
    assert page.loaded_url == "https://example.com"
    assert page.closed
    assert context.closed


def test_legacy_playwright_emulation_uses_cloakbrowser_context():
    """兼容旧 Playwright 仿真配置。"""
    page = _FakePage()
    context = _FakeContext([page])

    with patch(
        "app.adapters.network.browser.get_runtime_setting",
        return_value="Playwright",
    ), patch.object(
        PlaywrightHelper,
        "_PlaywrightHelper__launch_cloakbrowser_context",
        return_value=context,
    ):
        source = PlaywrightHelper().get_page_source(url="https://example.com")

    assert source == "<html>ok</html>"


def test_legacy_browser_type_constructor_is_accepted():
    """旧版 browser_type 构造参数应保持兼容。"""
    page = _FakePage()
    context = _FakeContext([page])

    with patch.object(
        PlaywrightHelper,
        "_PlaywrightHelper__launch_cloakbrowser_context",
        return_value=context,
    ):
        source = PlaywrightHelper(browser_type="firefox").get_page_source(
            url="https://example.com"
        )

    assert source == "<html>ok</html>"


def test_browser_action_runs_callback_when_network_never_becomes_idle():
    """页面 DOM 已就绪时，持续后台请求不得阻断登录等页面回调。"""
    page = _FakePage()
    page.wait_for_load_state = MagicMock(side_effect=TimeoutError("still busy"))
    context = _FakeContext([page])

    with patch(
        "app.adapters.network.browser.get_runtime_setting",
        return_value="cloakbrowser",
    ), patch.object(
        PlaywrightHelper,
        "_PlaywrightHelper__launch_cloakbrowser_context",
        return_value=context,
    ):
        result = PlaywrightHelper().action(
            url="https://example.com",
            callback=lambda current_page: current_page.loaded_url,
            timeout=30,
        )

    assert result == "https://example.com"
    assert page.loaded_url == "https://example.com"
    assert page.wait_for_load_state.call_args == call("networkidle", timeout=15000)
    assert page.closed
    assert context.closed


def test_sync_browser_facade_activates_display_only_for_headed_mode(monkeypatch):
    """同步启动仅在明确有界面模式获取 host.display，参数原样交给浏览器。"""
    provider = ModuleType("cloakbrowser")
    launch_context = MagicMock(return_value=object())
    provider.launch_context = launch_context
    monkeypatch.setitem(sys.modules, "cloakbrowser", provider)
    activate = MagicMock()
    monkeypatch.setattr(
        "app.adapters.network.browser.acquire_managed_resource",
        activate,
    )

    headless_context = launch_browser_context(headless=True, locale="zh-CN")
    headed_context = launch_browser_context(headless=False, locale="zh-CN")

    assert headless_context is launch_context.return_value
    assert headed_context is launch_context.return_value
    activate.assert_called_once_with(
        "host.display",
        reason="headed_browser_launch",
        retry=True,
    )
    assert launch_context.call_args_list == [
        call(headless=True, locale="zh-CN"),
        call(headless=False, locale="zh-CN"),
    ]


def test_async_browser_facade_waits_for_display_before_provider(monkeypatch):
    """异步有界面启动必须等待显示资源完成激活后再创建浏览器上下文。"""
    events: list[str] = []
    provider = ModuleType("cloakbrowser")

    async def provider_launch(**_kwargs):
        events.append("provider")
        return object()

    provider.launch_context_async = provider_launch
    monkeypatch.setitem(sys.modules, "cloakbrowser", provider)

    async def activate(*_args, **_kwargs):
        events.append("display")

    monkeypatch.setattr(
        "app.adapters.network.browser.acquire_managed_resource_async",
        AsyncMock(side_effect=activate),
    )

    asyncio.run(launch_browser_context_async(headless=False, timezone="Asia/Shanghai"))

    assert events == ["display", "provider"]


def test_browser_session_helper_blocks_private_network_by_default():
    """默认应阻止 Agent 浏览器访问本机或私网地址。"""
    with pytest.raises(ValueError, match="默认不允许访问本机或私网地址"):
        BrowserSessionHelper.validate_url("http://127.0.0.1:3000")


def test_browser_session_helper_allows_private_network_when_explicit():
    """显式允许时可访问本机或私网地址。"""
    assert (
        BrowserSessionHelper.validate_url(
            "http://127.0.0.1:3000",
            allow_private_network=True,
        )
        == "http://127.0.0.1:3000"
    )


def test_browser_session_helper_reuses_page_within_session():
    """同一 session_key 应复用同一个浏览器页面。"""
    page = _FakePage()
    context = _FakeContext([page])

    with patch.object(BrowserSessionHelper, "_launch_context", return_value=context):
        helper = BrowserSessionHelper()
        first = helper.with_session("session-1", lambda session: id(session.active_page))
        second = helper.with_session("session-1", lambda session: id(session.active_page))

    assert first == second
    assert not page.closed
    assert not context.closed


def test_browser_session_helper_runs_same_session_on_one_worker_thread():
    """同一 session_key 的浏览器操作应固定在同一个工作线程。"""
    page = _FakePage()
    context = _FakeContext([page])
    helper = BrowserSessionHelper()
    caller_thread_ids = set()
    session_thread_ids = []
    barrier = threading.Barrier(2)

    def _run_from_caller_thread() -> int:
        """从外部调用线程进入同一个浏览器会话。"""
        caller_thread_ids.add(threading.get_ident())
        barrier.wait(timeout=1)
        return helper.with_session(
            "session-1",
            lambda _session: threading.get_ident(),
        )

    with patch.object(BrowserSessionHelper, "_launch_context", return_value=context):
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(_run_from_caller_thread),
                executor.submit(_run_from_caller_thread),
            ]
            session_thread_ids = [future.result(timeout=1) for future in futures]

    assert len(caller_thread_ids) == 2
    assert len(set(session_thread_ids)) == 1
    assert session_thread_ids[0] not in caller_thread_ids


def test_browser_session_helper_preserves_each_call_context():
    """会话固定线程必须使用每次操作的上下文，不能保留首次请求状态。"""
    page = _FakePage()
    context = _FakeContext([page])
    helper = BrowserSessionHelper()
    observed = []

    with patch.object(BrowserSessionHelper, "_launch_context", return_value=context):
        for correlation_id in ("request-one", "request-two"):
            with correlation_scope(correlation_id):
                observed.append(
                    helper.with_session("session-1", lambda _session: get_correlation_id())
                )

    assert observed == ["request-one", "request-two"]


def test_browser_session_helper_closes_session_on_worker_thread():
    """关闭会话时应在创建浏览器对象的工作线程内释放资源。"""
    page = _FakePage()
    context = _FakeContext([page])
    helper = BrowserSessionHelper()

    with patch.object(BrowserSessionHelper, "_launch_context", return_value=context):
        session_thread_id = helper.with_session(
            "session-1",
            lambda _session: threading.get_ident(),
        )
        closed = BrowserSessionHelper.close_session("session-1")

    assert closed is True
    assert page.close_thread_id == session_thread_id
    assert context.close_thread_id == session_thread_id


def test_browser_session_helper_isolates_owner_and_closes_owner_sessions():
    """相同模型 session_key 在不同宿主作用域下不能串用，owner 收口会关闭上下文。"""
    first_page = _FakePage("first")
    second_page = _FakePage("second")
    contexts = [_FakeContext([first_page]), _FakeContext([second_page])]
    helper = BrowserSessionHelper()
    first_owner = TerminalScope("alice", "task-one", "conversation")
    second_owner = TerminalScope("alice", "task-two", "conversation")

    with patch.object(BrowserSessionHelper, "_launch_context", side_effect=contexts):
        assert helper.with_session("shared", lambda session: session.owner, owner=first_owner) is first_owner
        with pytest.raises(PermissionError):
            helper.with_session("shared", lambda _session: None, owner=second_owner)
        assert BrowserSessionHelper.close_owner(first_owner)

    assert first_page.closed and first_page.page_id == "first"
    assert contexts[0].closed
    with patch.object(BrowserSessionHelper, "_launch_context", return_value=contexts[1]):
        assert helper.with_session("shared", lambda session: session.owner, owner=second_owner) is second_owner
    assert BrowserSessionHelper.close_owner(second_owner)
    assert second_page.closed and contexts[1].closed


def test_browser_session_helper_rejects_closed_owner_before_context_creation():
    """作用域封口后不得重新创建或复用浏览器上下文。"""
    owner = TerminalScope("alice", "closed-browser-task", "conversation")
    owner.seal()
    with patch.object(BrowserSessionHelper, "_launch_context") as launch_context:
        with pytest.raises(PermissionError, match="所属任务已停止"):
            BrowserSessionHelper().with_session(
                "closed", lambda _session: None, owner=owner
            )
    launch_context.assert_not_called()


@pytest.mark.asyncio
async def test_close_terminal_scope_closes_browser_owner_without_blocking_event_loop():
    """通用作用域收口应回收其浏览器上下文，且通过线程避免阻塞事件循环。"""
    page = _FakePage("owned")
    context = _FakeContext([page])
    helper = BrowserSessionHelper()
    owner = TerminalScope("alice", "browser-task", "conversation")
    with patch.object(BrowserSessionHelper, "_launch_context", return_value=context):
        helper.with_session("owned", lambda session: session.active_page, owner=owner)
        assert await close_terminal_scope(owner)
    assert page.closed and context.closed


def test_browse_webpage_returns_snapshot_with_refs_after_goto():
    """goto 后应返回包含可交互元素 ref 的页面快照。"""
    page = _FakePage()
    context = _FakeContext([page])
    tool = BrowseWebpageTool(session_id="session-1", user_id="10001")

    with patch.object(BrowserSessionHelper, "_launch_context", return_value=context):
        result = tool._execute_browser_action(
            browser_action=BrowserAction.GOTO,
            url="https://example.com",
            selector=None,
            ref=None,
            value=None,
            script=None,
            content_type="text",
            timeout=3,
            cookies=None,
            user_agent=None,
            session_key="session-1",
            tab_index=None,
            allow_private_network=False,
        )

    payload = json.loads(result)
    assert payload["url"] == "https://example.com"
    assert payload["interactive_elements"][0]["ref"] == "e1"


def test_snapshot_clears_previous_refs_before_assigning_new_refs() -> None:
    """新快照应清理旧 ref，避免动态页面把过期引用映射到新元素。"""
    page = MagicMock()
    page.evaluate.return_value = [{"ref": "e1", "tag": "button"}]
    assert BrowserSessionHelper._extract_interactive_elements(page, max_elements=5)
    script = page.evaluate.call_args.args[0]
    assert "removeAttribute('data-moviepilot-agent-ref')" in script


def test_browse_webpage_click_ref_uses_snapshot_selector():
    """click_ref 应将 ref 转换为快照注入的稳定选择器。"""
    page = _FakePage()
    context = _FakeContext([page])
    tool = BrowseWebpageTool(session_id="session-1", user_id="10001")

    with patch.object(BrowserSessionHelper, "_launch_context", return_value=context):
        result = tool._execute_browser_action(
            browser_action=BrowserAction.CLICK_REF,
            url=None,
            selector=None,
            ref="e1",
            value=None,
            script=None,
            content_type="text",
            timeout=3,
            cookies=None,
            user_agent=None,
            session_key="session-1",
            tab_index=None,
            allow_private_network=False,
        )

    payload = json.loads(result)
    assert payload["success"] is True
    assert page.clicks == ['[data-moviepilot-agent-ref="e1"]']


def test_close_tab_before_active_page_keeps_active_index() -> None:
    """关闭活动页之前的标签页不能把活动页错误地指向下一页。"""
    first_page = _FakePage("first")
    pages = [first_page, _FakePage("active"), _FakePage("last")]
    context = _FakeContext(pages)
    session = _BrowserSessionState("close-index", context, pages, active_index=1)
    tabs = BrowserSessionHelper.close_tab(session, 0)
    assert session.active_index == 0
    assert session.active_page.page_id == "active"
    assert tabs[0]["active"] is True
    assert first_page.closed is True


def test_close_tab_failure_is_reported_as_unknown_state() -> None:
    """关闭动作异常时实际状态可能已发生，必须要求先核验而非盲目重试。"""
    page = _FakePage("close-error")
    page.close = MagicMock(side_effect=RuntimeError("provider failure"))
    context = _FakeContext([page, _FakePage("remaining")])
    session = _BrowserSessionState("close-error", context, context.pages, active_index=0)
    tool = BrowseWebpageTool(session_id="session-1", user_id="10001")

    with patch.object(
        BrowserSessionHelper,
        "with_session",
        side_effect=lambda callback, **_kwargs: callback(session),
    ):
        result = tool._execute_browser_action(
            browser_action=BrowserAction.CLOSE_TAB, url=None, selector=None, ref=None, value=None,
            script=None, content_type="text", timeout=3, cookies=None, user_agent=None,
            session_key="close-error", tab_index=0, allow_private_network=False,
        )
    payload = json.loads(result)
    assert payload["execution_outcome"] == "unknown"
    assert "list_tabs" in payload["recovery"]


def test_click_redirect_to_private_url_is_unknown_and_requires_recheck() -> None:
    """点击后的私网重定向不能被当作成功，且不能诱导重复点击。"""
    page = _FakePage("redirect")

    def redirect(_selector: str, *_args, **_kwargs) -> None:
        """模拟点击后跳转到未授权的私网地址。"""
        page.url = "http://127.0.0.1:1234/private"

    page.click = redirect
    context = _FakeContext([page])
    session = _BrowserSessionState("redirect", context, context.pages, active_index=0)
    tool = BrowseWebpageTool(session_id="session-1", user_id="10001")
    with patch.object(
        BrowserSessionHelper,
        "with_session",
        side_effect=lambda callback, **_kwargs: callback(session),
    ):
        result = tool._execute_browser_action(
            browser_action=BrowserAction.CLICK, url=None, selector="#go", ref=None, value=None,
            script=None, content_type="text", timeout=3, cookies=None, user_agent=None,
            session_key="redirect", tab_index=None, allow_private_network=False,
        )
    payload = json.loads(result)
    assert payload["execution_outcome"] == "unknown"
    assert "snapshot" in payload["recovery"]


def test_browse_webpage_get_cookies_returns_current_domain_cookie_and_ua():
    """管理员 Cookie 动作应只返回当前页面域名的会话字段。"""
    page = _FakePage()
    page.url = "https://tracker.example/path"
    context = _FakeContext(
        [page],
        cookies=[
            {"name": "sid", "value": "browser", "domain": "tracker.example"},
            {"name": "other", "value": "hidden", "domain": "other.example"},
        ],
    )
    session = type(
        "Session",
        (),
        {"context": context, "active_page": page, "cookies": "seed=1", "user_agent": "UA"},
    )()

    payload = json.loads(BrowseWebpageTool._action_get_cookies(session, page))

    assert payload["success"] is True
    assert payload["cookie"] == "seed=1; sid=browser"
    assert {item["name"] for item in payload["cookies"]} == {"seed", "sid"}
    assert payload["user_agent"] == "UA"


@pytest.mark.asyncio
async def test_browse_webpage_get_cookies_is_admin_only(monkeypatch: pytest.MonkeyPatch):
    """普通调用方不得通过浏览器动作读取认证 Cookie。"""
    tool = BrowseWebpageTool(session_id="session-1", user_id="10001")
    monkeypatch.setattr(
        BrowseWebpageTool,
        "is_admin_user",
        AsyncMock(return_value=False),
    )

    result = await tool.run(action="get_cookies")

    assert "仅允许管理员" in result
