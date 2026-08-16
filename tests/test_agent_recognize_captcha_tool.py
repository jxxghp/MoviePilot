import asyncio
import base64
import json
from unittest.mock import patch

from app.agent.tools.catalog import ToolCatalogSnapshot
from app.agent.tools.factory import MoviePilotToolFactory
from app.agent.tools.impl.recognize_captcha import RecognizeCaptchaTool
from app.agent.tools.manager import MoviePilotToolsManager
from app.adapters.external.ocr import OcrHelper


class _FakeResponse:
    """测试用响应对象，模拟 requests.Response 的最小行为。"""

    def __init__(self, content: bytes = b"", payload: dict = None):
        """初始化响应内容与 JSON 载荷。"""
        self.content = content
        self.payload = payload or {}

    def json(self) -> dict:
        """返回测试预设 JSON 内容。"""
        return self.payload

    def __bool__(self) -> bool:
        """模拟 requests.Response 在成功状态下为真。"""
        return True


def test_factory_registers_recognize_captcha_tool():
    """工具工厂应注册图形验证码识别工具。"""
    with patch(
        "app.agent.tools.factory.PluginManager.get_plugin_agent_tools",
        return_value=[],
    ):
        tools = MoviePilotToolFactory.create_tools(
            session_id="captcha-session",
            user_id="10001",
        )

    tool_names = {tool.name for tool in tools}

    assert "recognize_captcha" in tool_names


def test_mcp_tool_manager_exposes_recognize_captcha_schema():
    """MCP 工具管理器应暴露验证码识别工具参数。"""
    tool = RecognizeCaptchaTool(session_id="captcha-session", user_id="10001")
    catalog = ToolCatalogSnapshot.from_tools(
        [tool], plugin_revision=0, factory_revision="test"
    )

    with patch.object(
        MoviePilotToolFactory,
        "create_catalog",
        return_value=catalog,
    ) as create_catalog:
        manager = MoviePilotToolsManager(is_admin=True)
        create_catalog.assert_not_called()
        tool_definitions = manager.list_tools()
        create_catalog.assert_called_once()

    schema = tool_definitions[0].input_schema

    assert [item.name for item in tool_definitions] == ["recognize_captcha"]
    assert "image_url" in schema["required"]
    assert "cookie" in schema["properties"]
    assert "user_agent" in schema["properties"]
    assert "allow_private_network" in schema["properties"]


def test_ocr_helper_extracts_data_url_base64_without_downloading_image():
    """data:image 地址应直接提取 base64 内容并提交给 OCR 服务。"""
    image_b64 = base64.b64encode(b"captcha-image").decode()
    image_url = f"data:image/png;base64,{image_b64}"

    with patch("app.adapters.external.ocr.RequestUtils") as request_utils:
        request_utils.return_value.post_res.return_value = _FakeResponse(
            payload={"result": "a8k2"}
        )

        result = OcrHelper().get_captcha_text(image_url=image_url)

    assert result == "a8k2"
    request_utils.return_value.get_res.assert_not_called()
    request_utils.return_value.post_res.assert_called_once()
    assert request_utils.return_value.post_res.call_args.kwargs["json"] == {
        "base64_img": image_b64
    }


def test_ocr_helper_normalizes_data_url_base64_padding():
    """data:image 地址缺少 padding 时应补齐后提交给 OCR 服务。"""
    image_url = "data:image/jpeg;base64,YWJjZA"

    with patch("app.adapters.external.ocr.RequestUtils") as request_utils:
        request_utils.return_value.post_res.return_value = _FakeResponse(
            payload={"result": "z9k2"}
        )

        result = OcrHelper().get_captcha_text(image_url=image_url)

    assert result == "z9k2"
    request_utils.return_value.get_res.assert_not_called()
    assert request_utils.return_value.post_res.call_args.kwargs["json"] == {
        "base64_img": "YWJjZA=="
    }


def test_recognize_captcha_tool_formats_data_url_for_log():
    """验证码工具日志应隐藏 data:image 的完整图片内容。"""
    image_b64 = base64.b64encode(b"captcha-image").decode()
    image_url = f"data:image/jpeg;base64,{image_b64}"

    result = RecognizeCaptchaTool._format_image_url_for_log(image_url)

    assert result == f"data:image/jpeg;base64,<base64:{len(image_b64)} chars>"
    assert image_b64 not in result


def test_recognize_captcha_tool_returns_captcha_text_from_ocr_helper():
    """验证码工具应返回结构化识别结果，便于 Agent 继续填写表单。"""
    tool = RecognizeCaptchaTool(session_id="captcha-session", user_id="10001")

    async def _run_tool():
        """执行一次带 mock OCR 的工具调用。"""
        with patch(
            "app.agent.tools.impl.recognize_captcha.OcrHelper.get_captcha_text",
            return_value="x7p9",
        ) as recognize_mock:
            result = await tool.run(
                image_url="https://example.com/captcha.png",
                cookie="sid=abc",
                user_agent="MoviePilotTest/1.0",
            )
            return result, recognize_mock

    result, recognize_mock = asyncio.run(_run_tool())
    payload = json.loads(result)

    assert payload == {
        "success": True,
        "captcha_text": "x7p9",
        "message": "验证码识别成功",
    }
    recognize_mock.assert_called_once_with(
        image_url="https://example.com/captcha.png",
        cookie="sid=abc",
        ua="MoviePilotTest/1.0",
    )


def test_recognize_captcha_tool_blocks_private_network_by_default():
    """验证码工具默认应拒绝本机和私网图片地址。"""
    tool = RecognizeCaptchaTool(session_id="captcha-session", user_id="10001")

    with patch(
        "app.agent.tools.impl.recognize_captcha.OcrHelper.get_captcha_text",
        return_value="x7p9",
    ) as recognize_mock:
        result = asyncio.run(
            tool.run(image_url="http://127.0.0.1/captcha.png")
        )

    payload = json.loads(result)

    assert payload["success"] is False
    assert payload["captcha_text"] == ""
    assert "默认不允许访问本机或私网地址" in payload["message"]
    recognize_mock.assert_not_called()
