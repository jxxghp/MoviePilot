"""
通过真实 SDK 和 HTTP 表单验证新旧 qBittorrent 的跳过校验参数。
"""

from collections.abc import Iterator
from email import policy
from email.parser import BytesParser
from typing import Union
from urllib.parse import parse_qs, urlparse

import pytest
from qbittorrentapi import Client
from requests import PreparedRequest, Response
from requests.adapters import HTTPAdapter

from app.modules.qbittorrent.qbittorrent import Qbittorrent


def _form_fields(request: PreparedRequest) -> dict[str, str]:
    """
    读取实际准备发送的 URL 编码或 multipart 表单字段。
    """
    body = request.body
    assert isinstance(body, (str, bytes))
    content_type = str(request.headers["Content-Type"])
    if content_type.startswith("multipart/form-data"):
        assert isinstance(body, bytes)
        message = BytesParser(policy=policy.default).parsebytes(
            f"Content-Type: {content_type}\r\n\r\n".encode() + body
        )
        fields = {}
        for part in message.iter_parts():
            if part.get_filename() is not None:
                continue
            value = part.get_payload(decode=True)
            assert isinstance(value, bytes)
            fields[str(part.get_param("name", header="Content-Disposition"))] = value.decode()
        return fields
    text = body.decode() if isinstance(body, bytes) else body
    return {key: values[0] for key, values in parse_qs(text).items()}


@pytest.fixture(
    params=[("v4.6.7", "2.8.19"), ("v5.2.1", "2.14.1"), ("v5.3.0", "2.16.2")],
    ids=["qb46", "qb52", "qb53"],
)
def qb_client(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest,
) -> Iterator[tuple[Qbittorrent, list[PreparedRequest]]]:
    """
    保留真实 SDK 和请求序列化，只在 HTTP 发送边界回放下载器响应。
    """
    app_version, web_api_version = request.param
    added: list[PreparedRequest] = []

    def send(
        _adapter: HTTPAdapter, prepared: PreparedRequest, **_kwargs: object,
    ) -> Response:
        """
        回放版本和添加响应，拦截全部网络发送并记录添加请求。
        """
        path = urlparse(prepared.url).path
        response = Response()
        response.status_code = 200
        response.request = prepared
        response.encoding = "utf-8"
        if path == "/api/v2/app/version":
            response._content = app_version.encode()
        elif path == "/api/v2/app/webapiVersion":
            response._content = web_api_version.encode()
        else:
            assert path == "/api/v2/torrents/add"
            assert prepared.method == "POST"
            added.append(prepared)
            response._content = b"Ok."
        return response

    monkeypatch.setattr(HTTPAdapter, "send", send)
    sdk = Client(host="http://qb.example.test", FORCE_SCHEME_FROM_HOST=True)
    monkeypatch.setattr(Qbittorrent, "_Qbittorrent__login_qbittorrent", lambda _self: sdk)
    client = Qbittorrent(host="http://qb.example.test", category=True)
    try:
        yield client, added
    finally:
        sdk._trigger_session_initialization()


@pytest.mark.parametrize("content", ["magnet:?xt=urn:btih:" + "a" * 40, b"torrent-content"])
@pytest.mark.parametrize("skip_checking", [False, True])
def test_skip_checking_reaches_both_api_fields(
    qb_client: tuple[Qbittorrent, list[PreparedRequest]],
    content: Union[str, bytes], skip_checking: bool,
) -> None:
    """
    转种时暂停添加、路径和标签保持不变，新旧校验字段均收到显式开关值。
    """
    client, added = qb_client
    assert client.add_torrent(
        content=content, is_paused=True, download_dir="/downloads/seed",
        tag="转移做种", category="电影", is_skip_checking=skip_checking,
    ) == (True, [])

    assert len(added) == 1
    fields = _form_fields(added[0])
    expected = str(skip_checking).lower()
    assert fields["skip_checking"].lower() == expected
    assert fields["seedMode"].lower() == expected
    assert fields["paused"].lower() == "true"
    assert fields["stopped"].lower() == "true"
    assert fields["savepath"] == "/downloads/seed"
    assert fields["tags"] == "转移做种"
    assert fields["category"] == "电影"
    assert fields["autoTMM"].lower() == "true"


@pytest.mark.parametrize("kwargs", [{}, {"is_skip_checking": None}], ids=["omitted", "none"])
def test_regular_add_does_not_enable_seed_mode(
    qb_client: tuple[Qbittorrent, list[PreparedRequest]], kwargs: dict[str, None],
) -> None:
    """
    普通添加省略或传入空校验选项时，不启用任何跳过校验字段。
    """
    client, added = qb_client
    assert client.add_torrent(content=b"torrent-content", **kwargs) == (True, [])
    fields = _form_fields(added[0])
    assert "skip_checking" not in fields
    assert "seedMode" not in fields


def test_explicit_seed_mode_is_preserved(
    qb_client: tuple[Qbittorrent, list[PreparedRequest]],
) -> None:
    """
    原样透传调用方显式指定的新字段，保留既有扩展参数优先级。
    """
    client, added = qb_client
    assert client.add_torrent(
        content=b"torrent-content", is_skip_checking=False, seedMode=True,
    ) == (True, [])
    fields = _form_fields(added[0])
    assert fields["seedMode"].lower() == "true"
