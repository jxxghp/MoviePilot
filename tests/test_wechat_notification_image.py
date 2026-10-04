"""企业微信豆瓣图片代理的签名、取图和消息载荷测试。"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import HTTPException, Response

from app.api.endpoints import system as system_endpoint
from app.application.messaging import image as image_urls
from app.modules.wechat.wechat import WeChat


def _settings_with_domain(key: str) -> Any:
    """提供企业微信图片 URL 构造所需的最小运行配置。"""
    return {
        "APP_DOMAIN": "https://moviepilot.example",
        "API_V1_STR": "/api/v1",
    }[key]


def test_build_wechat_image_url_signs_only_douban_images(monkeypatch: pytest.MonkeyPatch) -> None:
    """只为豆瓣图片生成公开代理地址，其他图片保持原始 URL。"""
    monkeypatch.setattr(image_urls, "get_runtime_setting", _settings_with_domain)
    source_url = "https://img9.doubanio.com/view/photo/l/public/p2932615136.webp"

    proxy_url = image_urls.build_wechat_image_url(source_url)
    parsed = urlparse(proxy_url or "")
    signed_source = parse_qs(parsed.query)["url"][0]

    assert parsed.scheme == "https"
    assert parsed.netloc == "moviepilot.example"
    assert parsed.path == "/api/v1/system/notification-image"
    assert image_urls.verify_wechat_image_url(signed_source) == source_url
    assert image_urls.build_wechat_image_url("https://image.tmdb.org/poster.jpg") == (
        "https://image.tmdb.org/poster.jpg"
    )


def test_build_wechat_image_url_keeps_original_without_public_domain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未配置对外域名时不生成微信无法访问的本地地址。"""
    monkeypatch.setattr(
        image_urls,
        "get_runtime_setting",
        lambda key: "" if key == "APP_DOMAIN" else "/api/v1",
    )
    source_url = "https://img9.doubanio.com/view/photo/l/public/p2932615136.webp"

    assert image_urls.build_wechat_image_url(source_url) == source_url


@pytest.mark.asyncio
async def test_notification_image_rejects_wrong_signature_and_domain() -> None:
    """公开入口拒绝错误用途签名及非豆瓣来源，避免退化为通用代理。"""
    from app.application.security.url import SecurityUtils

    signed_tmdb_url = SecurityUtils.sign_url(
        "https://image.tmdb.org/poster.jpg",
        purpose=image_urls.WECHAT_IMAGE_SIGNATURE_PURPOSE,
    )

    with pytest.raises(HTTPException) as error:
        await system_endpoint.notification_image(url=signed_tmdb_url)

    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_notification_image_fetches_verified_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证通过后复用统一图片代理，并保留全局缓存和 ETag 参数。"""
    from app.application.security.url import SecurityUtils

    source_url = "https://img9.doubanio.com/view/photo/l/public/p2932615136.webp"
    signed_url = SecurityUtils.sign_url(
        source_url,
        purpose=image_urls.WECHAT_IMAGE_SIGNATURE_PURPOSE,
    )
    response = Response(content=b"image", media_type="image/webp")
    fetch_image = AsyncMock(return_value=response)
    monkeypatch.setattr(system_endpoint, "fetch_image", fetch_image)
    monkeypatch.setattr(
        system_endpoint,
        "get_runtime_settings",
        lambda: {"GLOBAL_IMAGE_CACHE": True},
    )

    result = await system_endpoint.notification_image(
        url=signed_url,
        if_none_match="etag",
    )

    assert result is response
    fetch_image.assert_awaited_once_with(
        url=source_url,
        use_cache=True,
        if_none_match="etag",
    )


def test_wechat_news_payload_uses_signed_douban_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """企业微信图文载荷应把豆瓣图片替换为可公开取图的地址。"""
    monkeypatch.setattr(image_urls, "get_runtime_setting", _settings_with_domain)
    captured: dict[str, Any] = {}
    client = WeChat.__new__(WeChat)
    client._appid = "1"
    monkeypatch.setattr(
        client,
        "_WeChat__post_request",
        lambda url, payload: captured.update(payload) or True,
    )
    source_url = "https://img9.doubanio.com/view/photo/l/public/p2932615136.webp"

    assert client._WeChat__send_image_message(
        title="奥德赛",
        text="已添加订阅",
        image_url=source_url,
    ) is True

    image_url = captured["news"]["articles"][0]["picurl"]
    signed_source = parse_qs(urlparse(image_url).query)["url"][0]
    assert image_urls.verify_wechat_image_url(signed_source) == source_url
