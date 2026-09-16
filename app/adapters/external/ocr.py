import base64
import binascii
from typing import Optional

from app.adapters.network.http import RequestUtils
from app.runtime.settings import get_runtime_setting


class OcrHelper:
    """
    OCR 辅助类，负责获取验证码图片并调用 OCR 服务识别文本。
    """

    def __init__(self, ocr_base_url: Optional[str] = None) -> None:
        """初始化 OCR 服务地址，优先使用组合根设置快照。"""
        if ocr_base_url is None:
            ocr_base_url = get_runtime_setting('OCR_HOST')
        base_url = str(ocr_base_url).rstrip('/')
        self._ocr_b64_url = f"{base_url}/captcha/base64"
        self._ocr_image_url = f"{base_url}/captcha/image"

    def get_captcha_text(
            self,
            image_url: Optional[str] = None,
            image_b64: Optional[str] = None,
            cookie: Optional[str] = None,
            ua: Optional[str] = None,
            image_data: Optional[bytes] = None,
    ) -> str:
        """
        获取验证码图片并识别内容，优先使用原始图片字节接口。
        :param image_url: 图片地址
        :param image_b64: 图片base64，跳过图片地址下载
        :param cookie: 下载图片使用的cookie
        :param ua: 下载图片使用的ua
        :param image_data: 已取得的原始图片字节，跳过下载和 Base64 编码
        :return: 验证码识别结果，失败时返回空字符串
        """
        raw_image = image_data or b""
        if not raw_image and image_url:
            data_url_b64 = self._extract_data_url_base64(image_url)
            if data_url_b64:
                try:
                    raw_image = base64.b64decode(
                        self._normalize_image_base64(data_url_b64),
                        validate=True,
                    )
                except (ValueError, binascii.Error):
                    return ""
            else:
                ret = RequestUtils(ua=ua,
                                   cookies=cookie).get_res(image_url)
                if ret is not None:
                    raw_image = ret.content or b""
                    if not raw_image:
                        return ""
        if raw_image:
            ret = RequestUtils(content_type="application/octet-stream").post_res(
                url=self._ocr_image_url,
                data=raw_image,
            )
        else:
            image_b64 = self._normalize_image_base64(image_b64)
            if not image_b64:
                return ""
            ret = RequestUtils(content_type="application/json").post_res(
                url=self._ocr_b64_url,
                json={"base64_img": image_b64},
            )
        if ret:
            return ret.json().get("result") or ""
        return ""

    @staticmethod
    def _normalize_image_base64(image_b64: Optional[str]) -> str:
        """规范化外部传入的图片 base64 内容。"""
        if not image_b64:
            return ""
        clean_image_b64 = OcrHelper._extract_data_url_base64(image_b64) or image_b64
        clean_image_b64 = "".join(clean_image_b64.split())
        if not clean_image_b64:
            return ""
        padding_size = len(clean_image_b64) % 4
        if padding_size:
            clean_image_b64 = f"{clean_image_b64}{'=' * (4 - padding_size)}"
        return clean_image_b64

    @staticmethod
    def _extract_data_url_base64(image_url: Optional[str]) -> str:
        """从 data:image/...;base64,... 地址中提取纯 base64 内容。"""
        image_url = (image_url or "").strip()
        if not image_url.lower().startswith("data:image/"):
            return ""
        metadata, separator, data = image_url.partition(",")
        if not separator or ";base64" not in metadata.lower():
            return ""
        return data.strip()
