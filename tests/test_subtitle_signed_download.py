"""
验证字幕下载签名、权限和过期校验。
"""

import asyncio
import json
from types import SimpleNamespace

import app.api.endpoints.download as download_endpoint
import app.api.endpoints.search as search_endpoint
from app import schemas
from app.application.security.url import SecurityUtils
from app.domain.context import SubtitleInfo

SUBTITLE_SITE_ID = 1001
SUBTITLE_PURPOSE = f"subtitle-download:{SUBTITLE_SITE_ID}"
SUBTITLE_URL = "https://example.test/downloadsubs.php?torrentid=1&subid=2"


class _NeverDisconnectedRequest:
    """
    为 SSE 测试提供始终在线的请求对象。
    """

    async def is_disconnected(self):
        """
        模拟客户端仍保持连接，避免字幕流因断开检测提前退出。
        """
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
        """
        提供固定字幕候选，验证响应链接签名而不查询真实来源。
        """
        async def async_search_subtitles_by_title(self, **_kwargs):
            """
            返回固定站点的字幕候选，供服务端签名投影测试使用。
            """
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

    response = _run(search_endpoint.search_subtitle_by_id(
        media_source="themoviedb",
        media_id="1",
        _=None,
    ))

    assert response.success
    signed_url = response.data[0]["enclosure"]
    assert signed_url != SUBTITLE_URL
    assert SecurityUtils.verify_signed_url(signed_url, purpose=SUBTITLE_PURPOSE) == SUBTITLE_URL


def test_subtitle_search_sse_events_sign_enclosures(monkeypatch):
    """
    字幕 SSE 的 append/replace/done 事件都应只暴露签名后的下载链接。
    """

    class FakeSearchChain:
        """
        提供固定字幕候选，验证响应链接签名而不查询真实来源。
        """
        def async_search_subtitles_by_title_stream(self, **_kwargs):
            """
            依次返回追加、替换和完成事件，验证字幕流各阶段都进行签名投影。
            """
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
        """
        替代字幕下载链，隔离文件下载并记录接口调用。
        """
        def download_subtitle(self, **_kwargs):
            """
            提供可观察的下载成功替身；无效请求应在调用此前被拦截。
            """
            return True, "不应下载", ["unsafe.srt"]

    monkeypatch.setattr(download_endpoint, "DownloadChain", FakeDownloadChain)

    response = download_endpoint.download_subtitle(
        subtitle_in=_subtitle_payload(),
        media_source="themoviedb",
        media_id="1",
        current_user=SimpleNamespace(name="tester"),
    )

    assert not response.success
    assert "签名" in response.message


def test_download_subtitle_rejects_other_purpose_signature(monkeypatch):
    """
    其它用途的签名不能复用于字幕下载。
    """

    class FakeDownloadChain:
        """
        替代字幕下载链，隔离文件下载并记录接口调用。
        """
        def download_subtitle(self, **_kwargs):
            """
            提供可观察的下载成功替身；未通过签名校验时不应到达该入口。
            """
            return True, "不应下载", ["unsafe.srt"]

    signed_url = SecurityUtils.sign_url(SUBTITLE_URL, purpose="image-proxy")
    monkeypatch.setattr(download_endpoint, "DownloadChain", FakeDownloadChain)

    response = download_endpoint.download_subtitle(
        subtitle_in=_subtitle_payload(enclosure=signed_url),
        media_source="themoviedb",
        media_id="1",
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

    class FakeSiteQuery:
        """
        提供站点查询结果，验证签名下载的身份和权限边界。
        """
        def get(self, site_id):
            """
            按预期站点标识返回服务端凭据，隔离真实站点配置查询。
            """
            assert site_id == SUBTITLE_SITE_ID
            return SimpleNamespace(url="https://example.test/", cookie="server-cookie=1", ua="ServerUA", proxy=True)

        def get_sync(self, site_id):
            """
            复用站点查询替身，供同步凭据读取路径使用。
            """
            return self.get(site_id)

    class FakeDownloadChain:
        """
        替代字幕下载链，隔离文件下载并记录接口调用。
        """
        def download_subtitle(self, **kwargs):
            """
            记录字幕下载参数，并返回固定的本地字幕文件路径。
            """
            captured.update(kwargs)
            return True, "字幕下载成功", ["/downloads/Demo.Movie.2026.zh-cn.srt"]

    monkeypatch.setattr(
        download_endpoint,
        "get_configured_site_query_service",
        lambda: FakeSiteQuery(),
    )
    monkeypatch.setattr(download_endpoint, "DownloadChain", FakeDownloadChain)

    response = download_endpoint.download_subtitle(
        subtitle_in=_subtitle_payload(enclosure=signed_url),
        media_source="themoviedb",
        media_id="1",
        current_user=SimpleNamespace(name="tester"),
    )

    subtitle = captured["subtitle"]
    assert response.success
    assert subtitle.enclosure == SUBTITLE_URL
    assert subtitle.site_cookie == "server-cookie=1"
    assert subtitle.site_ua == "ServerUA"
    assert subtitle.site_proxy is True
