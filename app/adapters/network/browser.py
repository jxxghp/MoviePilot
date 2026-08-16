import ipaddress
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol
from urllib.parse import urlparse

from app.runtime.config import settings
from app.runtime.log import logger
from app.runtime.managed_resources import (
    acquire_managed_resource,
    acquire_managed_resource_async,
)
from app.adapters.network.http import RequestUtils, cookie_parse


class BrowserElement(Protocol):
    """
    页面元素的最小接口，避免为了类型标注直接导入 Playwright。
    """

    def is_visible(self) -> bool:
        """判断元素是否可见。"""
        ...

    def fill(self, value: str) -> None:
        """向元素输入文本。"""
        ...

    def inner_text(self) -> str:
        """获取元素可见文本。"""
        ...


class BrowserContext(Protocol):
    """
    CloakBrowser 返回的上下文只需要满足这些能力即可。
    """

    def new_page(self) -> "BrowserPage":
        """创建新的浏览器页面。"""
        ...

    def cookies(self) -> list[dict[str, Any]]:
        """返回当前上下文 Cookie。"""
        ...

    def close(self) -> None:
        """关闭浏览器上下文。"""
        ...


class BrowserPage(Protocol):
    """
    CloakBrowser 页面对象的最小接口，覆盖 helper 和登录流程当前用到的方法。
    """

    context: BrowserContext
    url: str

    def set_extra_http_headers(self, headers: dict[str, str]) -> None:
        """设置页面额外请求头。"""
        ...

    def set_default_timeout(self, timeout: int) -> None:
        """设置页面默认超时时间。"""
        ...

    def goto(self, url: str, *args: Any, **kwargs: Any) -> Any:
        """导航到指定 URL。"""
        ...

    def wait_for_load_state(self, state: str, *args: Any, **kwargs: Any) -> Any:
        """等待页面加载状态。"""
        ...

    def wait_for_selector(self, selector: str, *args: Any, **kwargs: Any) -> Any:
        """等待指定选择器出现。"""
        ...

    def fill(self, selector: str, value: str, *args: Any, **kwargs: Any) -> Any:
        """向指定选择器输入文本。"""
        ...

    def click(self, selector: str, *args: Any, **kwargs: Any) -> Any:
        """点击指定选择器。"""
        ...

    def select_option(self, selector: str, *args: Any, **kwargs: Any) -> Any:
        """选择下拉框选项。"""
        ...

    def query_selector(self, selector: str) -> Optional[BrowserElement]:
        """查询指定选择器元素。"""
        ...

    def title(self) -> str:
        """返回页面标题。"""
        ...

    def inner_text(self, selector: str) -> str:
        """返回指定选择器的可见文本。"""
        ...

    def content(self) -> str:
        """返回页面 HTML 内容。"""
        ...

    def evaluate(self, expression: str, *args: Any, **kwargs: Any) -> Any:
        """执行页面 JavaScript 表达式。"""
        ...

    def screenshot(self, *args: Any, **kwargs: Any) -> bytes:
        """截取页面截图。"""
        ...

    def close(self) -> None:
        """关闭浏览器页面。"""
        ...


def launch_browser_context(headless: bool = True, **kwargs: Any) -> BrowserContext:
    """
    启动同步浏览器上下文；有界面模式先显式获取宿主显示资源。

    :param headless: 是否使用无头模式
    :param kwargs: 浏览器实现接受的其余启动参数
    :return: 浏览器上下文
    """
    if not headless:
        acquire_managed_resource(
            "host.display",
            reason="headed_browser_launch",
            retry=True,
        )
    from cloakbrowser import launch_context

    return launch_context(headless=headless, **kwargs)


async def launch_browser_context_async(
    headless: bool = True,
    **kwargs: Any,
) -> Any:
    """
    启动异步浏览器上下文；有界面模式等待宿主显示资源就绪。

    :param headless: 是否使用无头模式
    :param kwargs: 浏览器实现接受的其余启动参数
    :return: 浏览器上下文
    """
    if not headless:
        await acquire_managed_resource_async(
            "host.display",
            reason="headed_browser_launch",
            retry=True,
        )
    from cloakbrowser import launch_context_async

    return await launch_context_async(headless=headless, **kwargs)


@dataclass
class _BrowserSessionState:
    """保存一个可复用浏览器上下文及其页面游标。"""

    session_key: str
    context: BrowserContext
    pages: list[BrowserPage]
    active_index: int = 0
    user_agent: Optional[str] = None
    cookies: Optional[str] = None
    created_at: float = field(default_factory=time.monotonic)
    last_used_at: float = field(default_factory=time.monotonic)
    lock: threading.RLock = field(default_factory=threading.RLock)

    @property
    def active_page(self) -> BrowserPage:
        """返回会话当前选中的页面。"""
        return self.pages[self.active_index]


class BrowserSessionHelper:
    """
    Agent 浏览器会话辅助类，负责复用 CloakBrowser 上下文并生成可操作页面快照。
    """

    SESSION_TTL_SECONDS = 15 * 60
    MAX_SESSIONS = 8
    DEFAULT_VIEWPORT = {"width": 1280, "height": 720}
    PRIVATE_HOST_SUFFIXES = (".localhost", ".local", ".lan", ".home", ".internal")
    PRIVATE_HOSTNAMES = {"localhost", "ip6-localhost", "ip6-loopback"}
    REF_ATTRIBUTE = "data-moviepilot-agent-ref"
    SESSION_WORKER_NAME_PREFIX = "browser-session"

    _sessions: dict[str, _BrowserSessionState] = {}
    _session_executors: dict[str, ThreadPoolExecutor] = {}
    _session_thread_ids: dict[str, int] = {}
    _sessions_lock = threading.RLock()

    def __init__(self, headless: bool = True, viewport: Optional[dict[str, int]] = None):
        """
        初始化浏览器会话辅助类。

        :param headless: 是否使用无头浏览器
        :param viewport: 默认视口大小
        """
        self.headless = headless
        self.viewport = viewport or self.DEFAULT_VIEWPORT

    @classmethod
    def validate_url(cls, url: str, allow_private_network: bool = False) -> str:
        """
        校验浏览器可访问的 URL，默认拒绝本机、私网和非 HTTP 协议。

        :param url: 待访问的 URL
        :param allow_private_network: 是否允许访问本机或私网地址
        :return: 原始 URL
        """
        parsed = urlparse(url or "")
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("仅支持 http/https URL")
        if not parsed.hostname:
            raise ValueError("URL 缺少主机名")

        hostname = parsed.hostname.lower().rstrip(".")
        if allow_private_network:
            return url

        if hostname in cls.PRIVATE_HOSTNAMES or hostname.endswith(
            cls.PRIVATE_HOST_SUFFIXES
        ):
            raise ValueError("默认不允许访问本机或私网地址")

        try:
            ip_address = ipaddress.ip_address(hostname)
        except ValueError:
            return url

        if not ip_address.is_global:
            raise ValueError("默认不允许访问本机或私网地址")
        return url

    @classmethod
    def ref_to_selector(cls, ref: str) -> str:
        """
        将页面快照中的元素引用转换为稳定选择器。

        :param ref: 快照返回的元素引用
        :return: 可传给浏览器的属性选择器
        """
        clean_ref = (ref or "").strip()
        if not clean_ref:
            raise ValueError("元素 ref 不能为空")
        escaped_ref = clean_ref.replace("\\", "\\\\").replace('"', '\\"')
        return f'[{cls.REF_ATTRIBUTE}="{escaped_ref}"]'

    @classmethod
    def close_all_sessions(cls) -> None:
        """
        关闭所有 Agent 浏览器会话。
        """
        with cls._sessions_lock:
            session_keys = list(
                set(cls._sessions.keys()) | set(cls._session_executors.keys())
            )
        for session_key in session_keys:
            cls.close_session(session_key)

    @classmethod
    def close_session(cls, session_key: str) -> bool:
        """
        关闭指定 Agent 浏览器会话。

        :param session_key: 会话标识
        :return: 找到并关闭会话时返回 True
        """
        if cls._is_current_session_thread(session_key):
            closed = cls._close_session_in_thread(session_key)
            cls._shutdown_session_executor(session_key, wait=False)
            return closed

        executor = cls._get_existing_session_executor(session_key)
        if executor:
            future = executor.submit(
                cls._run_session_task,
                session_key,
                cls._close_session_in_thread,
                session_key,
            )
            try:
                return future.result()
            finally:
                cls._shutdown_session_executor(session_key)

        closed = cls._close_session_in_thread(session_key)
        cls._shutdown_session_executor(session_key)
        return closed

    def with_session(
        self,
        session_key: str,
        callback: Callable[[_BrowserSessionState], Any],
        user_agent: Optional[str] = None,
        cookies: Optional[str] = None,
        timeout: Optional[int] = 30,
    ) -> Any:
        """
        获取或创建浏览器会话，并在持有会话锁时执行回调。

        :param session_key: 会话标识
        :param callback: 使用浏览器会话执行操作的回调函数
        :param user_agent: 新建会话时使用的 User-Agent
        :param cookies: 本次操作要注入的 Cookie 请求头
        :param timeout: 默认操作超时时间，单位秒
        :return: 回调函数返回值
        """
        self._prune_sessions()
        return self._run_in_session_thread(
            session_key,
            self._with_session_in_thread,
            session_key,
            callback,
            user_agent=user_agent,
            cookies=cookies,
            timeout=timeout,
        )

    def _with_session_in_thread(
        self,
        session_key: str,
        callback: Callable[[_BrowserSessionState], Any],
        user_agent: Optional[str] = None,
        cookies: Optional[str] = None,
        timeout: Optional[int] = 30,
    ) -> Any:
        """
        在会话专属线程内获取浏览器会话并执行回调。

        :param session_key: 会话标识
        :param callback: 使用浏览器会话执行操作的回调函数
        :param user_agent: 新建会话时使用的 User-Agent
        :param cookies: 本次操作要注入的 Cookie 请求头
        :param timeout: 默认操作超时时间，单位秒
        :return: 回调函数返回值
        """
        session = self._get_or_create_session(
            session_key=session_key,
            user_agent=user_agent,
            cookies=cookies,
        )
        with session.lock:
            session.last_used_at = time.monotonic()
            if timeout and hasattr(session.active_page, "set_default_timeout"):
                session.active_page.set_default_timeout(int(timeout) * 1000)
            if cookies:
                session.cookies = cookies
                session.active_page.set_extra_http_headers({"cookie": cookies})
            return callback(session)

    @classmethod
    def _run_in_session_thread(
        cls,
        session_key: str,
        callback: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """
        将浏览器同步 API 调用投递到当前会话固定的单线程执行器。

        :param session_key: 会话标识
        :param callback: 需要在会话线程中运行的回调
        :param args: 回调位置参数
        :param kwargs: 回调关键字参数
        :return: 回调返回值
        """
        if cls._is_current_session_thread(session_key):
            return callback(*args, **kwargs)

        for _ in range(2):
            executor = cls._get_session_executor(session_key)
            try:
                future = executor.submit(
                    cls._run_session_task,
                    session_key,
                    callback,
                    *args,
                    **kwargs,
                )
            except RuntimeError:
                cls._discard_session_executor(session_key, executor)
                continue
            try:
                return future.result()
            except Exception:
                with cls._sessions_lock:
                    has_session = session_key in cls._sessions
                if not has_session:
                    cls._shutdown_session_executor(session_key)
                raise

        raise RuntimeError("浏览器会话线程已关闭")

    @classmethod
    def _run_session_task(
        cls,
        session_key: str,
        callback: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """
        记录当前会话工作线程后执行实际任务。

        :param session_key: 会话标识
        :param callback: 需要执行的回调
        :param args: 回调位置参数
        :param kwargs: 回调关键字参数
        :return: 回调返回值
        """
        with cls._sessions_lock:
            cls._session_thread_ids[session_key] = threading.get_ident()
        return callback(*args, **kwargs)

    @classmethod
    def _is_current_session_thread(cls, session_key: str) -> bool:
        """
        判断当前代码是否已运行在指定会话的固定线程内。

        :param session_key: 会话标识
        :return: 当前线程是会话工作线程时返回 True
        """
        with cls._sessions_lock:
            thread_id = cls._session_thread_ids.get(session_key)
        return thread_id == threading.get_ident()

    @classmethod
    def _get_session_executor(cls, session_key: str) -> ThreadPoolExecutor:
        """
        获取或创建指定会话的单线程执行器。

        :param session_key: 会话标识
        :return: 会话专属执行器
        """
        with cls._sessions_lock:
            executor = cls._session_executors.get(session_key)
            if executor:
                return executor
            executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix=cls.SESSION_WORKER_NAME_PREFIX,
            )
            cls._session_executors[session_key] = executor
            return executor

    @classmethod
    def _get_existing_session_executor(
        cls, session_key: str
    ) -> Optional[ThreadPoolExecutor]:
        """
        获取指定会话已存在的执行器。

        :param session_key: 会话标识
        :return: 已存在的执行器，不存在时返回 None
        """
        with cls._sessions_lock:
            return cls._session_executors.get(session_key)

    @classmethod
    def _discard_session_executor(
        cls,
        session_key: str,
        executor: ThreadPoolExecutor,
    ) -> None:
        """
        丢弃已经关闭的会话执行器。

        :param session_key: 会话标识
        :param executor: 需要从缓存中移除的执行器
        """
        with cls._sessions_lock:
            if cls._session_executors.get(session_key) is executor:
                cls._session_executors.pop(session_key, None)

    @classmethod
    def _shutdown_session_executor(
        cls,
        session_key: str,
        wait: bool = True,
    ) -> None:
        """
        关闭并移除指定会话的执行器。

        :param session_key: 会话标识
        :param wait: 是否等待工作线程退出
        """
        with cls._sessions_lock:
            executor = cls._session_executors.pop(session_key, None)
            cls._session_thread_ids.pop(session_key, None)
        if executor:
            executor.shutdown(wait=wait, cancel_futures=True)

    @classmethod
    def _close_session_in_thread(cls, session_key: str) -> bool:
        """
        在会话固定线程内关闭并移除浏览器会话。

        :param session_key: 会话标识
        :return: 找到并关闭会话时返回 True
        """
        with cls._sessions_lock:
            session = cls._sessions.pop(session_key, None)
        if not session:
            return False
        cls._close_session_state(session)
        return True

    def open_tab(
        self,
        session: _BrowserSessionState,
        url: Optional[str] = None,
        timeout: Optional[int] = 30,
        allow_private_network: bool = False,
    ) -> BrowserPage:
        """
        在当前会话中新建标签页，并可选导航到指定 URL。

        :param session: 当前浏览器会话
        :param url: 可选的目标 URL
        :param timeout: 导航超时时间，单位秒
        :param allow_private_network: 是否允许访问本机或私网地址
        :return: 新建的页面对象
        """
        page = session.context.new_page()
        if timeout and hasattr(page, "set_default_timeout"):
            page.set_default_timeout(int(timeout) * 1000)
        if session.cookies:
            page.set_extra_http_headers({"cookie": session.cookies})
        session.pages.append(page)
        session.active_index = len(session.pages) - 1
        if url:
            self.goto(
                page,
                url,
                timeout=timeout,
                allow_private_network=allow_private_network,
            )
        return page

    @staticmethod
    def list_tabs(session: _BrowserSessionState) -> list[dict[str, Any]]:
        """
        列出当前浏览器会话中的标签页。

        :param session: 当前浏览器会话
        :return: 标签页摘要列表
        """
        tabs = []
        for index, page in enumerate(session.pages):
            tabs.append(
                {
                    "index": index,
                    "active": index == session.active_index,
                    "url": getattr(page, "url", ""),
                    "title": BrowserSessionHelper._safe_page_title(page),
                }
            )
        return tabs

    @staticmethod
    def focus_tab(session: _BrowserSessionState, tab_index: int) -> BrowserPage:
        """
        切换当前会话的活动标签页。

        :param session: 当前浏览器会话
        :param tab_index: 标签页索引
        :return: 切换后的页面对象
        """
        if tab_index < 0 or tab_index >= len(session.pages):
            raise ValueError(f"标签页索引不存在: {tab_index}")
        session.active_index = tab_index
        return session.active_page

    @staticmethod
    def close_tab(session: _BrowserSessionState, tab_index: int) -> list[dict[str, Any]]:
        """
        关闭当前会话中的指定标签页。

        :param session: 当前浏览器会话
        :param tab_index: 标签页索引
        :return: 关闭后的标签页列表
        """
        if tab_index < 0 or tab_index >= len(session.pages):
            raise ValueError(f"标签页索引不存在: {tab_index}")
        page = session.pages.pop(tab_index)
        try:
            page.close()
        except Exception as err:
            logger.warning(f"关闭浏览器标签页失败: {str(err)}")
        if not session.pages:
            session.pages.append(session.context.new_page())
        session.active_index = min(session.active_index, len(session.pages) - 1)
        return BrowserSessionHelper.list_tabs(session)

    def goto(
        self,
        page: BrowserPage,
        url: str,
        timeout: Optional[int] = 30,
        allow_private_network: bool = False,
    ) -> Any:
        """
        校验并导航页面到指定 URL。

        :param page: 页面对象
        :param url: 目标 URL
        :param timeout: 导航超时时间，单位秒
        :param allow_private_network: 是否允许访问本机或私网地址
        :return: 浏览器导航响应对象
        """
        self.validate_url(url, allow_private_network=allow_private_network)
        response = page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=int(timeout or 30) * 1000,
        )
        try:
            page.wait_for_load_state(
                "networkidle",
                timeout=min(int(timeout or 30), 15) * 1000,
            )
        except Exception:
            pass
        self.validate_current_url(page, allow_private_network=allow_private_network)
        return response

    @classmethod
    def validate_current_url(
        cls, page: BrowserPage, allow_private_network: bool = False
    ) -> None:
        """
        校验当前页面地址，捕获跳转后的不安全目标。

        :param page: 页面对象
        :param allow_private_network: 是否允许访问本机或私网地址
        """
        current_url = getattr(page, "url", "")
        if current_url and current_url.startswith(("http://", "https://")):
            cls.validate_url(current_url, allow_private_network=allow_private_network)

    @classmethod
    def build_snapshot(
        cls,
        page: BrowserPage,
        status: Optional[Any] = None,
        max_text_chars: int = 8000,
        max_elements: int = 40,
    ) -> dict[str, Any]:
        """
        构建包含可读文本和可交互元素 ref 的页面快照。

        :param page: 页面对象
        :param status: 可选的导航状态码
        :param max_text_chars: 页面文本最大返回长度
        :param max_elements: 最大可交互元素数量
        :return: 页面快照字典
        """
        text_content = cls._safe_inner_text(page, "body")
        result = {
            "url": getattr(page, "url", ""),
            "title": cls._safe_page_title(page),
            "text_content": cls._truncate_text(text_content, max_text_chars),
            "interactive_elements": cls._extract_interactive_elements(
                page, max_elements=max_elements
            ),
        }
        if status is not None:
            result["status"] = status

        links = [
            {
                "ref": element.get("ref"),
                "text": element.get("text"),
                "href": element.get("href"),
            }
            for element in result["interactive_elements"]
            if element.get("tag") == "a" and element.get("href")
        ][:30]
        forms = [
            element
            for element in result["interactive_elements"]
            if element.get("tag") in {"input", "textarea", "select", "button"}
        ][:30]
        if links:
            result["links"] = links
        if forms:
            result["form_elements"] = forms
        return result

    @staticmethod
    def _launch_context(
        headless: bool,
        user_agent: Optional[str] = None,
        viewport: Optional[dict[str, int]] = None,
    ) -> BrowserContext:
        """按宿主反检测配置创建 CloakBrowser 上下文。"""
        context_kwargs = {
            "humanize": settings.CLOAKBROWSER_HUMANIZE,
            "human_preset": settings.CLOAKBROWSER_HUMAN_PRESET,
        }
        if user_agent:
            context_kwargs["user_agent"] = user_agent
        if viewport:
            context_kwargs["viewport"] = viewport
        return launch_browser_context(headless=headless, **context_kwargs)

    def _get_or_create_session(
        self,
        session_key: str,
        user_agent: Optional[str] = None,
        cookies: Optional[str] = None,
    ) -> _BrowserSessionState:
        """复用匹配会话，或为新的会话键创建上下文。"""
        with self._sessions_lock:
            session = self._sessions.get(session_key)
            if session and user_agent and session.user_agent != user_agent:
                self._sessions.pop(session_key, None)
                self._close_session_state(session)
                session = None
            if session:
                return session

        context = self._launch_context(
            headless=self.headless,
            user_agent=user_agent,
            viewport=self.viewport,
        )
        page = context.new_page()
        if cookies:
            page.set_extra_http_headers({"cookie": cookies})
        session = _BrowserSessionState(
            session_key=session_key,
            context=context,
            pages=[page],
            user_agent=user_agent,
            cookies=cookies,
        )
        with self._sessions_lock:
            existing_session = self._sessions.get(session_key)
            if existing_session:
                self._close_session_state(session)
                return existing_session
            self._sessions[session_key] = session
        self._enforce_session_limit(protect_session_key=session_key)
        return session

    @classmethod
    def _prune_sessions(cls) -> None:
        """关闭超过空闲时限的浏览器会话。"""
        now = time.monotonic()
        with cls._sessions_lock:
            expired_keys = [
                session_key
                for session_key, session in cls._sessions.items()
                if now - session.last_used_at > cls.SESSION_TTL_SECONDS
            ]
        for session_key in expired_keys:
            cls.close_session(session_key)

    @classmethod
    def _enforce_session_limit(cls, protect_session_key: Optional[str] = None) -> None:
        """
        清理超过数量上限的旧会话。

        :param protect_session_key: 本次刚创建、需要优先保留的会话标识
        """
        while True:
            with cls._sessions_lock:
                if len(cls._sessions) <= cls.MAX_SESSIONS:
                    return
                candidate_keys = [
                    session_key
                    for session_key in cls._sessions
                    if session_key != protect_session_key
                ]
                if not candidate_keys:
                    return
                oldest_key = min(
                    candidate_keys,
                    key=lambda key: cls._sessions[key].last_used_at,
                )
            if not cls.close_session(oldest_key):
                return

    @staticmethod
    def _close_session_state(session: _BrowserSessionState) -> None:
        """尽力关闭会话中的全部页面和浏览器上下文。"""
        with session.lock:
            for page in list(session.pages):
                try:
                    page.close()
                except Exception as err:
                    logger.warning(f"关闭浏览器页面失败: {str(err)}")
            try:
                session.context.close()
            except Exception as err:
                logger.warning(f"关闭浏览器上下文失败: {str(err)}")

    @staticmethod
    def _safe_page_title(page: BrowserPage) -> str:
        """读取页面标题，页面失效时返回空字符串。"""
        try:
            return page.title()
        except Exception:
            return ""

    @staticmethod
    def _safe_inner_text(page: BrowserPage, selector: str) -> str:
        """读取节点文本，选择器或页面失效时返回空字符串。"""
        try:
            return page.inner_text(selector)
        except Exception:
            return ""

    @staticmethod
    def _truncate_text(text: Optional[str], max_chars: int) -> str:
        """按 Agent 上下文上限截断页面文本。"""
        if not text:
            return ""
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + "\n\n...(内容已截断)"

    @classmethod
    def _extract_interactive_elements(
        cls, page: BrowserPage, max_elements: int
    ) -> list[dict[str, Any]]:
        """提取页面中可见且可交互的元素快照。"""
        script = f"""
            () => {{
                const limit = {int(max_elements)};
                const selector = [
                    'a[href]',
                    'button',
                    'input',
                    'textarea',
                    'select',
                    '[role="button"]',
                    '[role="link"]',
                    '[onclick]',
                    'summary'
                ].join(',');
                const isVisible = (el) => {{
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style && style.visibility !== 'hidden'
                        && style.display !== 'none'
                        && rect.width > 0
                        && rect.height > 0;
                }};
                return Array.from(document.querySelectorAll(selector))
                    .filter(isVisible)
                    .slice(0, limit)
                    .map((el, index) => {{
                        const ref = `e${{index + 1}}`;
                        el.setAttribute('{cls.REF_ATTRIBUTE}', ref);
                        const tag = el.tagName.toLowerCase();
                        const text = (
                            el.innerText
                            || el.value
                            || el.getAttribute('aria-label')
                            || el.getAttribute('title')
                            || el.getAttribute('placeholder')
                            || ''
                        ).trim();
                        return {{
                            ref,
                            tag,
                            type: el.type || '',
                            text: text.substring(0, 120),
                            name: el.name || '',
                            id: el.id || '',
                            role: el.getAttribute('role') || '',
                            placeholder: el.getAttribute('placeholder') || '',
                            href: el.href || '',
                            value: tag === 'select' ? '' : (el.value || '').substring(0, 80),
                            selector: `[${cls.REF_ATTRIBUTE}="${{ref}}"]`
                        }};
                    }});
            }}
        """
        try:
            elements = page.evaluate(script)
        except Exception as err:
            logger.debug(f"提取页面可交互元素失败: {str(err)}")
            return []
        if not isinstance(elements, list):
            return []
        return elements


class PlaywrightHelper:
    """为旧插件浏览器调用提供统一的 CloakBrowser 适配。"""

    def __init__(self, browser_type: Optional[str] = None, *args, **kwargs):
        """
        兼容旧的 PlaywrightHelper(browser_type=...) 构造方式。
        """
        self.browser_type = browser_type or settings.PLAYWRIGHT_BROWSER_TYPE

    @staticmethod
    def __browser_emulation() -> str:
        """
        当前浏览器仿真类型。
        """
        return (settings.BROWSER_EMULATION or "cloakbrowser").lower()

    @staticmethod
    def __launch_cloakbrowser_context(headless: bool,
                                      user_agent: Optional[str] = None,
                                      proxies: Optional[dict] = None) -> BrowserContext:
        """
        启动 CloakBrowser 上下文。
        """
        return launch_browser_context(headless=headless,
                                      proxy=proxies,
                                      user_agent=user_agent,
                                      humanize=settings.CLOAKBROWSER_HUMANIZE,
                                      human_preset=settings.CLOAKBROWSER_HUMAN_PRESET)

    @staticmethod
    def __fs_cookie_str(cookies: list) -> str:
        """将 FlareSolverr Cookie 列表转换为请求头字符串。"""
        if not cookies:
            return ""
        return "; ".join([f"{c.get('name')}={c.get('value')}" for c in cookies if c and c.get('name') is not None])

    @staticmethod
    def __flaresolverr_request(url: str,
                               cookies: Optional[str] = None,
                               proxy_config: Optional[dict] = None,
                               timeout: Optional[int] = 60) -> Optional[dict]:
        """
        调用 FlareSolverr 解决 Cloudflare 并返回 solution 结果
        参考: https://github.com/FlareSolverr/FlareSolverr
        """
        if not settings.FLARESOLVERR_URL:
            logger.warn("未配置 FLARESOLVERR_URL，无法使用 FlareSolverr")
            return None

        fs_api = settings.FLARESOLVERR_URL.rstrip("/") + "/v1"
        session_id = None

        try:
            # 检查是否需要代理认证
            need_proxy_auth = (proxy_config and proxy_config.get("server") and
                               (proxy_config.get("username") or proxy_config.get("password")))

            if need_proxy_auth:
                # 使用 session 模式支持代理认证
                logger.debug("检测到flaresolverr代理需要认证，使用 session 模式")

                # 1. 创建会话
                session_id = str(uuid.uuid4())
                create_payload: dict = {
                    "cmd": "sessions.create",
                    "session": session_id
                }

                # 添加代理配置到会话创建请求
                if proxy_config and proxy_config.get("server"):
                    proxy_payload: dict = {"url": proxy_config["server"]}
                    if proxy_config.get("username"):
                        proxy_payload["username"] = proxy_config["username"]
                    if proxy_config.get("password"):
                        proxy_payload["password"] = proxy_config["password"]
                    create_payload["proxy"] = proxy_payload

                # 创建会话
                create_result = RequestUtils(content_type="application/json",
                                             timeout=timeout or 60).post_json(url=fs_api, json=create_payload)
                if not create_result or create_result.get("status") != "ok":
                    logger.error(
                        f"创建 FlareSolverr 会话失败: {create_result.get('message') if create_result else '无响应'}")
                    return None

                # 2. 使用会话发送请求
                request_payload = {
                    "cmd": "request.get",
                    "url": url,
                    "session": session_id,
                    "maxTimeout": int(timeout or 60) * 1000,
                }
            else:
                # 使用普通模式（无代理认证）
                request_payload = {
                    "cmd": "request.get",
                    "url": url,
                    "maxTimeout": int(timeout or 60) * 1000,
                }
                # 添加代理配置（仅 URL，无认证）
                if proxy_config and proxy_config.get("server"):
                    request_payload["proxy"] = {"url": proxy_config["server"]}

            # 将 cookies 以数组形式传递给 FlareSolverr
            if cookies:
                try:
                    request_payload["cookies"] = cookie_parse(cookies, array=True)
                except Exception as e:
                    logger.debug(f"解析 cookies 失败，忽略: {str(e)}")

            # 发送请求
            data = RequestUtils(content_type="application/json",
                                timeout=timeout or 60).post_json(url=fs_api, json=request_payload)
            if not data:
                logger.error("FlareSolverr 返回空响应")
                return None
            if data.get("status") != "ok":
                logger.error(f"FlareSolverr 调用失败: {data.get('message')}")
                return None
            return data.get("solution")
        except Exception as e:
            logger.error(f"调用 FlareSolverr 失败: {str(e)}")
            return None
        finally:
            # 清理会话
            if session_id:
                try:
                    destroy_payload = {
                        "cmd": "sessions.destroy",
                        "session": session_id
                    }
                    RequestUtils(content_type="application/json",
                                 timeout=10).post_json(url=fs_api, json=destroy_payload)
                    logger.debug(f"已清理 FlareSolverr 会话: {session_id}")
                except Exception as e:
                    logger.warning(f"清理 FlareSolverr 会话失败: {str(e)}")

    def action(self, url: str,
               callback: Callable[[BrowserPage], Any],
               cookies: Optional[str] = None,
               ua: Optional[str] = None,
               proxies: Optional[dict] = None,
               headless: Optional[bool] = False,
               timeout: Optional[int] = 60) -> Any:
        """
        访问网页，接收Page对象并执行操作
        :param url: 网页地址
        :param callback: 回调函数，需要接收page对象
        :param cookies: cookies
        :param ua: user-agent
        :param proxies: 代理
        :param headless: 是否无头模式
        :param timeout: 超时时间
        """
        result = None
        try:
            context = None
            page = None
            try:
                # 如果配置使用 FlareSolverr，先通过其获取清除后的 cookies 与 UA
                fs_cookie_header = None
                fs_ua = None
                if self.__browser_emulation() == "flaresolverr":
                    solution = self.__flaresolverr_request(url=url, cookies=cookies,
                                                           proxy_config=proxies, timeout=timeout)
                    if solution:
                        fs_cookie_header = self.__fs_cookie_str(solution.get("cookies", []))
                        fs_ua = solution.get("userAgent")

                context = self.__launch_cloakbrowser_context(headless=headless,
                                                             user_agent=fs_ua or ua,
                                                             proxies=proxies)
                page = context.new_page()

                # 优先使用 FlareSolverr 返回，其次使用入参
                merged_cookie = fs_cookie_header or cookies
                if merged_cookie:
                    page.set_extra_http_headers({"cookie": merged_cookie})

                page.goto(url)
                page.wait_for_load_state("networkidle", timeout=timeout * 1000)

                # 回调函数
                result = callback(page)

            except Exception as e:
                logger.error(f"网页操作失败: {str(e)}")
            finally:
                if page:
                    page.close()
                if context:
                    context.close()
        except Exception as e:
            logger.error(f"CloakBrowser初始化失败: {str(e)}")

        return result

    def get_page_source(self, url: str,
                        cookies: Optional[str] = None,
                        ua: Optional[str] = None,
                        proxies: Optional[dict] = None,
                        headless: Optional[bool] = False,
                        timeout: Optional[int] = 60) -> Optional[str]:
        """
        获取网页源码
        :param url: 网页地址
        :param cookies: cookies
        :param ua: user-agent
        :param proxies: 代理
        :param headless: 是否无头模式
        :param timeout: 超时时间
        """
        source = None
        # 如果配置为 FlareSolverr，则直接调用获取页面源码
        if self.__browser_emulation() == "flaresolverr":
            try:
                solution = self.__flaresolverr_request(url=url, cookies=cookies,
                                                       proxy_config=proxies, timeout=timeout)
                if solution:
                    return solution.get("response")
            except Exception as e:
                logger.error(f"FlareSolverr 获取源码失败: {str(e)}")
        try:
            context = None
            page = None
            try:
                context = self.__launch_cloakbrowser_context(headless=headless,
                                                             user_agent=ua,
                                                             proxies=proxies)
                page = context.new_page()

                if cookies:
                    page.set_extra_http_headers({"cookie": cookies})

                page.goto(url)
                page.wait_for_load_state("networkidle", timeout=timeout * 1000)

                source = page.content()

            except Exception as e:
                logger.error(f"获取网页源码失败: {str(e)}")
                source = None
            finally:
                # 确保资源被正确清理
                if page:
                    page.close()
                if context:
                    context.close()
        except Exception as e:
            logger.error(f"CloakBrowser初始化失败: {str(e)}")

        return source
