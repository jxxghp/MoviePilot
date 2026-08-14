"""识别图形验证码工具。"""

import json
from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.adapters.network.browser import BrowserSessionHelper
from app.adapters.external.ocr import OcrHelper
from app.runtime.log import logger


class RecognizeCaptchaInput(BaseModel):
    """识别图形验证码工具的输入参数模型。"""

    image_url: str = Field(
        ...,
        description=(
            "Captcha image URL obtained from the browser page, usually an img.src value. "
            "Supports http/https URLs and data:image/...;base64,... URLs."
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
        "Pass cookie and user_agent when the image URL requires the current browser session. "
        "Supports http/https image URLs and data:image/...;base64,... URLs. "
        "For safety, localhost and private network URLs are blocked by default unless "
        "allow_private_network is true."
    )
    args_schema: Type[BaseModel] = RecognizeCaptchaInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据验证码图片参数生成友好的提示消息。"""
        image_url = str(kwargs.get("image_url") or "")
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
        image_url: str,
        cookie: Optional[str] = None,
        user_agent: Optional[str] = None,
        allow_private_network: bool = False,
    ) -> str:
        """
        在线程池中下载并识别验证码图片。

        :param image_url: 验证码图片地址
        :param cookie: 下载图片时使用的 Cookie
        :param user_agent: 下载图片时使用的 User-Agent
        :param allow_private_network: 是否允许访问本机或私网地址
        :return: 验证码文本，失败时返回空字符串
        """
        clean_url = (image_url or "").strip()
        if not clean_url:
            return ""
        if not clean_url.lower().startswith("data:image/"):
            BrowserSessionHelper.validate_url(
                clean_url,
                allow_private_network=allow_private_network,
            )
        return OcrHelper().get_captcha_text(
            image_url=clean_url,
            cookie=cookie,
            ua=user_agent,
        )

    async def run(
        self,
        image_url: str,
        cookie: Optional[str] = None,
        user_agent: Optional[str] = None,
        allow_private_network: bool = False,
        **kwargs,
    ) -> str:
        """
        识别指定图片地址中的图形验证码文本。

        :param image_url: 验证码图片地址
        :param cookie: 下载图片时使用的 Cookie
        :param user_agent: 下载图片时使用的 User-Agent
        :param allow_private_network: 是否允许访问本机或私网地址
        :return: JSON 格式的识别结果
        """
        logger.info(
            f"执行工具: {self.name}, "
            f"参数: image_url={self._format_image_url_for_log(image_url)}"
        )

        try:
            captcha_text = await self.run_blocking(
                "web",
                self._recognize_captcha_sync,
                image_url,
                cookie,
                user_agent,
                allow_private_network,
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
            return json.dumps(
                {
                    "success": False,
                    "captcha_text": "",
                    "message": "验证码识别失败或未返回内容",
                },
                ensure_ascii=False,
            )
        except ValueError as err:
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
            return json.dumps(
                {
                    "success": False,
                    "captcha_text": "",
                    "message": f"识别图形验证码时发生错误: {str(err)}",
                },
                ensure_ascii=False,
            )
