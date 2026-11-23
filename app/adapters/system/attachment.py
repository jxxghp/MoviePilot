"""
通知图片来源读取与短期上传文件管理，不保留远程图片或二维码缓存。
"""

import base64
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Optional

from app.adapters.network.http import RequestUtils
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting

# 统一图片输入最多读取 10 MiB；平台更严格的限制由各发送客户端处理。
IMAGE_MAX_BYTES = 10 * 1024 * 1024


def _download_image(url: str) -> Optional[bytes]:
    """
    有界读取插件提供的 HTTP(S) 图片，并始终释放流式响应。
    """
    response = RequestUtils(timeout=30).get_res(url, stream=True)
    if response is None:
        return None
    try:
        if response.status_code != 200:
            return None
        content = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if len(content) + len(chunk) > IMAGE_MAX_BYTES:
                return None
            content.extend(chunk)
        return bytes(content)
    finally:
        response.close()


def load_image(image: str) -> Optional[tuple[bytes, str]]:
    """
    从 URL、Base64 数据地址或本地路径读取 JPG/PNG 原图。

    :param image: 插件明确提供的图片来源；本地路径支持展开用户目录
    :return: 原始字节和匹配内容的文件后缀；格式、大小或读取失败返回 None
    """
    try:
        content: Optional[bytes]
        if image.startswith("data:image/"):
            header, encoded = image.split(",", 1)
            if not header.endswith(";base64") or len(encoded) > ((IMAGE_MAX_BYTES + 2) // 3) * 4:
                return None
            content = base64.b64decode(encoded, validate=True)
        elif image.startswith(("https://", "http://")):
            content = _download_image(image)
        elif "://" not in image:
            with Path(image).expanduser().open("rb") as image_file:
                content = image_file.read(IMAGE_MAX_BYTES + 1)
        else:
            return None
        if not content or not 5 < len(content) <= IMAGE_MAX_BYTES:
            return None
        if content.startswith(b"\x89PNG\r\n\x1a\n"):
            return content, ".png"
        if content.startswith(b"\xff\xd8\xff"):
            return content, ".jpg"
    except Exception as error:
        # 不记录图片地址、Base64 或本地路径，避免泄露二维码来源中的临时凭据。
        logger.warning(f"读取通知原图失败：{type(error).__name__}")
    return None


@contextmanager
def image_upload_file(image: str) -> Iterator[Optional[Path]]:
    """
    为仅接受文件路径的渠道提供短期原图，发送结束后清理且不删除调用方文件。

    :param image: HTTP(S)、Base64 或本地 JPG/PNG 图片
    :yields: 位于运行时临时目录的图片副本；无法读取时为 None
    """
    loaded = load_image(image)
    if loaded is None:
        logger.warning("通知原图不可用，仅支持不超过 10 MiB 的 JPG/PNG")
        yield None
        return
    content, suffix = loaded
    temp_root = Path(get_runtime_setting("TEMP_PATH"))
    temp_root.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="message-image-", dir=temp_root) as directory:
        path = Path(directory) / f"image{suffix}"
        path.write_bytes(content)
        yield path
