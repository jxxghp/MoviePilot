"""
验证媒体图片格式识别、WebP 转换和无效内容拒绝。
"""

from io import BytesIO

import pytest
from PIL import Image

from app.domain.artwork import normalize_artwork


@pytest.mark.parametrize("image_format,expected_suffix", [("JPEG", ".jpg"), ("PNG", ".png"), ("WEBP", ".png")])
def test_normalize_artwork_uses_actual_format(image_format, expected_suffix) -> None:
    """
    扩展名由实际图片内容决定；JPEG 和 PNG 不重编码，WebP 转成可读取的 PNG。
    """
    source = BytesIO()
    Image.new("RGB", (2, 2), color="blue").save(source, format=image_format)
    normalized, suffix = normalize_artwork(source.getvalue())

    assert suffix == expected_suffix
    if image_format != "WEBP":
        assert normalized == source.getvalue()
    with Image.open(BytesIO(normalized)) as image:
        assert image.format == ("JPEG" if suffix == ".jpg" else "PNG")
        assert image.size == (2, 2)


def test_webp_conversion_preserves_transparency() -> None:
    """
    透明 WebP 转成 PNG 后保留透明度，避免 Logo 出现不透明底色。
    """
    source = BytesIO()
    Image.new("RGBA", (2, 2), color=(10, 20, 30, 0)).save(source, format="WEBP", lossless=True)
    normalized, suffix = normalize_artwork(source.getvalue())

    assert suffix == ".png"
    with Image.open(BytesIO(normalized)) as image:
        assert image.convert("RGBA").getpixel((0, 0))[3] == 0


@pytest.mark.parametrize("content", [b"", b"<html>not artwork</html>", b"RIFF0000WEBPbroken"])
def test_normalize_artwork_rejects_invalid_content(content) -> None:
    """
    无效响应不能以图片扩展名写入媒体目录。
    """
    with pytest.raises(ValueError):
        normalize_artwork(content)
