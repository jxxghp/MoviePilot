"""
媒体图片内容与落盘格式的纯转换规则。
"""

import re
from io import BytesIO
from xml.etree import ElementTree

from PIL import Image


def normalize_artwork(content: bytes) -> tuple[bytes, str]:
    """
    验证图片并按实际内容选择落盘格式。

    JPEG 和 PNG 保留原始字节，WebP 无损转为 PNG，以保留透明度并避免远程
    URL 后缀决定本地格式。有效 SVG 保留原始字节和矢量格式，供支持 SVG 的
    媒体服务器读取；不支持 SVG 的媒体服务器可能忽略该图片。本函数只处理内存
    数据，不访问文件或网络。

    :param content: 完整下载的图片字节
    :return: 规范图片字节与小写后缀；JPEG 统一使用 ``.jpg``
    :raises ValueError: 图片无效或格式不是 JPEG、PNG、WebP、SVG
    """
    svg_probe = content.lstrip(b"\xef\xbb\xbf \t\r\n")
    if svg_probe.startswith((b"<?xml", b"<!--", b"<svg")):
        # SVG 不需要 DTD 或实体声明，拒绝它们以免解析不可信图片时扩展实体。
        if re.search(rb"<!\s*(?:DOCTYPE|ENTITY)\b", content, flags=re.IGNORECASE):
            raise ValueError("不支持包含文档类型或实体声明的 SVG 图片")
        try:
            root = ElementTree.fromstring(content)
        except ElementTree.ParseError:
            pass
        else:
            namespace, separator, local_name = root.tag.rpartition("}")
            if (
                local_name == "svg"
                and (not separator or namespace == "{http://www.w3.org/2000/svg")
            ):
                return content, ".svg"

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
