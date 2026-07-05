import asyncio
import json
from types import SimpleNamespace

import app.api.endpoints.download as download_endpoint
import app.api.endpoints.search as search_endpoint
from app import schemas
from app.core.context import SubtitleInfo
from app.utils.security import SecurityUtils


SUBTITLE_SITE_ID = 1001
SUBTITLE_PURPOSE = f"subtitle-download:{SUBTITLE_SITE_ID}"
SUBTITLE_URL = "https://example.test/downloadsubs.php?torrentid=1&subid=2"


class _NeverDisconnectedRequest:
    """
    为 SSE 测试提供始终在线的请求对象。
    """

    async def is_disconnected(self):
        return False


def _run(coro):
    """
    在同步测试中运行异步 endpoint。
    """
    return asyncio.run(coro)


async def _collect_sse_events(response):
    """
    读取 StreamingResponse 的 SSE 数据并解析为事件字典。
    """
    body = ""
    async for chunk in response.body_iterator:
        if isinstance(chunk, bytes):
            body += chunk.decode("utf-8")
        else:
            body += chunk

    events = []
    for block in body.strip().split("\n\n"):
        if not block:
            continue
        line = block.removeprefix("data: ")
        events.append(json.loads(line))
    return events


def _subtitle_payload(enclosure=SUBTITLE_URL, **overrides):
    """
    构造下载字幕接口入参。
    """
    payload = {
        "site": SUBTITLE_SITE_ID,
        "site_name": "ExampleSite",
        "site_cookie": "client-cookie=1",
        "site_ua": "ClientUA",
        "site_proxy": False,
        "title": "Demo.Movie.2026.zh-cn.srt",
        "enclosure": enclosure,
        "language": "简体中文",
        "size": 1024,
    }
    payload.update(overrides)
    return schemas.SubtitleInfo(**payload)


def test_subtitle_title_search_response_signs_enclosure(monkeypatch):
    """
    普通字幕搜索返回给客户端的下载链接必须带字幕下载专用签名。
    """

    class FakeSearchChain:
        async def async_search_subtitles_by_title(self, **_kwargs):
            return [
                SubtitleInfo(
                    site=SUBTITLE_SITE_ID,
                    site_name="ExampleSite",
                    title="Demo.Movie.2026.zh-cn.srt",
                    enclosure=SUBTITLE_URL,
                    language="简体中文",
                    size=1024,
                )
            ]

    monkeypatch.setattr(search_endpoint, "SearchChain", FakeSearchChain)

    response = _run(search_endpoint.search_subtitle_by_title(keyword="Demo", _=None))

    assert response.success
    signed_url = response.data[0]["enclosure"]
    assert signed_url != SUBTITLE_URL
    assert SecurityUtils.verify_signed_url(signed_url, purpose=SUBTITLE_PURPOSE) == SUBTITLE_URL
    assert SecurityUtils.verify_signed_url(signed_url) is None


def test_subtitle_media_search_response_signs_enclosure(monkeypatch):
    """
    精确媒体字幕搜索返回给客户端的下载链接必须带字幕下载专用签名。
    """

    async def fake_build_subtitle_search_source(**_kwargs):
        async def search_result():
            return [
                SubtitleInfo(
                    site=SUBTITLE_SITE_ID,
                    site_name="ExampleSite",
                    title="Demo.Movie.2026.zh-cn.srt",
                    enclosure=SUBTITLE_URL,
                    language="简体中文",
                    size=1024,
                )
            ]

        return search_result(), ""

    monkeypatch.setattr(
        search_endpoint,
        "_build_subtitle_search_source",
        fake_build_subtitle_search_source,
    )

    response = _run(search_endpoint.search_subtitle_by_id(mediaid="tmdb:1", _=None))

    assert response.success
    signed_url = response.data[0]["enclosure"]
    assert signed_url != SUBTITLE_URL
    assert SecurityUtils.verify_signed_url(signed_url, purpose=SUBTITLE_PURPOSE) == SUBTITLE_URL


def test_subtitle_search_sse_events_sign_enclosures(monkeypatch):
    """
    字幕 SSE 的 append/replace/done 事件都应只暴露签名后的下载链接。
    """

    class FakeSearchChain:
        def async_search_subtitles_by_title_stream(self, **_kwargs):
            async def source():
                for event_type in ("append", "replace", "done"):
                    yield {
                        "type": event_type,
                        "stage": "complete" if event_type == "done" else "searching",
                        "items": [
                            {
                                "site": SUBTITLE_SITE_ID,
                                "site_name": "ExampleSite",
                                "title": f"{event_type}.srt",
                                "enclosure": SUBTITLE_URL,
                                "language": "简体中文",
                                "size": 1024,
                            }
                        ],
                    }

            return source()

    monkeypatch.setattr(search_endpoint, "SearchChain", FakeSearchChain)

    response = _run(
        search_endpoint.search_subtitle_by_title_stream(
            request=_NeverDisconnectedRequest(),
            keyword="Demo",
            _=None,
        )
    )
    events = _run(_collect_sse_events(response))

    assert [event["type"] for event in events] == ["append", "replace", "done"]
    for event in events:
        signed_url = event["items"][0]["enclosure"]
        assert signed_url != SUBTITLE_URL
        assert SecurityUtils.verify_signed_url(signed_url, purpose=SUBTITLE_PURPOSE) == SUBTITLE_URL


def test_download_subtitle_rejects_unsigned_enclosure(monkeypatch):
    """
    下载接口必须拒绝历史未签名字幕链接。
    """

    class FakeDownloadChain:
        def download_subtitle(self, **_kwargs):
            return True, "不应下载", ["unsafe.srt"]

    monkeypatch.setattr(download_endpoint, "DownloadChain", FakeDownloadChain)

    response = download_endpoint.download_subtitle(
        subtitle_in=_subtitle_payload(),
        current_user=SimpleNamespace(name="tester"),
    )

    assert not response.success
    assert "签名" in response.message


def test_download_subtitle_rejects_other_purpose_signature(monkeypatch):
    """
    其它用途的签名不能复用于字幕下载。
    """

    class FakeDownloadChain:
        def download_subtitle(self, **_kwargs):
            return True, "不应下载", ["unsafe.srt"]

    signed_url = SecurityUtils.sign_url(SUBTITLE_URL, purpose="image-proxy")
    monkeypatch.setattr(download_endpoint, "DownloadChain", FakeDownloadChain)

    response = download_endpoint.download_subtitle(
        subtitle_in=_subtitle_payload(enclosure=signed_url),
        current_user=SimpleNamespace(name="tester"),
    )

    assert not response.success
    assert "签名" in response.message


def test_download_subtitle_cleans_url_and_uses_server_site_request_fields(monkeypatch):
    """
    下载链只应收到去签名后的真实 URL，以及服务端站点配置中的请求凭据。
    """
    captured = {}
    signed_url = SecurityUtils.sign_url(SUBTITLE_URL, purpose=SUBTITLE_PURPOSE)

    class FakeSiteOper:
        def get(self, site_id):
            assert site_id == SUBTITLE_SITE_ID
            return SimpleNamespace(cookie="server-cookie=1", ua="ServerUA", proxy=True)

    class FakeDownloadChain:
        def download_subtitle(self, **kwargs):
            captured.update(kwargs)
            return True, "字幕下载成功", ["/downloads/Demo.Movie.2026.zh-cn.srt"]

    monkeypatch.setattr(download_endpoint, "SiteOper", FakeSiteOper, raising=False)
    monkeypatch.setattr(download_endpoint, "DownloadChain", FakeDownloadChain)

    response = download_endpoint.download_subtitle(
        subtitle_in=_subtitle_payload(enclosure=signed_url),
        current_user=SimpleNamespace(name="tester"),
    )

    subtitle = captured["subtitle"]
    assert response.success
    assert subtitle.enclosure == SUBTITLE_URL
    assert subtitle.site_cookie == "server-cookie=1"
    assert subtitle.site_ua == "ServerUA"
    assert subtitle.site_proxy is True
