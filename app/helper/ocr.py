import base64

from app.core.config import settings
from app.utils.http import RequestUtils


class OcrHelper:

    _ocr_b64_url = f"{settings.OCR_HOST}/captcha/base64"

    def get_captcha_text(self, image_url=None, image_b64=None, cookie=None, ua=None):
        """
        根据图片地址，获取验证码图片，并识别内容
        :param image_url: 图片地址
        :param image_b64: 图片base64，跳过图片地址下载
        :param cookie: 下载图片使用的cookie
        :param ua: 下载图片使用的ua
        """
        if image_url:
            ret = RequestUtils(ua=ua,
                               cookies=cookie).get_res(image_url)
            if ret is not None:
                image_bin = ret.content
                if not image_bin:
                    return ""
                image_b64 = base64.b64encode(image_bin).decode()
        if not image_b64:
            return ""
        ret = RequestUtils(content_type="application/json").post_res(
            url=self._ocr_b64_url,
            json={"base64_img": image_b64})
        if ret:
            return ret.json().get("result")
        return ""
