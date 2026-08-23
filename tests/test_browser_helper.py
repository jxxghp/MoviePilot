from __future__ import annotations

import json
import asyncio
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from types import ModuleType
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from app.agent.tools.impl.browse_webpage import BrowserAction, BrowseWebpageTool
from app.adapters.network.browser import (
    BrowserSessionHelper,
    PlaywrightHelper,
    launch_browser_context,
    launch_browser_context_async,
)
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

    def __init__(self, pages: Optional[list[_FakePage]] = None) -> None:
        self.pages = pages or [_FakePage()]
        self.closed = False
        self.close_thread_id = None

    def new_page(self) -> _FakePage:
        """返回或创建模拟页面。"""
        if self.pages:
            return self.pages.pop(0)
        return _FakePage("extra")

    def cookies(self) -> list[dict]:
        """返回空 Cookie 列表。"""
        return []

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

    with patch("app.adapters.network.browser.settings.BROWSER_EMULATION", "cloakbrowser"), patch.object(
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

    with patch("app.adapters.network.browser.settings.BROWSER_EMULATION", "Playwright"), patch.object(
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
