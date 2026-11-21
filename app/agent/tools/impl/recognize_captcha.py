"""
识别图形验证码工具。
"""

import base64
import binascii
import json
from typing import Any, Optional, Type, Union

from pydantic import BaseModel, Field, model_validator

from app.adapters.external.ocr import OcrHelper
from app.adapters.network.browser import BrowserSessionHelper
from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.runtime.log import logger

IMAGE_MAX_BASE64_CHARS = 1024 * 1024
IMAGE_MAX_BYTES = 768 * 1024


class CaptchaUrlError(ValueError):
    """
    图片地址被安全校验拒绝，不能建议改用其他工具绕过该边界。
    """


class RecognizeCaptchaInput(BaseModel):
    """
    识别图形验证码工具的输入参数模型。
    """

    image_url: Optional[str] = Field(
        None,
        description="从浏览器页面获得的验证码图片地址，通常为 img.src；支持 HTTP(S) 和 data:image/...;base64,...",
    )
    image_data: Optional[str] = Field(
        None,
        description="Base64 图片内容或 data:image/...;base64,...；调用方已取得图片内容时使用，执行层显式解码后提交 OCR",
    )
    cookie: Optional[str] = Field(
        None,
        description="下载验证码需要同一已登录浏览器会话时使用的可选 Cookie 请求头",
    )
    user_agent: Optional[str] = Field(
        None,
        description="下载验证码图片时使用的可选 User-Agent",
    )
    allow_private_network: bool = Field(
        False,
        description="是否允许本机、回环、私网或链路本地验证码地址；仅管理员可启用，其他调用者返回 admin_required",
    )

    # Pydantic 校验器由运行时注册，保留第三方装饰器所需的动态类型兼容。
    @model_validator(mode="after")
    def require_image_source(self) -> "RecognizeCaptchaInput":
        """
        确保验证码工具至少收到图片地址或图片内容。
        """
        if not self.image_url and not self.image_data:
            raise ValueError("image_url or image_data is required")
        return self


class RecognizeCaptchaTool(MoviePilotTool):
    """
    图形验证码识别工具，供 Agent 在浏览器自动化登录时读取验证码文本。
    """

    name: str = Field(
        default="recognize_captcha",
        description="供模型调用的稳定工具名称",
    )
    tags: list[str] = Field(
        default=[ToolTag.Read, ToolTag.Web, ToolTag.Site],
        description="工具的能力和权限分类标签，初始化时会统一归一化",
    )
    description: str = Field(
        default=(
            "识别图片验证码并返回文字。"
            "浏览器取得页面验证码 img.src 后可调用；"
            "也接受 image_data 提供的 Base64 图片内容，执行层解码后提交 OCR。"
            "图片地址依赖当前浏览器会话时应传 cookie 和 user_agent。"
            "支持 HTTP(S) 及 data:image/...;base64,... 地址。"
            "使用已配置的 OCR 服务，不使用多模态模型。"
            "OCR 失败时，应先用 browse_webpage(action='screenshot', selector=...) 查看浏览器当前验证码，再决定刷新或请用户识别。"
            "默认禁止本机与私网 URL，只有管理员可将 allow_private_network 设为 true。"
        ),
        description="供模型理解工具用途、输入约束和调用边界的说明",
    )
    args_schema: Type[BaseModel] = Field(
        default=RecognizeCaptchaInput,
        description="工具输入参数的验证模型或 JSON Schema",
    )

    def get_tool_message(self, **kwargs: Any) -> Optional[str]:
        """
        根据验证码图片参数生成友好的提示消息。
        """
        image_url = str(kwargs.get("image_url") or "")
        image_data = kwargs.get("image_data")
        if isinstance(image_data, bytes):
            return f"识别图形验证码: raw image ({len(image_data)} bytes)"
        if image_data:
            return f"识别图形验证码: image content ({len(image_data)} chars)"
        if image_url.lower().startswith("data:image/"):
            return "识别图形验证码: data image"
        return f"识别图形验证码: {image_url}"

    @staticmethod
    def _decode_image_data(image_data: Optional[Union[str, bytes]]) -> Optional[bytes]:
        """
        把模型传入的 Base64 图片内容解码为原始字节；内部直接传入的字节原样保留。

        :param image_data: Base64 图片内容、data:image/...;base64,... 或原始图片字节
        :return: 解码后的原始图片字节，未提供时返回 None
        """
        if image_data is None:
            return None
        if isinstance(image_data, bytes):
            return image_data
        value = image_data.strip()
        if not value:
            return None
        if value.lower().startswith("data:image/"):
            header, separator, encoded = value.partition(",")
            if not separator or "base64" not in header.lower():
                raise ValueError("image_data 不是有效的 data:image/...;base64,... 内容")
            value = encoded.strip()
        if len(value) > IMAGE_MAX_BASE64_CHARS:
            raise ValueError("image_data 图片内容超过大小上限")
        normalized = "".join(value.split())
        padding_size = len(normalized) % 4
        if padding_size:
            normalized = f"{normalized}{'=' * (4 - padding_size)}"
        try:
            image_bytes = base64.b64decode(normalized, validate=True)
        except (ValueError, binascii.Error) as error:
            raise ValueError("image_data 不是有效的 Base64 图片内容") from error
        if not image_bytes or len(image_bytes) > IMAGE_MAX_BYTES:
            raise ValueError("image_data 图片内容为空或超过大小上限")
        return image_bytes

    @staticmethod
    def _format_image_url_for_log(image_url: str) -> str:
        """
        生成验证码图片地址的安全日志摘要，避免 data URL 图片刷屏。
        """
        clean_url = (image_url or "").strip()
        if not clean_url:
            return ""
        if clean_url.lower().startswith("data:image/"):
            metadata, separator, data = clean_url.partition(",")
            if separator:
                return f"{metadata},<base64:{len(data)} chars>"
            return f"data:image,<invalid:{len(clean_url)} chars>"
        if len(clean_url) > 300:
            return f"{clean_url[:300]}...(已截断，总长度: {len(clean_url)})"
        return clean_url

    @staticmethod
    def _recognize_captcha_sync(
        image_url: Optional[str] = None,
        cookie: Optional[str] = None,
        user_agent: Optional[str] = None,
        allow_private_network: bool = False,
        image_data: Optional[bytes] = None,
    ) -> str:
        """
        在线程池中下载并识别验证码图片。

        :param image_url: 验证码图片地址
        :param image_data: 已取得的原始验证码图片字节
        :param cookie: 下载图片时使用的 Cookie
        :param user_agent: 下载图片时使用的 User-Agent
        :param allow_private_network: 是否允许访问本机或私网地址
        :return: 验证码文本，失败时返回空字符串
        """
        clean_url = (image_url or "").strip()
        if image_data:
            return OcrHelper().get_captcha_text(
                image_data=image_data,
                cookie=cookie,
                ua=user_agent,
            )
        if not clean_url:
            return ""
        if not clean_url.lower().startswith("data:image/"):
            try:
                BrowserSessionHelper.validate_url(
                    clean_url,
                    allow_private_network=allow_private_network,
                )
            except ValueError as error:
                raise CaptchaUrlError(str(error)) from error
        return OcrHelper().get_captcha_text(image_url=clean_url, cookie=cookie, ua=user_agent)

    @staticmethod
    def _recognition_failure(message: str) -> str:
        """
        OCR 失败仍保留失败状态，并引导模型先观察同一浏览器验证码而非立即刷新。
        """
        return json.dumps(
            {
                "success": False,
                "captcha_text": "",
                "message": message,
                "recovery": (
                    "若验证码来自当前浏览器页面，先保持同一 session_key 和标签页，"
                    "调用 browse_webpage(action='screenshot', selector='已观察到的验证码元素选择器')；"
                    "无法确定选择器时可省略 selector 截取当前视口。"
                    "不要先刷新页面或重新请求验证码图片地址，以免改变当前验证码。"
                    "若已持有图片内容，可用 view_image(image_data=...)；"
                    "仅对不依赖浏览器会话的独立公开图片使用 view_image(url=...)。"
                    "只有收到真实图像观察后才读取验证码，填写后验证网站是否接受。"
                    "若模型未接收到图片或无法看清，不要猜测；按 browser-use 技能有限重试或请求手工输入。"
                ),
            },
            ensure_ascii=False,
        )

    async def run(  # type: ignore[override]
        self,
        image_url: Optional[str] = None,
        cookie: Optional[str] = None,
        user_agent: Optional[str] = None,
        allow_private_network: bool = False,
        image_data: Optional[Union[str, bytes]] = None,
        **kwargs: object,
    ) -> str:
        """
        识别指定图片地址中的图形验证码文本。

        :param image_url: 验证码图片地址
        :param image_data: Base64 图片内容、data:image/...;base64,... 或原始图片字节
        :param cookie: 下载图片时使用的 Cookie
        :param user_agent: 下载图片时使用的 User-Agent
        :param allow_private_network: 是否允许访问本机或私网地址
        :return: JSON 格式的识别结果
        """
        try:
            image_bytes = self._decode_image_data(image_data)
        except ValueError as err:
            logger.warning(f"验证码图片内容无效: {str(err)}")
            return json.dumps(
                {
                    "success": False,
                    "captcha_text": "",
                    "message": str(err),
                    "recovery": "请提供有效的 Base64 图片内容或 data:image/...;base64,...，或改用 image_url 传入图片地址。",
                },
                ensure_ascii=False,
            )

        logger.info(
            f"执行工具: {self.name}, "
            f"参数: image_url={self._format_image_url_for_log(image_url or '')}, "
            f"image_data={'%s bytes' % len(image_bytes) if image_bytes else 'none'}"
        )

        if allow_private_network and not await self.is_admin_user():
            return json.dumps(
                {
                    "success": False,
                    "captcha_text": "",
                    "error": "admin_required",
                    "message": "allow_private_network 仅允许管理员使用",
                    "recovery": "去掉 allow_private_network 后只识别公网地址或 data:image 图片，或请求管理员授权。",
                },
                ensure_ascii=False,
            )

        try:
            captcha_text = await self.run_blocking(
                "web",
                self._recognize_captcha_sync,
                image_url,
                cookie,
                user_agent,
                allow_private_network,
                image_bytes,
            )
            if captcha_text:
                return json.dumps(
                    {
                        "success": True,
                        "captcha_text": captcha_text,
                        "message": "验证码识别成功",
                    },
                    ensure_ascii=False,
                )
            return self._recognition_failure("验证码识别失败或未返回内容")
        except CaptchaUrlError as err:
            logger.warning(f"验证码图片地址校验失败: {str(err)}")
            return json.dumps(
                {
                    "success": False,
                    "captcha_text": "",
                    "message": str(err),
                },
                ensure_ascii=False,
            )
        except Exception as err:
            logger.error(f"识别图形验证码失败: {str(err)}", exc_info=True)
            return self._recognition_failure("OCR 服务调用失败，尚未识别验证码")
