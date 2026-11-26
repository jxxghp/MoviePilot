"""
验证 Bangumi 数据代理、接口响应和图片地址转换。
"""

from unittest.mock import MagicMock

from app.modules.bangumi.bangumi import BangumiApi


def test_bangumi_api_uses_data_proxy_without_rewriting_payload() -> None:
    """
    Bangumi 数据代理只改变请求地址，不依赖或修改响应报文结构。
    """
    original_image = "https://lain.bgm.tv/pic/cover.jpg"
    response = MagicMock(status_code=200)
    response.json.return_value = {
        "images": {"large": original_image},
        "unknown_image_field": original_image,
    }
    api = BangumiApi(base_url="api-proxy.example/bangumi", image_domain="")
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
    """
    空数据代理地址应回退官方 Bangumi API。
    """
    api = BangumiApi(base_url="")

    assert api.base_url == "https://api.bgm.tv/"
    api.close()


def test_bangumi_calendar_upgrades_only_lain_http_image_urls_to_https() -> None:
    """
    每日放送封面使用 HTTPS，其他图片与非图片地址保持原样。
    """
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


def test_bangumi_api_rewrites_image_hosts_when_image_domain_configured() -> None:
    """
    配置图片代理后，响应里的官方图片域名被替换，页面链接与其它主机保持原样。
    """
    response = MagicMock(status_code=200)
    response.json.return_value = {
        "id": 502123,
        "url": "http://bgm.tv/subject/502123",
        "images": {
            "large": "https://lain.bgm.tv/pic/cover/l/e1/6f/502123_IiVB6.jpg",
            "small": "http://lain.bgm.tv/r/200/pic/cover/l/e1/6f/502123_IiVB6.jpg",
            "spoofer": "http://lain.bgm.tv.evil.example/cover.jpg",
            "external": "https://cdn.example/cover.jpg",
            "missing": None,
        },
        "actors": [{"image": "https://lain.bgm.tv/pic/crt/l/4c/7c/2000_crt_9baR9.jpg"}],
    }
    api = BangumiApi(base_url="", image_domain="https://bgmp.example:8443/mirror/")
    api._req.get_res = MagicMock(return_value=response)

    try:
        result = api.detail(502123)
    finally:
        api.clear_cache()
        api.close()

    assert result["images"] == {
        "large": "https://bgmp.example:8443/mirror/pic/cover/l/e1/6f/502123_IiVB6.jpg",
        "small": "https://bgmp.example:8443/mirror/r/200/pic/cover/l/e1/6f/502123_IiVB6.jpg",
        "spoofer": "http://lain.bgm.tv.evil.example/cover.jpg",
        "external": "https://cdn.example/cover.jpg",
        "missing": None,
    }
    assert result["actors"][0]["image"] == (
        "https://bgmp.example:8443/mirror/pic/crt/l/4c/7c/2000_crt_9baR9.jpg"
    )
    assert result["url"] == "http://bgm.tv/subject/502123"
    # 解码边界只做复制改写，原始响应报文必须保持不变
    assert response.json.return_value["images"]["large"] == (
        "https://lain.bgm.tv/pic/cover/l/e1/6f/502123_IiVB6.jpg"
    )
    assert api._normalize_images(502123) == 502123


def test_bangumi_api_without_image_domain_keeps_payload_identity() -> None:
    """
    未配置图片代理时不改写任何响应节点，图片地址保持官方域名。
    """
    api = BangumiApi(base_url="", image_domain="")
    payload = {"images": {"large": "http://lain.bgm.tv/pic/cover/l/1.jpg"}}

    try:
        assert api.image_proxy is None
        assert api._normalize_images(payload) is payload
    finally:
        api.close()


def test_bangumi_image_proxy_distinguishes_three_modes() -> None:
    """
    图片代理地址按尾部斜杠与查询串区分替换主机、查询拼接和路径拼接。
    """
    normalize = BangumiApi._normalize_image_proxy

    assert (normalize("bgmp.example/").mode, normalize("bgmp.example/").prefix) == (
        "host",
        "https://bgmp.example",
    )
    assert (normalize("https://bgmp.example").mode, normalize("https://bgmp.example").prefix) == (
        "path",
        "https://bgmp.example",
    )
    assert (
        normalize("https://worker.example/?url=").mode,
        normalize("https://worker.example/?url=").prefix,
    ) == ("query", "https://worker.example/?url=")
    assert (normalize("HTTP://CDN.Example:8080/mirror/").mode, normalize("HTTP://CDN.Example:8080/mirror/").prefix) == (
        "host",
        "HTTP://CDN.Example:8080/mirror",
    )
    assert normalize("https://user:pw@bgmp.example") is None
    assert normalize("ftp://bgmp.example") is None
    assert normalize("//bgmp.example") is None
    assert normalize("http://[bad") is None
    assert normalize("https://bgmp.example/#frag") is None
    assert normalize("") is None
    assert normalize(None) is None


def test_bangumi_image_url_replacement_is_idempotent() -> None:
    """
    只替换官方图片主机，已指向代理的地址与其它主机保持原样。
    """
    proxy = BangumiApi._normalize_image_proxy("bgmp.example/")

    assert BangumiApi._normalize_image_url(
        "http://lain.bgm.tv/pic/cover/c/1.jpg", proxy
    ) == "https://bgmp.example/pic/cover/c/1.jpg"
    assert BangumiApi._normalize_image_url(
        "https://bgmp.example/pic/cover/c/1.jpg", proxy
    ) == "https://bgmp.example/pic/cover/c/1.jpg"
    assert BangumiApi._normalize_image_url(
        "https://bgm.tv/subject/1", proxy
    ) == "https://bgm.tv/subject/1"
    assert BangumiApi._normalize_image_url("http://[bad", proxy) == "http://[bad"
    assert BangumiApi._normalize_image_url(None, proxy) is None


def test_bangumi_image_url_query_mode_appends_encoded_original() -> None:
    """
    含查询串的代理地址把原图地址整体编码到查询参数上。
    """
    api = BangumiApi(base_url="", image_domain="https://worker.example/?url=")

    try:
        assert api._normalize_image_url(
            "https://lain.bgm.tv/pic/cover/l/1.jpg", api.image_proxy
        ) == "https://worker.example/?url=https%3A%2F%2Flain.bgm.tv%2Fpic%2Fcover%2Fl%2F1.jpg"
    finally:
        api.close()


def test_bangumi_image_url_path_mode_appends_original() -> None:
    """
    既无尾斜杠也无查询串的代理地址按路径拼接原图地址。
    """
    api = BangumiApi(base_url="", image_domain="https://worker.example")

    try:
        assert api._normalize_image_url(
            "https://lain.bgm.tv/pic/cover/l/1.jpg", api.image_proxy
        ) == "https://worker.example/https://lain.bgm.tv/pic/cover/l/1.jpg"
    finally:
        api.close()


def test_bangumi_cache_token_includes_image_proxy() -> None:
    """
    切换图片代理必须产生新的缓存身份，避免复用旧来源的图片地址。
    """
    official = BangumiApi(base_url="", image_domain="")
    mirrored = BangumiApi(base_url="", image_domain="bgmp.example")

    try:
        assert official._cache_token != mirrored._cache_token
        assert "bgmp.example" in mirrored._cache_token
    finally:
        official.close()
        mirrored.close()
