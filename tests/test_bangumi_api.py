from unittest.mock import MagicMock

from app.modules.bangumi.bangumi import BangumiApi


def test_bangumi_api_uses_data_proxy_without_rewriting_payload() -> None:
    """Bangumi 数据代理只改变请求地址，不依赖或修改响应报文结构。"""
    original_image = "https://lain.bgm.tv/pic/cover.jpg"
    response = MagicMock(status_code=200)
    response.json.return_value = {
        "images": {"large": original_image},
        "unknown_image_field": original_image,
    }
    api = BangumiApi(base_url="api-proxy.example/bangumi")
    api._req.get_res = MagicMock(return_value=response)

    try:
        result = api.detail(123)
    finally:
        api.clear_cache()
        api.close()

    assert api._req.get_res.call_args.kwargs["url"] == (
        "https://api-proxy.example/bangumi/v0/subjects/123"
    )
    assert result == response.json.return_value


def test_bangumi_api_empty_data_proxy_uses_official_endpoint() -> None:
    """空数据代理地址应回退官方 Bangumi API。"""
    api = BangumiApi(base_url="")

    assert api.base_url == "https://api.bgm.tv/"
    api.close()


def test_bangumi_calendar_upgrades_only_lain_http_image_urls_to_https() -> None:
    """每日放送封面使用 HTTPS，其他图片与非图片地址保持原样。"""
    original_item = {
        "id": 646154,
        "url": "http://lain.bgm.tv/subject/646154",
        "image": "http://lain.bgm.tv/pic/cover/legacy.jpg",
        "images": {
            "large": "http://lain.bgm.tv/pic/cover/l/89/fc/646154_uQi81.jpg",
            "small": "https://lain.bgm.tv/pic/cover/s/646154.jpg",
            "common": "http://cdn.example/cover.jpg",
            "untrusted": "http://lain.bgm.tv.evil.example/cover.jpg",
        },
    }
    response = [{"weekday": {"id": 1}, "items": [original_item]}]

    result = BangumiApi._calendar_items(response)

    assert result == [
        {
            "id": 646154,
            "url": "http://lain.bgm.tv/subject/646154",
            "image": "https://lain.bgm.tv/pic/cover/legacy.jpg",
            "images": {
                "large": "https://lain.bgm.tv/pic/cover/l/89/fc/646154_uQi81.jpg",
                "small": "https://lain.bgm.tv/pic/cover/s/646154.jpg",
                "common": "http://cdn.example/cover.jpg",
                "untrusted": "http://lain.bgm.tv.evil.example/cover.jpg",
            },
        }
    ]
    assert response[0]["items"][0] == original_item
