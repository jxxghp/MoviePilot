"""浏览器截图在 Agent 工具路径保持真实图像，外部直调保持 JSON 兼容。"""

import base64
import json
import random
from copy import deepcopy
from io import BytesIO
from types import SimpleNamespace
from typing import Any, Union
from unittest.mock import AsyncMock

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from PIL import Image
from pydantic import Field

from app.adapters.external.ocr import OcrHelper
from app.adapters.network.browser import BrowserSessionHelper
from app.agent.middleware.output import ToolOutputMiddleware
from app.agent.middleware.policy import AgentPolicyMiddleware
from app.agent.middleware.vision import VISION_UNAVAILABLE, VisionMiddleware
from app.agent.policy.contracts import AuthSource, ExecutionOutcome, PrincipalType, ToolOrigin, ToolPolicyContext
from app.agent.tools import base as base_module
from app.agent.tools.base import MoviePilotTool
from app.agent.tools.impl.browse_webpage import (
    SCREENSHOT_MAX_BASE64_CHARS,
    SCREENSHOT_MAX_BYTES,
    SCREENSHOT_METADATA_MAX_CHARS,
    BrowserAction,
    BrowseWebpageTool,
)
from app.agent.tools.impl.recognize_captcha import RecognizeCaptchaTool
from app.agent.tools.manager import MoviePilotToolsManager
from app.agent.tools.result import ToolExecutionError, inspect_tool_result


def _image_bytes(*, large: bool = False, image_format: str = "JPEG", width: int = 32, height: int = 32) -> bytes:
    """生成本地有效图片；大图用固定随机纹理避免纯色压缩掩盖截断问题。"""
    if large:
        width, height = 384, 384
        image = Image.frombytes("RGB", (width, height), random.Random(7).randbytes(width * height * 3))
    else:
        image = Image.new("RGB", (width, height), color=(80, 120, 160))
    with image, BytesIO() as buffer:
        image.save(buffer, format=image_format, quality=85)
        return buffer.getvalue()


@pytest.fixture(scope="module")
def large_jpeg() -> bytes:
    """提供超过通用 64KiB 文本上限、仍在截图自身预算内的真实 JPEG。"""
    image = _image_bytes(large=True)
    assert 64 * 1024 < len(base64.b64encode(image)) <= SCREENSHOT_MAX_BASE64_CHARS
    return image


class _ScreenshotPage:
    """浏览器 page 的离线截图边界，记录采集参数并返回既定图像字节。"""

    url = "https://browser.example.invalid/observation"

    def __init__(self, images: list[bytes]) -> None:
        """每次截图消耗一个预设结果，便于验证降质次数。"""
        self.images = list(images)
        self.calls: list[dict[str, Any]] = []

    def screenshot(self, **kwargs: Any) -> bytes:
        """模拟截图调用，不初始化浏览器或访问页面。"""
        self.calls.append(kwargs)
        return self.images.pop(0)

    @staticmethod
    def title() -> str:
        """提供工具来源文本，与截图编码分离。"""
        return "截图测试页面"


class _ElementScreenshotPage(_ScreenshotPage):
    """提供已渲染验证码元素，任何误用整页截图都会因无预设整页图像而失败。"""

    def __init__(self, images: list[bytes]) -> None:
        """分开记录元素和整页截图，验证代码没有忽略 selector。"""
        super().__init__([])
        self.element = _ScreenshotPage(images)
        self.selectors: list[str] = []

    def locator(self, selector: str) -> _ScreenshotPage:
        """记录元素定位，不访问验证码图片 URL。"""
        self.selectors.append(selector)
        return self.element


def _payload(image: bytes) -> dict[str, Any]:
    """复用真实截图生产者构造外部 JSON 合同，而不是手写假成功形状。"""
    return json.loads(BrowseWebpageTool._action_screenshot(_ScreenshotPage([image])))


def _context() -> ToolPolicyContext:
    """为真实 ToolNode 提供独立且不依赖用户数据库的上下文。"""
    return ToolPolicyContext(
        session_id="browser-image-test", user_id="browser-image-owner", origin=ToolOrigin.AGENT_INTERACTIVE,
        principal_type=PrincipalType.HUMAN, auth_source=AuthSource.INTERNAL, agent_context={"is_admin": True},
    )


class _ImageModel(FakeMessagesListChatModel):
    """固定工具调用驱动真实图，只测试图像协议，不调用真实模型。"""

    requests: list[list[Any]] = Field(default_factory=list)

    def bind_tools(self, _tools: Any, **_kwargs: Any) -> "_ImageModel":
        """接受工具绑定，图片内容最终从 ToolMessage 核验。"""
        return self

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        """记录经过视觉中间件处理的真实模型请求，保留原有预设回复。"""
        self.requests.append([message.model_copy(deep=True) for message in messages])
        return await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)


@pytest.mark.parametrize("reduce_quality", [False, True])
def test_element_screenshot_preserves_displayed_captcha_and_size_limits(reduce_quality: bool) -> None:
    """元素截图与整页截图共用字节校验，降质仍采集同一元素且不重新下载验证码。"""
    image = _image_bytes()
    images = [b"x" * (SCREENSHOT_MAX_BYTES + 1), image] if reduce_quality else [image]
    page = _ElementScreenshotPage(images)
    result = json.loads(BrowseWebpageTool._action_screenshot(page, selector="#captcha", timeout=7))

    assert result["success"] is True
    assert base64.b64decode(result["screenshot_base64"]) == image
    assert page.selectors == ["#captcha"]
    assert not page.calls
    assert [item["quality"] for item in page.element.calls] == ([60, 30] if reduce_quality else [60])
    assert all(item["timeout"] == 7000 and "full_page" not in item for item in page.element.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("supports_images", [True, False])
async def test_ocr_failure_then_browser_capture_reaches_model_in_same_session(
    monkeypatch: pytest.MonkeyPatch, supports_images: bool,
) -> None:
    """真实工具图保留 OCR 失败，后续把同会话验证码像素交给模型或明确报告不支持图片。"""
    image = _image_bytes()
    page = _ElementScreenshotPage([image])
    sessions: list[str] = []

    def use_session(_self, *, session_key, callback, **_kwargs):
        """模拟已有登录页面，不创建新页面或发起图片下载。"""
        sessions.append(session_key)
        return callback(SimpleNamespace(active_page=page))

    async def run_inline(_self, _lane, operation, *args, **kwargs):
        """只将测试边界放到当前线程，不修改实际工具执行与结果投影。"""
        return operation(*args, **kwargs)

    monkeypatch.setattr(BrowserSessionHelper, "with_session", use_session)
    monkeypatch.setattr(MoviePilotTool, "run_blocking", run_inline)
    monkeypatch.setattr(OcrHelper, "get_captcha_text", lambda *_args, **_kwargs: "")
    browser = BrowseWebpageTool(session_id="browser-image-test", user_id="browser-image-owner")
    ocr = RecognizeCaptchaTool(session_id="browser-image-test", user_id="browser-image-owner")
    model = _ImageModel(responses=[
        AIMessage(content="", tool_calls=[{
            "id": "captcha-ocr", "name": ocr.name, "args": {"image_url": "https://example.com/captcha.png"},
        }]),
        AIMessage(content="", tool_calls=[{
            "id": "captcha-vision", "name": browser.name,
            "args": {"action": "screenshot", "selector": "#captcha", "session_key": "login-tab"},
        }]),
        AIMessage(content="收到验证码观察。"),
    ])
    graph = create_agent(model=model, tools=[ocr, browser], middleware=[
        AgentPolicyMiddleware(context=_context()), ToolOutputMiddleware(_context()),
        VisionMiddleware(supports_images=lambda _model: supports_images),
    ])
    result = await graph.ainvoke({"messages": [HumanMessage(content="完成当前页面的登录")]})

    ocr_message = next(item for item in result["messages"] if isinstance(item, ToolMessage) and item.name == ocr.name)
    assert inspect_tool_result(ocr_message) is ExecutionOutcome.FAILED
    assert "recovery" in json.loads(ocr_message.content)
    assert sessions == ["login-tab"]
    assert page.selectors == ["#captcha"] and not page.calls
    images = [block for message in model.requests[-1] if isinstance(message.content, list)
              for block in message.content if isinstance(block, dict) and block.get("type") == "image_url"]
    if supports_images:
        assert len(images) == 1
        assert base64.b64decode(images[0]["image_url"]["url"].split(",", 1)[1]) == image
    else:
        assert not images
        assert any(VISION_UNAVAILABLE in str(message.content) for message in model.requests[-1])


@pytest.mark.asyncio
async def test_large_screenshot_survives_real_tool_node_and_output_middleware(
    large_jpeg: bytes, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """超过通用字符串上限的截图仍以完整图块返回，不变成带破损 base64 的预览。"""
    payload = _payload(large_jpeg)
    tool = BrowseWebpageTool(session_id="browser-image-test", user_id="browser-image-owner")
    monkeypatch.setattr(BrowseWebpageTool, "run_blocking", AsyncMock(return_value=json.dumps(payload)))
    model = _ImageModel(responses=[
        AIMessage(content="", tool_calls=[{"id": "screenshot-1", "name": tool.name, "args": {"action": "screenshot"}}]),
        AIMessage(content="截图回执已收到。"),
    ])
    graph = create_agent(model=model, tools=[tool], middleware=[
        AgentPolicyMiddleware(context=_context()), ToolOutputMiddleware(_context()),
    ])
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="截取当前页面")]}, config={"configurable": {"thread_id": "browser-image-test"}},
    )
    message = next(item for item in result["messages"] if isinstance(item, ToolMessage))
    assert message.tool_call_id == "screenshot-1"
    assert message.status == "success"
    assert isinstance(message.content, list)
    assert [block["type"] for block in message.content] == ["text", "image_url"]
    data_url = message.content[1]["image_url"]["url"]
    assert data_url == f"data:image/jpeg;base64,{payload['screenshot_base64']}"
    assert base64.b64decode(data_url.split(",", 1)[1], validate=True) == large_jpeg
    metadata = json.loads(message.content[0]["text"])
    assert metadata["action"] == "screenshot"
    assert metadata["byte_size"] == len(large_jpeg)
    assert "screenshot_base64" not in metadata
    assert "tool_result_truncated" not in metadata


def test_agent_screenshot_metadata_is_strictly_bounded_and_source_only(large_jpeg: bytes) -> None:
    """长 URL、标题和控制字符只缩短来源预览，不重复或破坏图片本体。"""
    payload = _payload(large_jpeg)
    payload.update(url="\x01" * 20000, title="\x02" * 20000)
    result = BrowseWebpageTool(session_id="browser-image-test", user_id="owner").format_agent_result(
        json.dumps(payload), action="screenshot",
    )
    assert isinstance(result, list)
    assert len(result[0]["text"]) <= SCREENSHOT_METADATA_MAX_CHARS
    metadata = json.loads(result[0]["text"])
    assert metadata["url_truncated"] and metadata["title_truncated"]
    assert payload["screenshot_base64"] not in result[0]["text"]
    assert result[1]["image_url"]["url"].endswith(payload["screenshot_base64"])


@pytest.mark.parametrize("invalid", ["bad_base64", "oversized_base64", "wrong_declared_format", "png_bytes", "truncated_jpeg", "unproven_success", "oversized_pixels"])
def test_invalid_screenshot_never_becomes_successful_image(invalid: str) -> None:
    """编码、真实 MIME、字节预算和成功状态都必须有效，不能只信 screenshot_base64 键名。"""
    payload = _payload(_image_bytes())
    if invalid == "bad_base64":
        payload["screenshot_base64"] = "invalid-base64*"
    elif invalid == "oversized_base64":
        payload["screenshot_base64"] = "a" * (SCREENSHOT_MAX_BASE64_CHARS + 4)
    elif invalid == "wrong_declared_format":
        payload["format"] = "png"
    elif invalid == "png_bytes":
        payload["screenshot_base64"] = base64.b64encode(_image_bytes(image_format="PNG")).decode()
    elif invalid == "truncated_jpeg":
        payload["screenshot_base64"] = base64.b64encode(_image_bytes()[:-100]).decode()
    elif invalid == "unproven_success":
        payload.pop("success")
    else:
        payload["screenshot_base64"] = base64.b64encode(_image_bytes(width=1281, height=721)).decode()
    tool = BrowseWebpageTool(session_id="browser-image-test", user_id="owner")
    result = tool.format_agent_result(json.dumps(payload), action="screenshot")
    assert isinstance(result, str)
    assert inspect_tool_result(result) is ExecutionOutcome.FAILED
    assert json.loads(result)["error"] == "invalid_screenshot"
    assert "screenshot_base64" not in result


@pytest.mark.parametrize("payload", ["截图失败的普通文本", "[]", "null"])
def test_non_json_or_non_object_screenshot_result_is_structured_failure(payload: str) -> None:
    """截图异常文本和不完整响应也不能继续标为成功。"""
    result = BrowseWebpageTool(session_id="browser-image-test", user_id="owner").format_agent_result(payload, action="screenshot")
    assert isinstance(result, str)
    assert json.loads(result)["success"] is False
    assert inspect_tool_result(result) is ExecutionOutcome.FAILED


@pytest.mark.parametrize("outcome", ["failed", "unknown", "pending"])
def test_non_success_screenshot_state_does_not_gain_image(outcome: str) -> None:
    """已有明确的非成功状态必须保留，不能因为附带图片字段而升级。"""
    payload = {"execution_outcome": outcome, "success": False, "message": "未完成截图"}
    result = BrowseWebpageTool(session_id="browser-image-test", user_id="owner").format_agent_result(json.dumps(payload), action="screenshot")
    assert isinstance(result, str)
    assert inspect_tool_result(result).value == outcome
    assert json.loads(result) == payload


def test_second_screenshot_must_fit_hard_byte_limit() -> None:
    """二次降低质量后仍超限时明确失败，不向任何调用方返回超限图片。"""
    page = _ScreenshotPage([b"x" * (SCREENSHOT_MAX_BYTES + 1)] * 2)
    result = BrowseWebpageTool._action_screenshot(page)
    assert [call["quality"] for call in page.calls] == [60, 30]
    assert json.loads(result)["error"] == "screenshot_too_large"
    assert inspect_tool_result(result) is ExecutionOutcome.FAILED
    assert "screenshot_base64" not in result


def test_second_screenshot_returns_the_complete_valid_smaller_image() -> None:
    """降质成功时使用第二次完整 JPEG，避免把第一张截断后假称符合上限。"""
    valid = _image_bytes()
    page = _ScreenshotPage([b"x" * (SCREENSHOT_MAX_BYTES + 1), valid])
    result = json.loads(BrowseWebpageTool._action_screenshot(page))
    assert [call["quality"] for call in page.calls] == [60, 30]
    assert base64.b64decode(result["screenshot_base64"]) == valid
    assert result["success"] is True


def test_browser_returning_invalid_image_bytes_is_structured_failure() -> None:
    """真实浏览器边界若返回错误字节，外部 JSON 也应报告失败。"""
    result = BrowseWebpageTool._action_screenshot(_ScreenshotPage([b"not-an-image"]))
    assert json.loads(result)["error"] == "invalid_screenshot"
    assert inspect_tool_result(result) is ExecutionOutcome.FAILED


@pytest.mark.asyncio
async def test_external_run_and_tool_manager_keep_json_without_agent_projection(monkeypatch: pytest.MonkeyPatch) -> None:
    """同一截图经外部 run 与 HTTP/MCP manager 仍为完整 JSON，不调用 Agent 钩子。"""
    payload = _payload(_image_bytes())
    raw = json.dumps(payload)
    monkeypatch.setattr(BrowseWebpageTool, "run_blocking", AsyncMock(return_value=raw))

    def forbidden_projection(*_args: Any, **_kwargs: Any) -> str:
        """外部入口若误用 Agent 投影则立即失败。"""
        raise AssertionError("external call projected image")

    monkeypatch.setattr(BrowseWebpageTool, "format_agent_result", forbidden_projection)
    tool = BrowseWebpageTool(session_id="browser-image-test", user_id="owner")
    assert json.loads(await tool.run(action="screenshot")) == payload
    manager = MoviePilotToolsManager(session_id="browser-image-test", user_id="owner", is_admin=True)
    manager.tools = [tool]
    assert json.loads(await manager.call_tool(tool.name, {"action": "screenshot"})) == payload


@pytest.mark.asyncio
async def test_screenshot_runtime_and_browser_session_errors_are_structured(monkeypatch: pytest.MonkeyPatch) -> None:
    """启动或执行浏览器时的截图异常在两层边界都转为可判定失败。"""
    tool = BrowseWebpageTool(session_id="browser-image-test", user_id="owner")
    monkeypatch.setattr(BrowseWebpageTool, "run_blocking", AsyncMock(side_effect=RuntimeError("session failed")))
    assert inspect_tool_result(await tool.run(action="screenshot")) is ExecutionOutcome.FAILED

    def fail_session(*_args: Any, **_kwargs: Any) -> str:
        """阻止浏览器启动，离线模拟会话层错误。"""
        raise RuntimeError("browser failed")

    monkeypatch.setattr(BrowserSessionHelper, "with_session", fail_session)
    result = tool._execute_browser_action(
        browser_action=BrowserAction.SCREENSHOT, url=None, selector=None, ref=None, value=None, script=None,
        content_type="text", timeout=3, cookies=None, user_agent=None, session_key="browser-image-test", tab_index=None,
        allow_private_network=False,
    )
    assert inspect_tool_result(result) is ExecutionOutcome.FAILED
    assert json.loads(result)["action"] == "screenshot"


@pytest.mark.asyncio
async def test_non_screenshot_actions_and_other_tools_do_not_enable_vision() -> None:
    """图片字段不是启用视觉的通用触发器，实际工具类型和动作必须同时匹配。"""
    payload = _payload(_image_bytes())
    raw = json.dumps(payload)
    browser = BrowseWebpageTool(session_id="browser-image-test", user_id="owner")
    result = browser.format_agent_result(raw, action="evaluate")
    assert isinstance(result, str)
    assert json.loads(result) == payload

    class OtherTool(MoviePilotTool):
        """其他工具返回同名字段也仍是普通业务 JSON。"""

        name: str = "other_image_payload"
        description: str = "Return an ordinary JSON payload."

        async def run(self, **_kwargs: Any) -> str:
            """返回测试载荷，不参与浏览器截图投影。"""
            return raw

    result = await OtherTool(session_id="browser-image-test", user_id="owner")._arun(action="screenshot")
    assert isinstance(result, str)
    assert json.loads(result) == payload


@pytest.mark.asyncio
async def test_agent_formatter_runs_before_result_summary_and_failure_is_protected(monkeypatch: pytest.MonkeyPatch) -> None:
    """日志摘要收到格式化后的结果；格式化异常与工具异常一样进入失败边界。"""
    payload = _payload(_image_bytes())
    monkeypatch.setattr(BrowseWebpageTool, "run_blocking", AsyncMock(return_value=json.dumps(payload)))
    summarized: list[Any] = []

    def summarize(result: Any) -> str:
        """记录摘要输入的结构，不把图像原文写到测试输出。"""
        summarized.append(deepcopy(result))
        return "safe-summary"

    monkeypatch.setattr(base_module, "summarize_result", summarize)
    tool = BrowseWebpageTool(session_id="browser-image-test", user_id="owner")
    result = await tool._arun(action="screenshot")
    assert isinstance(result, list)
    assert summarized == [result]

    def broken_formatter(_self: Any, _result: Any, **_kwargs: Any) -> Union[str, list[dict[str, Any]]]:
        """模拟格式化边界内部异常，确认不会跳过工具统一故障处理。"""
        raise ValueError("private-formatter-data")

    monkeypatch.setattr(BrowseWebpageTool, "format_agent_result", broken_formatter)
    with pytest.raises(ToolExecutionError) as failure:
        await tool._arun(action="screenshot")
    assert "ValueError" in str(failure.value)
    assert "private-formatter-data" not in str(failure.value)
