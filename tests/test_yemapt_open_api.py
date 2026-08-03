import asyncio
import base64
import json

from app.chain.download import DownloadChain
from app.core.context import TorrentInfo
from app.modules.indexer.parser.yema import YemaSiteUserInfo
from app.modules.indexer.spider.yema import YemaSpider
from app.schemas import MediaType


class _FakeResponse:
    """构造 YemaPT 开放 API 测试使用的最小响应对象。"""

    def __init__(self, payload: dict, status_code: int = 200):
        """保存响应数据和状态码。"""
        self._payload = payload
        self.status_code = status_code
        self.reason = "OK"
        self.text = json.dumps(payload)

    def __bool__(self) -> bool:
        """按 HTTP 成功状态模拟 requests.Response 的布尔值。"""
        return self.status_code < 400

    def json(self) -> dict:
        """返回预设 JSON 数据。"""
        return self._payload


def _build_indexer() -> dict:
    """构造 YemaPT 开放 API Spider 所需的最小站点配置。"""
    return {
        "id": "yemapt",
        "name": "YemaPT",
        "domain": "https://www.yemapt.org/",
        "apikey": "yema-auth-key",
        "ua": "MoviePilot-Test",
        "proxy": False,
    }


def _torrent_response() -> _FakeResponse:
    """构造 YemaPT 公开种子列表响应。"""
    return _FakeResponse({
        "success": True,
        "showType": 0,
        "data": [{
            "id": 100,
            "showName": "Movie.2026.2160p.WEB-DL.H.265-GROUP",
            "shortDesc": "电影中文副标题",
            "categoryId": 4,
            "fileSize": 21474836480,
            "seedNum": 15,
            "leechNum": 2,
            "completedNum": 30,
            "listingTime": "2026-08-02T14:00:00+08:00",
            "uploadPromotion": "double_upload",
            "downloadPromotion": "free",
            "downloadPromotionEndTime": "2026-08-04T14:00:00+08:00",
            "tagList": ["6", "9"],
            "hrPunishEnable": True,
        }],
    })


def test_yemapt_search_uses_open_api_auth_and_maps_fields(monkeypatch):
    """YemaPT 搜索应使用 AuthKey、开放接口及标准字段映射。"""
    captured = {}

    def fake_post_res(request, url: str, json: dict = None, **_kwargs):
        """记录开放 API 搜索请求并回放种子数据。"""
        captured.update({
            "url": url,
            "json": json,
            "headers": request._headers,
            "cookies": request._cookies,
        })
        return _torrent_response()

    monkeypatch.setattr(
        "app.modules.indexer.spider.yema.RequestUtils.post_res",
        fake_post_res,
    )

    error, torrents = YemaSpider(_build_indexer()).search(
        keyword="Movie",
        mtype=MediaType.MOVIE,
        page=2,
    )

    assert not error
    assert captured == {
        "url": "https://www.yemapt.org/openApi/torrent/fetchOpenTorrentList.json",
        "json": {
            "keyword": "Movie",
            "pageParam": {"current": 3, "pageSize": 40},
            "sorter": {},
        },
        "headers": {
            "Authorization": "yema-auth-key",
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "MoviePilot-Test",
        },
        "cookies": None,
    }
    assert torrents[0] == {
        "title": "Movie.2026.2160p.WEB-DL.H.265-GROUP",
        "description": "电影中文副标题",
        "enclosure": torrents[0]["enclosure"],
        "pubdate": "2026-08-02 14:00:00",
        "size": 21474836480,
        "seeders": 15,
        "peers": 2,
        "grabs": 30,
        "downloadvolumefactor": 0,
        "uploadvolumefactor": 2,
        "freedate": "2026-08-04 14:00:00",
        "page_url": "https://www.yemapt.org/#/torrent/detail/100/",
        "labels": ["中字", "HDR10"],
        "hit_and_run": True,
        "category": MediaType.MOVIE.value,
    }

    encoded_config, credential_url = torrents[0]["enclosure"].split("]", 1)
    request_config = json.loads(base64.b64decode(encoded_config[1:]).decode("utf-8"))
    assert credential_url == (
        "https://www.yemapt.org/openApi/torrent/generateDownloadKey.json"
    )
    assert request_config["header"]["Authorization"] == "yema-auth-key"
    assert request_config["params"] == {"id": 100}
    assert request_config["success"] == "success"
    assert request_config["result_path"] == "api/torrent/download1"
    assert request_config["result_query_param"] == "token"


def test_yemapt_search_rejects_business_failure(monkeypatch):
    """HTTP 成功但业务失败时，YemaPT 搜索必须返回错误。"""
    monkeypatch.setattr(
        "app.modules.indexer.spider.yema.RequestUtils.post_res",
        lambda *_args, **_kwargs: _FakeResponse({
            "success": False,
            "errorCode": 403,
            "errorMessage": "need api auth",
        }),
    )

    error, torrents = YemaSpider(_build_indexer()).search(keyword="Movie")

    assert error
    assert torrents == []


def test_yemapt_async_search_uses_open_api(monkeypatch):
    """YemaPT 异步搜索应使用与同步搜索相同的开放 API 契约。"""
    captured = {}

    async def fake_post_res(request, url: str, json: dict = None, **_kwargs):
        """记录异步开放 API 请求并回放种子数据。"""
        captured.update({
            "url": url,
            "json": json,
            "headers": request._headers,
            "cookies": request._cookies,
        })
        return _torrent_response()

    monkeypatch.setattr(
        "app.modules.indexer.spider.yema.AsyncRequestUtils.post_res",
        fake_post_res,
    )

    error, torrents = asyncio.run(
        YemaSpider(_build_indexer()).async_search(keyword="Movie", page=0)
    )

    assert not error
    assert len(torrents) == 1
    assert captured["url"].endswith("/openApi/torrent/fetchOpenTorrentList.json")
    assert captured["headers"]["Authorization"] == "yema-auth-key"
    assert captured["cookies"] is None


def test_yemapt_user_parser_uses_basic_info_only(monkeypatch):
    """YemaPT 用户解析器应仅调用开放 API 已提供的基本信息接口。"""
    captured = []
    payload = {
        "success": True,
        "showType": 0,
        "data": {
            "id": 10,
            "name": "yema-user",
            "bonus": 1000000,
            "level": 7,
            "registerTime": "2024-05-01T00:00:00+08:00",
            "promotionUploadSize": 2000000,
            "promotionDownloadSize": 1000000,
        },
    }

    def fake_post_res(request, url: str, json: dict = None, **_kwargs):
        """记录用户基本信息请求并回放开放 API 响应。"""
        captured.append((url, json, request._headers, request._cookies))
        return _FakeResponse(payload)

    monkeypatch.setattr(
        "app.modules.indexer.parser.RequestUtils.post_res",
        fake_post_res,
    )
    parser = YemaSiteUserInfo(
        site_name="YemaPT",
        url="https://www.yemapt.org/",
        site_cookie="legacy-cookie",
        apikey="yema-auth-key",
        token=None,
        ua="MoviePilot-Test",
        proxy=False,
    )

    parser.parse()

    assert len(captured) == 1
    assert captured[0][0] == "https://www.yemapt.org/openApi/user/fetchBasicInfo.json"
    assert captured[0][1] == {}
    assert captured[0][2]["Authorization"] == "yema-auth-key"
    assert captured[0][3] is None
    assert parser.userid == 10
    assert parser.username == "yema-user"
    assert parser.user_level == "7"
    assert parser.upload == 2000000
    assert parser.download == 1000000
    assert parser.ratio == 2.0


def test_yemapt_download_generates_and_urlencodes_temporary_key(monkeypatch):
    """YemaPT 下载应临时生成凭证并在下载 URL 中安全编码。"""
    captured = {}

    def fake_post_res(request, url: str, params: dict = None, **_kwargs):
        """回放下载凭证接口响应。"""
        captured.update({
            "credential_url": url,
            "credential_params": params,
            "credential_headers": request._headers,
            "credential_cookies": request._cookies,
        })
        return _FakeResponse({"success": True, "data": "abc+/="})

    def fake_download_torrent(_helper, **kwargs):
        """记录最终种子文件下载地址并返回有效种子内容。"""
        captured.update(kwargs)
        return None, b"torrent-content", "Movie", ["Movie.mkv"], ""

    monkeypatch.setattr("app.chain.download.RequestUtils.post_res", fake_post_res)
    monkeypatch.setattr(
        "app.chain.download.TorrentHelper.download_torrent",
        fake_download_torrent,
    )
    enclosure = YemaSpider(_build_indexer())._build_download_url(100)
    torrent = TorrentInfo(
        title="Movie.2026",
        enclosure=enclosure,
        site_cookie="legacy-cookie",
        site_ua="MoviePilot-Test",
        site_proxy=False,
    )

    content, folder, files = object.__new__(DownloadChain).download_torrent(torrent)

    assert content == b"torrent-content"
    assert folder == "Movie"
    assert files == ["Movie.mkv"]
    assert captured["credential_url"] == (
        "https://www.yemapt.org/openApi/torrent/generateDownloadKey.json"
    )
    assert captured["credential_params"] == {"id": 100}
    assert captured["credential_headers"]["Authorization"] == "yema-auth-key"
    assert captured["credential_cookies"] is None
    assert captured["url"] == (
        "https://www.yemapt.org/api/torrent/download1?token=abc%2B%2F%3D"
    )
    assert captured["cookie"] is None
    assert captured["cache_invalid"] is False
