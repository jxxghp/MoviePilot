"""
浏览器页面、元素及上下文替身；不注册测试用例。
"""

from __future__ import annotations
import threading
from typing import Optional


class _FakeResponse:
    """
    模拟浏览器导航响应。
    """

    status = 200


class _FakeElement:
    """
    模拟页面元素。
    """

    def is_visible(self) -> bool:
        """
        返回元素可见状态。
        """
        return True

    def fill(self, value: str) -> None:
        """
        记录输入值。
        """
        self.value = value

    def inner_text(self) -> str:
        """
        返回元素文本。
        """
        return "元素文本"


class _FakePage:
    """
    模拟 CloakBrowser 页面对象。
    """

    def __init__(self, page_id: str = "page-1") -> None:
        """
        初始化本用例独立持有的替身状态与可观察记录。

        :param page_id: 页面标识
        """
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
        """
        记录额外请求头。
        """
        self.headers = headers

    def set_default_timeout(self, timeout: int) -> None:
        """
        记录默认超时时间。
        """
        self.timeout = timeout

    def goto(self, url: str, *args, **kwargs) -> _FakeResponse:
        """
        记录导航目标。
        """
        self.loaded_url = url
        self.url = url
        return _FakeResponse()

    def wait_for_load_state(self, _state: str, timeout: int) -> None:
        """
        记录页面等待超时。
        """
        self.timeout = timeout

    def wait_for_selector(self, selector: str, *args, **kwargs) -> _FakeElement:
        """
        返回模拟元素。
        """
        self.waited_selector = selector
        return _FakeElement()

    def fill(self, selector: str, value: str, *args, **kwargs) -> None:
        """
        记录表单输入。
        """
        self.fills.append((selector, value))

    def click(self, selector: str, *args, **kwargs) -> None:
        """
        记录点击选择器。
        """
        self.clicks.append(selector)

    def select_option(self, selector: str, *args, **kwargs) -> None:
        """
        记录下拉选择。
        """
        self.selects.append((selector, kwargs.get("value")))

    def query_selector(self, selector: str) -> _FakeElement:
        """
        返回模拟元素。
        """
        self.queried_selector = selector
        return _FakeElement()

    def title(self) -> str:
        """
        返回页面标题。
        """
        return f"标题 {self.page_id}"

    def inner_text(self, selector: str) -> str:
        """
        返回页面文本。
        """
        return f"正文 {self.page_id}"

    def content(self) -> str:
        """
        返回页面源码。
        """
        return "<html>ok</html>"

    def evaluate(self, expression: str, *args, **kwargs):
        """
        返回可交互元素或脚本结果。
        """
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
        """
        返回模拟截图内容。
        """
        return b"image"

    def close(self) -> None:
        """
        记录页面关闭状态。
        """
        self.close_thread_id = threading.get_ident()
        self.closed = True


class _FakeContext:
    """
    模拟 CloakBrowser 上下文。
    """

    def __init__(
        self,
        pages: Optional[list[_FakePage]] = None,
        cookies: Optional[list[dict]] = None,
    ) -> None:
        """
        初始化本用例独立持有的替身状态与可观察记录。

        :param pages: 上下文持有的模拟页面
        :param cookies: 上下文返回的 Cookie 数据
        """
        self.pages = pages or [_FakePage()]
        self.cookie_values = cookies or []
        self.closed = False
        self.close_thread_id = None

    def new_page(self) -> _FakePage:
        """
        返回或创建模拟页面。
        """
        if self.pages:
            return self.pages.pop(0)
        return _FakePage("extra")

    def cookies(self) -> list[dict]:
        """
        返回预设 Cookie 列表。
        """
        return list(self.cookie_values)

    def close(self) -> None:
        """
        记录上下文关闭状态。
        """
        self.close_thread_id = threading.get_ident()
        self.closed = True
