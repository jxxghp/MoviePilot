"""浏览器操作工具 - 让Agent能够通过Playwright控制浏览器进行网页交互"""

import base64
import json
from enum import Enum
from typing import Any, Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.helper.browser import BrowserSessionHelper
from app.log import logger

# 页面内容最大长度；保留在全局工具结果兜底上限以内。
MAX_CONTENT_LENGTH = 12_000
# 默认超时时间（秒）
DEFAULT_TIMEOUT = 30
# 截图最大宽度
SCREENSHOT_MAX_WIDTH = 1280
# 截图最大高度
SCREENSHOT_MAX_HEIGHT = 720


class BrowserAction(str, Enum):
    """浏览器操作类型"""

    GOTO = "goto"
    SNAPSHOT = "snapshot"
    GET_CONTENT = "get_content"
    SCREENSHOT = "screenshot"
    CLICK = "click"
    CLICK_REF = "click_ref"
    FILL = "fill"
    FILL_REF = "fill_ref"
    SELECT = "select"
    SELECT_REF = "select_ref"
    EVALUATE = "evaluate"
    WAIT = "wait"
    LIST_TABS = "list_tabs"
    OPEN_TAB = "open_tab"
    FOCUS_TAB = "focus_tab"
    CLOSE_TAB = "close_tab"
    CLOSE_SESSION = "close_session"


class BrowseWebpageInput(BaseModel):
    """浏览器操作工具的输入参数模型"""

    action: str = Field(
        ...,
        description=(
            "The browser action to perform. Available actions:\n"
            "- 'goto': Navigate to a URL, returns page title and text summary\n"
            "- 'snapshot': Get current page snapshot with interactive element refs\n"
            "- 'get_content': Get current page content (text or HTML)\n"
            "- 'screenshot': Take a screenshot of the current page, returns base64 image\n"
            "- 'click': Click on an element specified by selector\n"
            "- 'click_ref': Click an element by ref from the latest snapshot\n"
            "- 'fill': Fill text into an input element specified by selector\n"
            "- 'fill_ref': Fill text into an input element by ref from the latest snapshot\n"
            "- 'select': Select an option from a dropdown element\n"
            "- 'select_ref': Select an option by ref from the latest snapshot\n"
            "- 'evaluate': Execute JavaScript code on the page and return the result\n"
            "- 'wait': Wait for an element to appear on the page\n"
            "- 'list_tabs': List browser tabs in the current session\n"
            "- 'open_tab': Open a new tab, optionally navigating to a URL\n"
            "- 'focus_tab': Switch active tab by index\n"
            "- 'close_tab': Close a tab by index\n"
            "- 'close_session': Close the current browser session"
        ),
    )
    url: Optional[str] = Field(
        None, description="URL to navigate to (required for 'goto' action)"
    )
    selector: Optional[str] = Field(
        None,
        description="CSS selector or text selector for the target element (for 'click', 'fill', 'select', 'wait' actions). "
        "Supports CSS selectors like '#id', '.class', 'tag', and Playwright text selectors like 'text=Click me'",
    )
    ref: Optional[str] = Field(
        None,
        description="Element ref returned by 'snapshot' or action results (for 'click_ref', 'fill_ref', 'select_ref')",
    )
    value: Optional[str] = Field(
        None,
        description="Value to fill into input or option value to select (for 'fill' and 'select' actions)",
    )
    script: Optional[str] = Field(
        None,
        description="JavaScript code to execute on the page (for 'evaluate' action). "
        "The script should return a value that can be serialized to JSON.",
    )
    content_type: Optional[str] = Field(
        "text",
        description="Content type for 'get_content' action: 'text' for readable text, 'html' for raw HTML",
    )
    timeout: Optional[int] = Field(
        DEFAULT_TIMEOUT, description="Timeout in seconds for the action (default: 30)"
    )
    cookies: Optional[str] = Field(
        None,
        description="Cookies to set for the browser context, format: 'name1=value1; name2=value2'",
    )
    user_agent: Optional[str] = Field(
        None, description="Custom User-Agent string for the browser context"
    )
    session_key: Optional[str] = Field(
        None,
        description="Browser session key. Defaults to the current agent session id.",
    )
    tab_index: Optional[int] = Field(
        None,
        description="Tab index for 'focus_tab' and 'close_tab' actions.",
    )
    allow_private_network: bool = Field(
        False,
        description="Allow browser navigation to localhost, loopback, private, or link-local addresses.",
    )


class BrowseWebpageTool(MoviePilotTool):
    name: str = "browse_webpage"
    tags: list[str] = [
        ToolTag.Read,
        ToolTag.Web,
    ]
    description: str = (
        "Control a real browser (Playwright) to interact with web pages. "
        "Supports navigating to URLs, reading page content, taking screenshots, "
        "clicking elements, filling forms, selecting dropdown options, executing JavaScript, waiting for elements, "
        "and managing tabs. "
        "Use this tool when you need to interact with dynamic web pages, "
        "fill in forms, click buttons, or extract content from JavaScript-rendered pages. "
        "The browser session persists across multiple calls within the same conversation - "
        "first call 'goto' to open a page, inspect 'interactive_elements', then use *_ref actions when possible. "
        "For safety, localhost and private network URLs are blocked by default unless allow_private_network is true."
    )
    args_schema: Type[BaseModel] = BrowseWebpageInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据操作类型生成友好的提示消息"""
        action = kwargs.get("action", "")
        url = kwargs.get("url", "")
        selector = kwargs.get("selector", "")
        action_messages = {
            "goto": f"打开网页: {url}",
            "snapshot": "读取页面快照",
            "get_content": "获取页面内容",
            "screenshot": "截取页面截图",
            "click": f"点击元素: {selector}",
            "click_ref": f"点击元素引用: {kwargs.get('ref', '')}",
            "fill": f"填写表单: {selector}",
            "fill_ref": f"填写元素引用: {kwargs.get('ref', '')}",
            "select": f"选择选项: {selector}",
            "select_ref": f"选择元素引用: {kwargs.get('ref', '')}",
            "evaluate": "执行 JavaScript",
            "wait": f"等待元素: {selector}",
            "list_tabs": "列出浏览器标签页",
            "open_tab": f"打开新标签页: {url}",
            "focus_tab": f"切换浏览器标签页: {kwargs.get('tab_index', '')}",
            "close_tab": f"关闭浏览器标签页: {kwargs.get('tab_index', '')}",
            "close_session": "关闭浏览器会话",
        }
        return action_messages.get(action, f"执行浏览器操作: {action}")

    async def run(
        self,
        action: str,
        url: Optional[str] = None,
        selector: Optional[str] = None,
        ref: Optional[str] = None,
        value: Optional[str] = None,
        script: Optional[str] = None,
        content_type: Optional[str] = "text",
        timeout: Optional[int] = DEFAULT_TIMEOUT,
        cookies: Optional[str] = None,
        user_agent: Optional[str] = None,
        session_key: Optional[str] = None,
        tab_index: Optional[int] = None,
        allow_private_network: bool = False,
        **kwargs,
    ) -> str:
        """执行浏览器操作"""
        logger.info(
            f"执行工具: {self.name}, 动作: {action}, URL: {url}, 选择器: {selector}"
        )

        try:
            # 验证操作类型
            try:
                browser_action = BrowserAction(action)
            except ValueError:
                valid_actions = ", ".join([a.value for a in BrowserAction])
                return f"错误: 不支持的操作类型 '{action}'，支持的操作: {valid_actions}"

            # 参数校验
            if browser_action == BrowserAction.GOTO and not url:
                return "错误: 'goto' 操作需要提供 url 参数"
            if browser_action == BrowserAction.OPEN_TAB and not url:
                return "错误: 'open_tab' 操作需要提供 url 参数"
            if (
                browser_action
                in (
                    BrowserAction.CLICK,
                    BrowserAction.FILL,
                    BrowserAction.SELECT,
                    BrowserAction.WAIT,
                )
                and not selector
            ):
                return f"错误: '{action}' 操作需要提供 selector 参数"
            if (
                browser_action
                in (
                    BrowserAction.CLICK_REF,
                    BrowserAction.FILL_REF,
                    BrowserAction.SELECT_REF,
                )
                and not ref
            ):
                return f"错误: '{action}' 操作需要提供 ref 参数"
            if browser_action == BrowserAction.FILL and value is None:
                return "错误: 'fill' 操作需要提供 value 参数"
            if browser_action == BrowserAction.FILL_REF and value is None:
                return "错误: 'fill_ref' 操作需要提供 value 参数"
            if browser_action == BrowserAction.EVALUATE and not script:
                return "错误: 'evaluate' 操作需要提供 script 参数"
            if (
                browser_action == BrowserAction.EVALUATE
                and not await self.is_admin_user()
            ):
                return "错误: 'evaluate' 操作仅允许管理员使用"
            if (
                browser_action in (BrowserAction.FOCUS_TAB, BrowserAction.CLOSE_TAB)
                and tab_index is None
            ):
                return f"错误: '{action}' 操作需要提供 tab_index 参数"

            effective_session_key = session_key or self._session_id

            result = await self.run_blocking(
                "web",
                self._execute_browser_action,
                browser_action=browser_action,
                url=url,
                selector=selector,
                ref=ref,
                value=value,
                script=script,
                content_type=content_type,
                timeout=timeout,
                cookies=cookies,
                user_agent=user_agent,
                session_key=effective_session_key,
                tab_index=tab_index,
                allow_private_network=allow_private_network,
            )
            return result

        except Exception as e:
            logger.error(f"浏览器操作失败: {e}", exc_info=True)
            return f"浏览器操作失败: {str(e)}"

    def _execute_browser_action(
        self,
        browser_action: BrowserAction,
        url: Optional[str],
        selector: Optional[str],
        ref: Optional[str],
        value: Optional[str],
        script: Optional[str],
        content_type: Optional[str],
        timeout: int,
        cookies: Optional[str],
        user_agent: Optional[str],
        session_key: str,
        tab_index: Optional[int],
        allow_private_network: bool,
    ) -> str:
        """在同步上下文中执行 CloakBrowser 浏览器操作"""

        try:
            if browser_action == BrowserAction.CLOSE_SESSION:
                closed = BrowserSessionHelper.close_session(session_key)
                message = "浏览器会话已关闭" if closed else "浏览器会话不存在"
                return self._json_response(
                    {
                        "success": closed,
                        "message": message,
                    }
                )

            helper = BrowserSessionHelper(
                headless=True,
                viewport={
                    "width": SCREENSHOT_MAX_WIDTH,
                    "height": SCREENSHOT_MAX_HEIGHT,
                },
            )

            def _callback(session) -> str:
                return self._do_action(
                    helper=helper,
                    session=session,
                    browser_action=browser_action,
                    url=url,
                    selector=selector,
                    ref=ref,
                    value=value,
                    script=script,
                    content_type=content_type,
                    timeout=timeout,
                    tab_index=tab_index,
                    allow_private_network=allow_private_network,
                )

            return helper.with_session(
                session_key=session_key,
                callback=_callback,
                user_agent=user_agent,
                cookies=cookies,
                timeout=timeout,
            )

        except Exception as e:
            logger.error(f"CloakBrowser 执行失败: {e}", exc_info=True)
            return f"CloakBrowser 执行失败: {str(e)}"

    def _do_action(
        self,
        helper: BrowserSessionHelper,
        session,
        browser_action: BrowserAction,
        url: Optional[str],
        selector: Optional[str],
        ref: Optional[str],
        value: Optional[str],
        script: Optional[str],
        content_type: Optional[str],
        timeout: int,
        tab_index: Optional[int],
        allow_private_network: bool,
    ) -> str:
        """执行具体的浏览器操作"""
        page = session.active_page

        if browser_action == BrowserAction.GOTO:
            return self._action_goto(
                helper,
                page,
                url,
                timeout,
                allow_private_network=allow_private_network,
            )

        elif browser_action == BrowserAction.SNAPSHOT:
            return self._json_response(
                BrowserSessionHelper.build_snapshot(
                    page,
                    max_text_chars=MAX_CONTENT_LENGTH,
                )
            )

        elif browser_action == BrowserAction.GET_CONTENT:
            return self._action_get_content(page, content_type)

        elif browser_action == BrowserAction.SCREENSHOT:
            return self._action_screenshot(page)

        elif browser_action == BrowserAction.CLICK:
            return self._action_click(page, selector, timeout)

        elif browser_action == BrowserAction.CLICK_REF:
            return self._action_click(
                page,
                BrowserSessionHelper.ref_to_selector(ref),
                timeout,
                ref=ref,
            )

        elif browser_action == BrowserAction.FILL:
            return self._action_fill(page, selector, value, timeout)

        elif browser_action == BrowserAction.FILL_REF:
            return self._action_fill(
                page,
                BrowserSessionHelper.ref_to_selector(ref),
                value,
                timeout,
                ref=ref,
            )

        elif browser_action == BrowserAction.SELECT:
            return self._action_select(page, selector, value, timeout)

        elif browser_action == BrowserAction.SELECT_REF:
            return self._action_select(
                page,
                BrowserSessionHelper.ref_to_selector(ref),
                value,
                timeout,
                ref=ref,
            )

        elif browser_action == BrowserAction.EVALUATE:
            return self._action_evaluate(page, script)

        elif browser_action == BrowserAction.WAIT:
            return self._action_wait(page, selector, timeout)

        elif browser_action == BrowserAction.LIST_TABS:
            return self._json_response({"tabs": BrowserSessionHelper.list_tabs(session)})

        elif browser_action == BrowserAction.OPEN_TAB:
            page = helper.open_tab(
                session,
                url=url,
                timeout=timeout,
                allow_private_network=allow_private_network,
            )
            return self._json_response(
                {
                    "success": True,
                    "active_tab": session.active_index,
                    "tabs": BrowserSessionHelper.list_tabs(session),
                    "snapshot": BrowserSessionHelper.build_snapshot(
                        page,
                        max_text_chars=MAX_CONTENT_LENGTH,
                    ),
                }
            )

        elif browser_action == BrowserAction.FOCUS_TAB:
            page = BrowserSessionHelper.focus_tab(session, tab_index)
            return self._json_response(
                {
                    "success": True,
                    "active_tab": session.active_index,
                    "tabs": BrowserSessionHelper.list_tabs(session),
                    "snapshot": BrowserSessionHelper.build_snapshot(
                        page,
                        max_text_chars=MAX_CONTENT_LENGTH,
                    ),
                }
            )

        elif browser_action == BrowserAction.CLOSE_TAB:
            tabs = BrowserSessionHelper.close_tab(session, tab_index)
            return self._json_response({"success": True, "tabs": tabs})

        return f"未知操作: {browser_action}"

    @staticmethod
    def _json_response(payload: dict[str, Any]) -> str:
        """返回格式化 JSON 字符串"""
        return json.dumps(payload, ensure_ascii=False, indent=2)

    @staticmethod
    def _action_goto(
        helper: BrowserSessionHelper,
        page,
        url: str,
        timeout: int,
        allow_private_network: bool,
    ) -> str:
        """导航到URL"""
        response = helper.goto(
            page,
            url,
            timeout=timeout,
            allow_private_network=allow_private_network,
        )
        status = response.status if response else "unknown"
        result = BrowserSessionHelper.build_snapshot(
            page,
            status=status,
            max_text_chars=MAX_CONTENT_LENGTH,
        )
        return BrowseWebpageTool._json_response(result)

    @staticmethod
    def _action_get_content(page, content_type: Optional[str]) -> str:
        """获取页面内容"""
        title = page.title()
        page_url = page.url

        if content_type == "html":
            content = page.content()
        else:
            content = page.inner_text("body")

        if content and len(content) > MAX_CONTENT_LENGTH:
            content = content[:MAX_CONTENT_LENGTH] + "\n\n...(内容已截断)"

        result = {
            "url": page_url,
            "title": title,
            "content_type": content_type,
            "content": content,
        }
        return BrowseWebpageTool._json_response(result)

    @staticmethod
    def _action_screenshot(page) -> str:
        """截取页面截图"""
        screenshot_bytes = page.screenshot(
            full_page=False,
            type="jpeg",
            quality=60,
        )
        screenshot_b64 = base64.b64encode(screenshot_bytes).decode("utf-8")

        # 限制截图大小（base64编码后大约增大33%）
        max_b64_size = 200 * 1024  # ~150KB 原始图片
        if len(screenshot_b64) > max_b64_size:
            # 降低质量重新截图
            screenshot_bytes = page.screenshot(
                full_page=False,
                type="jpeg",
                quality=30,
            )
            screenshot_b64 = base64.b64encode(screenshot_bytes).decode("utf-8")

        title = page.title()
        page_url = page.url

        result = {
            "url": page_url,
            "title": title,
            "screenshot_base64": screenshot_b64,
            "format": "jpeg",
            "note": "截图已以 base64 编码返回",
        }
        return BrowseWebpageTool._json_response(result)

    @staticmethod
    def _action_click(
        page,
        selector: str,
        timeout: int,
        ref: Optional[str] = None,
    ) -> str:
        """点击元素"""
        page.click(selector, timeout=timeout * 1000)

        # 等待可能的页面变化
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass

        return BrowseWebpageTool._json_response(
            {
                "success": True,
                "message": f"成功点击元素: {ref or selector}",
                "snapshot": BrowserSessionHelper.build_snapshot(
                    page,
                    max_text_chars=MAX_CONTENT_LENGTH,
                ),
            }
        )

    @staticmethod
    def _action_fill(
        page,
        selector: str,
        value: str,
        timeout: int,
        ref: Optional[str] = None,
    ) -> str:
        """填写表单"""
        page.fill(selector, value, timeout=timeout * 1000)

        return BrowseWebpageTool._json_response(
            {
                "success": True,
                "message": f"成功填写元素 '{ref or selector}'",
                "snapshot": BrowserSessionHelper.build_snapshot(
                    page,
                    max_text_chars=MAX_CONTENT_LENGTH,
                ),
            }
        )

    @staticmethod
    def _action_select(
        page,
        selector: str,
        value: Optional[str],
        timeout: int,
        ref: Optional[str] = None,
    ) -> str:
        """选择下拉选项"""
        if value:
            page.select_option(selector, value=value, timeout=timeout * 1000)
        else:
            return "错误: 'select' 操作需要提供 value 参数"

        return BrowseWebpageTool._json_response(
            {
                "success": True,
                "message": f"成功选择元素 '{ref or selector}' 的选项 '{value}'",
                "snapshot": BrowserSessionHelper.build_snapshot(
                    page,
                    max_text_chars=MAX_CONTENT_LENGTH,
                ),
            }
        )

    @staticmethod
    def _action_evaluate(page, script: str) -> str:
        """执行 JavaScript"""
        result = page.evaluate(script)

        # 格式化结果
        if result is None:
            formatted = "null"
        elif isinstance(result, (dict, list)):
            formatted = json.dumps(result, ensure_ascii=False, indent=2)
        else:
            formatted = str(result)

        # 限制结果长度
        if len(formatted) > MAX_CONTENT_LENGTH:
            formatted = formatted[:MAX_CONTENT_LENGTH] + "\n\n...(结果已截断)"

        return BrowseWebpageTool._json_response(
            {
                "success": True,
                "result": formatted,
            }
        )

    @staticmethod
    def _action_wait(page, selector: str, timeout: int) -> str:
        """等待元素出现"""
        element = page.wait_for_selector(selector, timeout=timeout * 1000)

        if element:
            visible = element.is_visible()
            text = element.inner_text()
            if text and len(text) > 200:
                text = text[:200] + "..."

            return BrowseWebpageTool._json_response(
                {
                    "success": True,
                    "message": f"元素 '{selector}' 已出现",
                    "visible": visible,
                    "text": text,
                    "snapshot": BrowserSessionHelper.build_snapshot(
                        page,
                        max_text_chars=MAX_CONTENT_LENGTH,
                    ),
                }
            )
        else:
            return BrowseWebpageTool._json_response(
                {
                    "success": False,
                    "message": f"等待元素 '{selector}' 超时",
                }
            )
