"""
媒体图片内容与落盘格式的纯转换规则。
"""

from io import BytesIO

from PIL import Image


def normalize_artwork(content: bytes) -> tuple[bytes, str]:
    """
    验证图片并按实际内容选择媒体服务器通用的文件后缀。

    JPEG 和 PNG 保留原始字节，WebP 无损转为 PNG，以保留透明度并避免远程
    URL 后缀决定本地格式。本函数只处理内存数据，不访问文件或网络。

    :param content: 完整下载的图片字节
    :return: 规范图片字节与小写后缀；JPEG 统一使用 ``.jpg``
    :raises ValueError: 图片无效或格式不是 JPEG、PNG、WebP
    """
    try:
        with Image.open(BytesIO(content)) as image:
            image_format = image.format
            if image_format in {"JPEG", "PNG"}:
                image.verify()
                return content, ".jpg" if image_format == "JPEG" else ".png"
            if image_format != "WEBP":
                raise ValueError(f"不支持的媒体图片格式：{image_format}")
            with BytesIO() as output:
                image.save(output, format="PNG")
                return output.getvalue(), ".png"
    except (OSError, SyntaxError) as err:
        raise ValueError("下载内容不是有效的媒体图片") from err
