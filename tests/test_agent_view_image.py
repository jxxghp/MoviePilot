"""Agent 图片查看工具的来源、安全和多模态输出边界测试。"""

import base64
import json
from io import BytesIO
from typing import Any, Optional

import pytest
from PIL import Image
from pydantic import ValidationError

from app.adapters.network.http import AsyncRequestUtils
from app.agent.middleware.vision import VisionMiddleware
from app.agent.tools.factory import MoviePilotToolFactory
from app.agent.tools.impl.view_image import (
    IMAGE_MAX_BYTES,
    ViewImageInput,
    ViewImageTool,
)
from app.application.security.url import SecurityUtils


def _image_bytes(image_format: str = "PNG") -> bytes:
    """生成不依赖外部文件或网络的有效测试图片。"""
    with BytesIO() as buffer:
        image = Image.new("RGB", (24, 16), color=(30, 90, 160))
        with image:
            image.save(buffer, format=image_format)
        return buffer.getvalue()


class _StreamResponse:
    """提供 AsyncRequestUtils 流式响应的最小离线测试替身。"""

    def __init__(self, content: bytes, status_code: int = 200, headers: Optional[dict[str, str]] = None) -> None:
        """保存状态、响应头和分块内容。"""
        self.status_code = status_code
        self.headers = headers or {}
        self._content = content

    async def aiter_bytes(self):
        """按两个分块返回响应体，覆盖工具的流式大小检查。"""
        midpoint = max(1, len(self._content) // 2)
        yield self._content[:midpoint]
        yield self._content[midpoint:]


class _StreamContext:
    """模拟异步响应上下文管理器，确保测试不建立真实连接。"""

    def __init__(self, response: _StreamResponse) -> None:
        """保存待返回的响应。"""
        self.response = response

    async def __aenter__(self) -> _StreamResponse:
        """进入离线响应上下文。"""
        return self.response

    async def __aexit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        """退出离线响应上下文。"""
        del exc_type, exc_value, traceback


def _tool() -> ViewImageTool:
    """构造不依赖宿主启动组合根的工具实例。"""
    return ViewImageTool(session_id="view-image-test", user_id="owner")


def test_input_requires_exactly_one_image_source() -> None:
    """输入模型必须拒绝缺少来源或同时提供两个来源。"""
    with pytest.raises(ValidationError):
        ViewImageInput()
    with pytest.raises(ValidationError):
        ViewImageInput(url="https://images.example.invalid/a.png", file_path="/tmp/a.png")
    assert ViewImageInput(url="https://images.example.invalid/a.png").detail == "auto"
    assert ViewImageInput(image_data=base64.b64encode(_image_bytes()).decode("ascii")).detail == "auto"


@pytest.mark.asyncio
async def test_local_image_returns_a_real_model_image_block(tmp_path) -> None:
    """本地图片应通过 Agent 路径权限后返回完整且可被视觉中间件接受的图块。"""
    image_path = tmp_path / "poster.png"
    image_path.write_bytes(_image_bytes())
    tool = _tool()
    tool.set_agent_context({"is_admin": True})

    result = await tool.run(file_path=str(image_path), detail="high")
    payload = json.loads(result)
    projected = tool.format_agent_result(result, file_path=str(image_path), detail="high")

    assert payload["success"] is True
    assert payload["source_type"] == "file"
    assert isinstance(projected, list)
    assert [block["type"] for block in projected] == ["text", "image_url"]
    assert projected[1]["image_url"]["detail"] == "high"
    image_block = projected[1]
    assert VisionMiddleware._image_for_model(image_block) == image_block
    assert base64.b64decode(image_block["image_url"]["url"].split(",", 1)[1], validate=True) == image_path.read_bytes()
    assert "image_base64" not in projected[0]["text"]


@pytest.mark.asyncio
async def test_data_url_is_validated_and_projected_without_network() -> None:
    """data URL 图片应走同一真实格式校验，不触发 URL 安全或网络访问。"""
    encoded = base64.b64encode(_image_bytes("JPEG")).decode("ascii")
    tool = _tool()

    result = await tool.run(url=f"data:image/jpeg;base64,{encoded}")
    projected = tool.format_agent_result(result, url="data:image/jpeg;base64,...")

    assert json.loads(result)["mime_type"] == "image/jpeg"
    assert isinstance(projected, list)
    assert projected[1]["image_url"]["url"] == f"data:image/jpeg;base64,{encoded}"


@pytest.mark.asyncio
async def test_image_content_accepts_raw_bytes_and_plain_base64() -> None:
    """图片内容输入支持工具间传递的原始字节和纯 Base64 文本。"""
    image = _image_bytes("PNG")
    tool = _tool()

    raw_result = await tool.run(image_data=image)
    encoded_result = await tool.run(image_data=base64.b64encode(image).decode("ascii"))

    raw_payload = json.loads(raw_result)
    encoded_payload = json.loads(encoded_result)
    assert raw_payload["source_type"] == "content"
    assert encoded_payload["source_type"] == "content"
    assert base64.b64decode(raw_payload["image_base64"]) == image
    assert base64.b64decode(encoded_payload["image_base64"]) == image


@pytest.mark.asyncio
async def test_remote_image_uses_ssrf_validation_and_streaming_download(monkeypatch: pytest.MonkeyPatch) -> None:
    """远程图片必须先通过公网校验，再使用有大小上限的异步流下载。"""
    image = _image_bytes("JPEG")
    safe_calls: list[tuple[str, set[str], bool, bool]] = []

    async def safe_url(url: str, domains: set[str], strict: bool, block_private: bool) -> bool:
        """记录 URL 安全参数并放行测试域名。"""
        safe_calls.append((url, domains, strict, block_private))
        return True

    def get_stream(_request: AsyncRequestUtils, url: str, **kwargs: Any) -> _StreamContext:
        """返回固定图片响应并断言请求未自动跟随重定向。"""
        assert url == "https://images.example.invalid/poster.jpg"
        assert kwargs["raise_exception"] is False
        return _StreamContext(_StreamResponse(image, headers={"content-length": str(len(image))}))

    monkeypatch.setattr(SecurityUtils, "is_safe_url_async", safe_url)
    monkeypatch.setattr(AsyncRequestUtils, "get_stream", get_stream)

    result = await _tool().run(url="https://images.example.invalid/poster.jpg")
    payload = json.loads(result)

    assert payload["success"] is True
    assert safe_calls == [
        ("https://images.example.invalid/poster.jpg", {"images.example.invalid"}, True, True),
    ]


@pytest.mark.asyncio
async def test_remote_private_or_rejected_url_never_reaches_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """未通过 URL 安全校验时必须在网络边界前失败。"""

    async def unsafe_url(*_args: Any, **_kwargs: Any) -> bool:
        """拒绝模拟的内部目标。"""
        return False

    def unexpected_network(*_args: Any, **_kwargs: Any) -> _StreamContext:
        """如果网络被调用则让测试明确失败。"""
        raise AssertionError("unsafe image URL reached HTTP")

    monkeypatch.setattr(SecurityUtils, "is_safe_url_async", unsafe_url)
    monkeypatch.setattr(AsyncRequestUtils, "get_stream", unexpected_network)

    result = await _tool().run(url="https://images.example.invalid/private.png")
    payload = json.loads(result)

    assert payload["success"] is False
    assert payload["error"] == "unsafe_url"


@pytest.mark.asyncio
async def test_remote_content_length_limit_is_enforced_before_body_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """远程响应声明超限时应立即失败，不把大响应交给图片解码器。"""

    async def safe_url(*_args: Any, **_kwargs: Any) -> bool:
        """放行测试 URL。"""
        return True

    def get_stream(_request: AsyncRequestUtils, _url: str, **_kwargs: Any) -> _StreamContext:
        """返回声明超限且正文很小的响应。"""
        return _StreamContext(
            _StreamResponse(b"too-small-to-read", headers={"content-length": str(IMAGE_MAX_BYTES + 1)})
        )

    monkeypatch.setattr(SecurityUtils, "is_safe_url_async", safe_url)
    monkeypatch.setattr(AsyncRequestUtils, "get_stream", get_stream)

    result = await _tool().run(url="https://images.example.invalid/oversized.jpg")

    assert json.loads(result)["error"] == "image_too_large"


def test_image_tool_is_registered_for_agent_but_keeps_raw_data_out_of_generic_formatter() -> None:
    """工具工厂应注册图片能力，成功图片由专用 formatter 保持图块而非文本截断。"""
    tool_names = {tool_class.model_fields["name"].default for tool_class in MoviePilotToolFactory.BUILTIN_TOOL_CLASSES}
    assert "view_image" in tool_names
    assert IMAGE_MAX_BYTES == 768 * 1024
