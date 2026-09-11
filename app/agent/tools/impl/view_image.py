"""图片查看工具：把 URL 或本地图片转换为 Agent 可消费的视觉输入。"""

import base64
import binascii
import json
from io import BytesIO
from pathlib import Path
from typing import Any, Literal, Optional, Type, Union
from urllib.parse import urljoin, urlsplit

from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field, model_validator

from app.adapters.network.browser import BrowserSessionHelper
from app.adapters.network.http import AsyncRequestUtils
from app.agent.policy.contracts import ExecutionOutcome
from app.agent.tools.base import MoviePilotTool
from app.agent.tools.result import inspect_tool_result
from app.agent.tools.tags import ToolTag
from app.application.security.url import SecurityUtils
from app.runtime.log import logger

IMAGE_MAX_BYTES = 768 * 1024
IMAGE_MAX_BASE64_CHARS = 1024 * 1024
IMAGE_MAX_PIXELS = 16_000_000
IMAGE_FETCH_TIMEOUT = 20
IMAGE_MAX_REDIRECTS = 3
IMAGE_MAX_URL_CHARS = 8192
IMAGE_METADATA_MAX_CHARS = 4096
SUPPORTED_IMAGE_MIME_TYPES = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "GIF": "image/gif",
    "WEBP": "image/webp",
}


class ViewImageError(ValueError):
    """携带稳定错误码的图片查看失败。"""

    def __init__(self, code: str, message: str) -> None:
        """保存可供 Agent 判断的错误码和脱敏说明。"""
        super().__init__(message)
        self.code = code
        self.message = message


class ViewImageInput(BaseModel):  # type: ignore[misc]
    """图片查看工具的输入参数模型。"""

    url: Optional[str] = Field(
        None,
        description=(
            "HTTP(S) image URL or a data:image/...;base64,... URL. "
            "The remote URL must be publicly reachable and return a supported image."
        ),
    )
    file_path: Optional[str] = Field(
        None,
        description=(
            "Local image path. Non-administrator users may only read files under the Agent configuration directory."
        ),
    )
    image_data: Optional[Union[str, bytes]] = Field(
        None,
        description=(
            "Base64 image content, a data:image/...;base64,... URL, or raw image bytes. Use exactly one image source."
        ),
    )
    detail: Literal["auto", "low", "high"] = Field(
        "auto",
        description="Image detail hint passed to models that support it.",
    )

    @model_validator(mode="after")  # type: ignore[misc]
    def require_one_source(self) -> "ViewImageInput":
        """确保图片来源恰好是 URL、本地路径或图片内容中的一个。"""
        has_url = bool(self.url and self.url.strip())
        has_file_path = bool(self.file_path and self.file_path.strip())
        has_image_data = bool(self.image_data)
        if sum((has_url, has_file_path, has_image_data)) != 1:
            raise ValueError("provide exactly one of url, file_path, or image_data")
        return self


class ViewImageTool(MoviePilotTool):
    """读取远程或本地图片，并为支持视觉的模型生成真实图像输入。"""

    name: str = "view_image"
    tags: list[str] = [
        ToolTag.Read,
        ToolTag.File,
        ToolTag.Web,
    ]
    description: str = (
        "View an image so a multimodal model can inspect its actual pixels. "
        "Provide exactly one of url, file_path, or image_data. Supports public HTTP(S) "
        "image URLs, data:image/...;base64,... URLs, Base64/raw image content, and local image paths. Local access follows "
        "the Agent file permission boundary; private-network URLs are blocked. "
        "Use this when visual inspection is needed, and do not claim to have seen "
        "the image unless the tool returns an image observation."
    )
    args_schema: Type[BaseModel] = ViewImageInput

    def get_tool_message(self, **kwargs: Any) -> Optional[str]:
        """根据图片来源生成不泄露查询参数的工具提示。"""
        file_path = str(kwargs.get("file_path") or "").strip()
        url = str(kwargs.get("url") or "").strip()
        image_data = kwargs.get("image_data")
        if file_path:
            return f"查看本地图片: {Path(file_path).name or '未知文件'}"
        if url.lower().startswith("data:image/"):
            return "查看 data URL 图片"
        if url:
            try:
                parsed = urlsplit(url)
                label = f"{parsed.netloc}{parsed.path}" or parsed.netloc
            except ValueError:
                label = "远程图片 URL"
            return f"查看远程图片: {label[:256]}"
        if image_data:
            size = len(image_data) if hasattr(image_data, "__len__") else 0
            return f"查看图片内容: {size} chars/bytes"
        return "查看图片"

    def format_agent_result(self, result: Any, **tool_arguments: Any) -> Union[str, list[dict[str, Any]]]:
        """在通用文本截断前把成功结果转换成模型可接收的图像内容块。"""
        try:
            payload = json.loads(result) if isinstance(result, str) else result
            if not isinstance(payload, dict):
                raise ViewImageError("invalid_image_result", "图片响应不是对象")
            if inspect_tool_result(payload) is not ExecutionOutcome.SUCCEEDED:
                return super().format_agent_result(payload, **tool_arguments)
            if payload.get("success") is not True:
                raise ViewImageError("invalid_image_result", "图片响应未确认成功")

            encoded = payload.get("image_base64")
            if not isinstance(encoded, str) or not encoded:
                raise ViewImageError("invalid_image_result", "图片响应缺少图像数据")
            if len(encoded) > IMAGE_MAX_BASE64_CHARS:
                raise ViewImageError("image_too_large", "图片编码超过模型输入大小上限")
            image_bytes = base64.b64decode(encoded, validate=True)
            mime_type, width, height = self._validate_image_bytes(image_bytes)
            if payload.get("mime_type") != mime_type:
                raise ViewImageError("invalid_image_result", "图片 MIME 类型与实际内容不一致")

            metadata = {
                "tool": self.name,
                "success": True,
                "execution_outcome": ExecutionOutcome.SUCCEEDED.value,
                "source_type": payload.get("source_type") or "unknown",
                "source": str(payload.get("source_label") or "图片来源")[:256],
                "mime_type": mime_type,
                "width": width,
                "height": height,
                "byte_size": len(image_bytes),
                "note": "以下图像是 view_image 工具读取的图片观察；只能根据实际收到的图像回答。",
            }
            metadata_text = json.dumps(metadata, ensure_ascii=False, indent=2)
            if len(metadata_text) > IMAGE_METADATA_MAX_CHARS:
                raise ViewImageError("invalid_image_result", "图片元信息超过大小上限")

            detail = tool_arguments.get("detail", payload.get("detail", "auto"))
            if detail not in {"auto", "low", "high"}:
                detail = "auto"
            data_url = f"data:{mime_type};base64,{encoded}"
            if len(data_url) > IMAGE_MAX_BASE64_CHARS:
                raise ViewImageError("image_too_large", "图片编码超过模型输入大小上限")
            return [
                {"type": "text", "text": metadata_text},
                {"type": "image_url", "image_url": {"url": data_url, "detail": detail}},
            ]
        except (ValueError, TypeError, binascii.Error, Image.DecompressionBombError, OSError) as error:
            if isinstance(error, ViewImageError):
                failure = error
            else:
                failure = ViewImageError("invalid_image_result", "图片数据无效或超过大小上限")
            return self._failure(failure.code, failure.message)

    async def run(
        self,
        url: Optional[str] = None,
        file_path: Optional[str] = None,
        image_data: Optional[Union[str, bytes]] = None,
        detail: Literal["auto", "low", "high"] = "auto",
        **kwargs: Any,
    ) -> str:
        """读取并验证图片，返回供 Agent formatter 使用的结构化结果。"""
        del kwargs
        clean_url = str(url or "").strip()
        clean_file_path = str(file_path or "").strip()
        clean_image_data = image_data
        has_url = bool(clean_url)
        has_file_path = bool(clean_file_path)
        has_image_data = bool(clean_image_data)
        if sum((has_url, has_file_path, has_image_data)) != 1:
            return self._failure(
                "invalid_source",
                "请在 url、file_path 和 image_data 中恰好提供一个图片来源。",
            )

        source_type = "url" if has_url else "file" if has_file_path else "content"
        try:
            if has_file_path:
                resolved_path, access_error = await self._check_local_file_access(
                    clean_file_path,
                    operation="读取图片",
                )
                if access_error:
                    return self._failure("local_path_access_denied", access_error, source_type)
                if resolved_path is None:
                    return self._failure("local_path_invalid", "本地图片路径无效。", source_type)
                image_bytes = await self.run_blocking("storage", self._read_local_file, resolved_path)
                source_label = resolved_path.name or "本地图片"
            elif clean_url.lower().startswith("data:image/"):
                image_bytes = self._decode_data_url(clean_url)
                source_label = "data image"
            elif has_image_data:
                if clean_image_data is None:
                    raise ViewImageError("invalid_image", "图片内容不能为空。")
                image_bytes = self._decode_image_content(clean_image_data)
                source_label = "image content"
            else:
                await self._validate_remote_url(clean_url)
                image_bytes = await self._fetch_remote_image(clean_url)
                parsed = urlsplit(clean_url)
                source_label = f"{parsed.netloc}{parsed.path}"[:256]

            mime_type, width, height = self._validate_image_bytes(image_bytes)
            encoded = base64.b64encode(image_bytes).decode("ascii")
            if len(f"data:{mime_type};base64,{encoded}") > IMAGE_MAX_BASE64_CHARS:
                raise ViewImageError("image_too_large", "图片编码超过模型输入大小上限。")
            return self._success(
                source_type=source_type,
                source_label=source_label,
                mime_type=mime_type,
                width=width,
                height=height,
                byte_size=len(image_bytes),
                image_base64=encoded,
                detail=detail,
            )
        except ViewImageError as error:
            return self._failure(error.code, error.message, source_type)
        except FileNotFoundError, IsADirectoryError, PermissionError:
            return self._failure("local_file_unavailable", "本地图片不存在、不是文件或没有读取权限。", source_type)
        except Exception as error:  # noqa: BLE001 - 图片读取边界必须稳定返回失败合同
            logger.warning("Agent 查看图片失败: %s", type(error).__name__)
            return self._failure("image_read_failed", "读取图片失败，请检查图片来源和格式。", source_type)

    async def _validate_remote_url(self, url: str) -> None:
        """校验远程图片 URL 的协议、主机、DNS 和私网边界。"""
        if len(url) > IMAGE_MAX_URL_CHARS:
            raise ViewImageError("invalid_url", "图片 URL 过长。")
        try:
            parsed = urlsplit(url)
        except ValueError as error:
            raise ViewImageError("invalid_url", "图片 URL 无效。") from error
        if parsed.username or parsed.password or not parsed.netloc:
            raise ViewImageError("unsafe_url", "图片 URL 不允许包含用户凭据或无效主机。")
        try:
            BrowserSessionHelper.validate_url(url)
        except ValueError as error:
            raise ViewImageError("unsafe_url", str(error)) from error
        is_safe = await SecurityUtils.is_safe_url_async(
            url,
            {parsed.netloc},
            strict=True,
            block_private=True,
        )
        if not is_safe:
            raise ViewImageError("unsafe_url", "图片 URL 未通过公网地址安全校验。")

    async def _fetch_remote_image(self, url: str) -> bytes:
        """以流式方式下载远程图片，拒绝超大响应和未经校验的重定向。"""
        request = AsyncRequestUtils(
            accept_type="image/*",
            timeout=IMAGE_FETCH_TIMEOUT,
            follow_redirects=False,
        )
        current_url = url
        for redirect_count in range(IMAGE_MAX_REDIRECTS + 1):
            async with request.get_stream(current_url, raise_exception=False) as response:
                if response is None:
                    raise ViewImageError("image_download_failed", "无法下载远程图片。")
                if 300 <= response.status_code < 400:
                    location = response.headers.get("location")
                    if not location:
                        raise ViewImageError("image_redirect_failed", "远程图片重定向缺少目标地址。")
                    if redirect_count >= IMAGE_MAX_REDIRECTS:
                        raise ViewImageError("image_redirect_failed", "远程图片重定向次数超过上限。")
                    current_url = urljoin(current_url, location)
                    await self._validate_remote_url(current_url)
                    continue
                if response.status_code < 200 or response.status_code >= 300:
                    raise ViewImageError("image_download_failed", "远程图片服务器返回了失败状态。")
                content_length = response.headers.get("content-length")
                if content_length:
                    try:
                        content_length_value = int(content_length)
                    except ValueError:
                        content_length_value = 0
                    if content_length_value > IMAGE_MAX_BYTES:
                        raise ViewImageError("image_too_large", "远程图片超过大小上限。")

                chunks: list[bytes] = []
                total_size = 0
                async for chunk in response.aiter_bytes():
                    total_size += len(chunk)
                    if total_size > IMAGE_MAX_BYTES:
                        raise ViewImageError("image_too_large", "远程图片超过大小上限。")
                    chunks.append(chunk)
                return b"".join(chunks)
        raise ViewImageError("image_redirect_failed", "远程图片重定向次数超过上限。")

    @staticmethod
    def _read_local_file(path: Path) -> bytes:
        """在线程池中读取有限大小的本地图片字节。"""
        if not path.exists():
            raise FileNotFoundError(path)
        if not path.is_file():
            raise IsADirectoryError(path)
        with path.open("rb") as image_file:
            image_bytes = image_file.read(IMAGE_MAX_BYTES + 1)
        if len(image_bytes) > IMAGE_MAX_BYTES:
            raise ViewImageError("image_too_large", "本地图片超过大小上限。")
        return image_bytes

    @staticmethod
    def _decode_data_url(url: str) -> bytes:
        """解码受限的 base64 图片 data URL，不接受可执行或文本数据。"""
        header, separator, encoded = url.partition(",")
        if not separator or len(url) > IMAGE_MAX_BASE64_CHARS:
            raise ViewImageError("invalid_url", "图片 data URL 无效或超过大小上限。")
        parts = header.split(";")
        mime_type = parts[0][5:].lower() if parts and parts[0].lower().startswith("data:") else ""
        if mime_type not in {mime.lower() for mime in SUPPORTED_IMAGE_MIME_TYPES.values()}:
            raise ViewImageError("unsupported_image", "仅支持 JPEG、PNG、GIF 和 WEBP 图片。")
        if not any(part.lower() == "base64" for part in parts[1:]):
            raise ViewImageError("invalid_url", "图片 data URL 必须使用 base64 编码。")
        try:
            image_bytes = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as error:
            raise ViewImageError("invalid_url", "图片 data URL 的 base64 编码无效。") from error
        if not image_bytes:
            raise ViewImageError("invalid_image", "图片 data URL 为空。")
        return image_bytes

    @staticmethod
    def _decode_image_content(content: Union[str, bytes]) -> bytes:
        """解码纯 Base64、data URL 或原始图片字节，并限制输入大小。"""
        if isinstance(content, bytes):
            image_bytes = content
        elif isinstance(content, str):
            value = content.strip()
            if value.lower().startswith("data:"):
                image_bytes = ViewImageTool._decode_data_url(value)
            else:
                if len(value) > IMAGE_MAX_BASE64_CHARS:
                    raise ViewImageError("image_too_large", "图片编码超过大小上限。")
                try:
                    image_bytes = base64.b64decode("".join(value.split()), validate=True)
                except (ValueError, binascii.Error) as error:
                    raise ViewImageError("invalid_image", "图片 Base64 内容无效。") from error
        else:
            raise ViewImageError("invalid_image", "图片内容必须是 Base64 文本或原始字节。")
        if not image_bytes or len(image_bytes) > IMAGE_MAX_BYTES:
            raise ViewImageError("image_too_large", "图片为空或超过大小上限。")
        return image_bytes

    @staticmethod
    def _validate_image_bytes(image_bytes: bytes) -> tuple[str, int, int]:
        """验证真实图片格式、像素和解码结果，防止伪造 MIME 或损坏数据进入模型。"""
        if not image_bytes or len(image_bytes) > IMAGE_MAX_BYTES:
            raise ViewImageError("image_too_large", "图片为空或超过大小上限。")
        try:
            with Image.open(BytesIO(image_bytes)) as image:
                image_format = str(image.format or "").upper()
                mime_type = SUPPORTED_IMAGE_MIME_TYPES.get(image_format)
                if not mime_type:
                    raise ViewImageError("unsupported_image", "仅支持 JPEG、PNG、GIF 和 WEBP 图片。")
                width, height = image.size
                if width <= 0 or height <= 0 or width * height > IMAGE_MAX_PIXELS:
                    raise ViewImageError("image_too_large", "图片像素超过模型输入上限。")
                image.load()
        except ViewImageError:
            raise
        except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as error:
            raise ViewImageError("invalid_image", "图片数据无效或已损坏。") from error
        return mime_type, width, height

    @staticmethod
    def _success(**payload: Any) -> str:
        """生成包含完整 base64 图像数据的内部成功结果。"""
        return json.dumps(
            {"success": True, "execution_outcome": ExecutionOutcome.SUCCEEDED.value, **payload},
            ensure_ascii=False,
        )

    @staticmethod
    def _failure(code: str, message: str, source_type: Optional[str] = None) -> str:
        """生成不携带原始图片数据的结构化失败结果。"""
        payload: dict[str, Any] = {
            "success": False,
            "execution_outcome": ExecutionOutcome.FAILED.value,
            "error": code,
            "message": message,
        }
        if source_type:
            payload["source_type"] = source_type
        return json.dumps(payload, ensure_ascii=False)
