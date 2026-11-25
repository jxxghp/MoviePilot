"""
验证媒体服务器缩略图参数在原图鉴权后应用，并保留原有缓存与鉴权信息。
"""

from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from app.api.endpoints import system
from app.application.image import ImageHelper


@pytest.mark.parametrize("path", ["/Items/1/Images/Primary", "/emby/Items/abc/Images/Backdrop/0", "/jellyfin/items/1/images/Thumb"])
def test_thumbnail_url_keeps_identity_and_auth_parameters(path: str) -> None:
    """
    海报与背景图仅改变缩略参数，保留媒体条目、标签与鉴权字段。
    """
    url = f"https://media.test{path}?tag=version-one&api_key=test-key&unknown=a%2Bb"
    result = urlsplit(ImageHelper.thumbnail_url(url, 500, 80))
    assert result.netloc == "media.test"
    assert result.path == path
    assert parse_qs(result.query) == {
        "tag": ["version-one"], "api_key": ["test-key"], "unknown": ["a+b"],
        "maxWidth": ["500"], "quality": ["80"],
    }


@pytest.mark.parametrize("url", [
    "https://image.tmdb.org/t/p/original/poster.jpg",
    "https://example.test/images/photo.jpg?width=2000",
    "https://media.test/Items/1/stream",
])
def test_other_image_protocols_keep_the_original_url(url: str) -> None:
    """
    未声明同一种图片协议的地址不被猜测性改写。
    """
    assert ImageHelper.thumbnail_url(url, 500, 80) == url


def test_thumbnail_url_preserves_smaller_variants_and_uses_a_separate_cache_key() -> None:
    """
    已有更小的图片不被放大，大小写重复参数归一化且不同尺寸使用不同缓存键。
    """
    url = "https://media.test/Items/1/Images/Primary?MAXWIDTH=160&maxWidth=800&Quality=60&tag=v1"
    small = ImageHelper.thumbnail_url(url, 500, 80)
    assert parse_qs(urlsplit(small).query) == {"maxWidth": ["160"], "quality": ["60"], "tag": ["v1"]}
    assert ImageHelper.thumbnail_url(url) == url
    large = ImageHelper.thumbnail_url("https://media.test/Items/1/Images/Primary?tag=v1", 500, 80)
    assert ImageHelper._prepare_cache_path(small) != ImageHelper._prepare_cache_path(large)


@pytest.mark.asyncio
async def test_proxy_validates_original_signed_url_before_requesting_a_thumbnail(monkeypatch) -> None:
    """
    URL 签名与访问策略先校验原地址，随后才请求同一主机上的缩略版本。
    """
    original = "https://media.test/Items/1/Images/Primary?tag=v1&mp_signature=test"
    clean = "https://media.test/Items/1/Images/Primary?tag=v1"
    validate = AsyncMock(return_value=True)
    fetch = AsyncMock(return_value=(b"thumbnail", "image/jpeg"))
    monkeypatch.setattr(system.SecurityUtils, "is_safe_image_url_async", validate)
    monkeypatch.setattr(system.SecurityUtils, "strip_url_signature", lambda _url: clean)
    monkeypatch.setattr(system, "_get_image_proxy_trusted_hosts", lambda: {"media.test"})
    monkeypatch.setattr(system, "_get_image_proxy_allowed_domains", AsyncMock(return_value={"media.test"}))
    monkeypatch.setattr(ImageHelper, "async_fetch_image_with_mime_type", fetch)
    response = await system.proxy_img(original, max_width=500, quality=80)
    validate.assert_awaited_once()
    assert validate.await_args.args[0] == original
    assert parse_qs(urlsplit(fetch.await_args.kwargs["url"]).query) == {"tag": ["v1"], "maxWidth": ["500"], "quality": ["80"]}
    assert response is not None and response.body == b"thumbnail"
    assert response.headers["cache-control"].startswith("private,")
    assert response.headers["vary"] == "Cookie, Authorization"
    validate.return_value = False
    fetch.reset_mock()
    assert await system.proxy_img(original, max_width=500, quality=80) is None
    fetch.assert_not_awaited()


@pytest.mark.parametrize("params", [
    {"maxWidth": 0}, {"maxWidth": 2049}, {"maxWidth": "bad"}, {"quality": -1}, {"quality": 101},
])
def test_thumbnail_query_limits_reject_invalid_dimensions(params: dict[str, object]) -> None:
    """
    HTTP 查询边界限制尺寸与质量，非法值不得进入图片获取逻辑。
    """
    app = FastAPI()
    app.dependency_overrides[system.verify_resource_token] = lambda: None
    app.get("/img/{proxy}", response_model=None)(system.proxy_img)
    response = TestClient(app).get("/img/0", params={"imgurl": "https://media.test/poster.jpg", **params})
    assert response.status_code == 422
