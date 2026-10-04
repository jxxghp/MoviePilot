"""识别图形验证码工具。"""

import json
from typing import Optional, Type

from pydantic import BaseModel, Field, model_validator

from app.adapters.external.ocr import OcrHelper
from app.adapters.network.browser import BrowserSessionHelper
from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.runtime.log import logger


class CaptchaUrlError(ValueError):
    """图片地址被安全校验拒绝，不能建议改用其他工具绕过该边界。"""


class RecognizeCaptchaInput(BaseModel):
    """识别图形验证码工具的输入参数模型。"""

    image_url: Optional[str] = Field(
        None,
        description=(
            "Captcha image URL obtained from the browser page, usually an img.src value. "
            "Supports http/https URLs and data:image/...;base64,... URLs."
        ),
    )
    image_data: Optional[bytes] = Field(
        None,
        description=(
            "Raw image bytes already obtained by the caller. Prefer this when the browser "
            "or another tool provides binary image data; it is sent to OCR without a Base64 conversion."
        ),
    )
    cookie: Optional[str] = Field(
        None,
        description=(
            "Optional Cookie header used to download the captcha image when the image URL "
            "requires the same authenticated browser session."
        ),
    )
    user_agent: Optional[str] = Field(
        None,
        description="Optional User-Agent used when downloading the captcha image.",
    )
    allow_private_network: bool = Field(
        False,
        description="Allow captcha image URLs on localhost, loopback, private, or link-local addresses.",
    )

    @model_validator(mode="after")  # type: ignore[misc]
    def require_image_source(self) -> "RecognizeCaptchaInput":
        """确保验证码工具至少收到图片地址或原始图片字节。"""
        if not self.image_url and not self.image_data:
            raise ValueError("image_url or image_data is required")
        return self


class RecognizeCaptchaTool(MoviePilotTool):
    """
    图形验证码识别工具，供 Agent 在浏览器自动化登录时读取验证码文本。
    """

    name: str = "recognize_captcha"
    tags: list[str] = [
        ToolTag.Read,
        ToolTag.Web,
        ToolTag.Site,
    ]
    description: str = (
        "Recognize a graphic captcha image and return the captcha text. "
        "Use this after browser automation extracts a captcha img.src from the page. "
        "It also accepts raw image_data bytes and sends them directly to OCR without Base64 conversion. "
        "Pass cookie and user_agent when the image URL requires the current browser session. "
        "Supports http/https image URLs and data:image/...;base64,... URLs. "
        "This tool uses the configured OCR service, not the multimodal model. If OCR fails, "
        "inspect the current browser captcha with browse_webpage(action='screenshot', selector=...) "
        "before refreshing it or asking the user to read it. "
        "For safety, localhost and private network URLs are blocked by default unless "
        "allow_private_network is true."
    )
    args_schema: Type[BaseModel] = RecognizeCaptchaInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据验证码图片参数生成友好的提示消息。"""
        image_url = str(kwargs.get("image_url") or "")
        image_data = kwargs.get("image_data")
        if image_data:
            return f"识别图形验证码: raw image ({len(image_data)} bytes)"
        if image_url.lower().startswith("data:image/"):
            return "识别图形验证码: data image"
        return f"识别图形验证码: {image_url}"

    @staticmethod
    def _format_image_url_for_log(image_url: str) -> str:
        """生成验证码图片地址的安全日志摘要，避免 data URL 图片刷屏。"""
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
        """OCR 失败仍保留失败状态，并引导模型先观察同一浏览器验证码而非立即刷新。"""
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

    async def run(
        self,
        image_url: Optional[str] = None,
        cookie: Optional[str] = None,
        user_agent: Optional[str] = None,
        allow_private_network: bool = False,
        image_data: Optional[bytes] = None,
        **kwargs,
    ) -> str:
        """
        识别指定图片地址中的图形验证码文本。

        :param image_url: 验证码图片地址
        :param image_data: 已取得的原始验证码图片字节
        :param cookie: 下载图片时使用的 Cookie
        :param user_agent: 下载图片时使用的 User-Agent
        :param allow_private_network: 是否允许访问本机或私网地址
        :return: JSON 格式的识别结果
        """
        logger.info(
            f"执行工具: {self.name}, "
            f"参数: image_url={self._format_image_url_for_log(image_url or '')}, "
            f"image_data={'%s bytes' % len(image_data) if image_data else 'none'}"
        )

        try:
            captcha_text = await self.run_blocking(
                "web",
                self._recognize_captcha_sync,
                image_url,
                cookie,
                user_agent,
                allow_private_network,
                image_data,
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
