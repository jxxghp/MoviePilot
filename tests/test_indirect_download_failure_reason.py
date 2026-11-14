"""
换票与临时下载地址失败的原因传播、凭据脱敏和冷却隔离回归。
"""

import base64
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import Mock

import pytest
from requests import Response

import app.application.site.throttle as throttle
import app.application.torrent.download as torrent_download
import app.chain.download.submission as submission
from app.adapters.cache.backends import FileBackend
from app.chain.download import DownloadChain
from app.chain.download.batch import _BatchDownloadRun
from app.chain.download.failure import DownloadFailureOwner
from app.domain.context import Context, MediaInfo, TorrentInfo
from app.domain.metainfo import MetaInfo
from app.modules.indexer import IndexerModule
from app.runtime.rate import RequestRateLimiter
from app.schemas.types import MediaType

# 合成的换票接口、短效下载地址和单文件种子，不使用真实站点或账号。
TICKET_URL = "https://api.tracker.example/token"
DOWNLOAD_URL = "https://api.tracker.example/download?tid=39&t=1&sign=synthetic-ticket"
CONTENT = b"d4:infod4:name10:Series.mkv6:lengthi1eee"


def _response(payload: object, status: int = 200) -> Response:
    """
    构造真实 HTTP 响应，保留错误状态为假的布尔语义。

    :param payload: JSON 数据或原始响应字节
    :param status: 回放的 HTTP 状态码
    :return: 带可核验关闭回执的离线响应
    """
    response = Response()
    response.status_code = status
    response._content = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
    response.encoding = "utf-8"
    response.headers["Content-Type"] = "text/html" if isinstance(payload, bytes) else "application/json"
    response.close = Mock()
    return response


def _ticket(**options: Any) -> str:
    """
    生成未声明专站协议的旧换票配方，覆盖 RSS 缓存的兼容路径。

    :param options: 本次覆盖的请求方法、结果字段及 Cookie 开关
    :return: 编码的原始资源下载地址
    """
    recipe = {"method": "post", "params": {"id": "39"}, "result": "data", "cookie": False, **options}
    encoded = base64.b64encode(json.dumps(recipe).encode()).decode()
    return f"[{encoded}]{TICKET_URL}"


@pytest.fixture
def boundary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    """
    保留真实配方解析、种子解析及冷却写入，仅隔离 HTTP、缓存和外部媒体补全。

    :param monkeypatch: 用例结束时恢复模块和实例替身的夹具
    :param tmp_path: 本用例私有种子缓存目录
    :return: 下载链、原始候选、HTTP 回放端口和黑名单观察点
    """
    cache = FileBackend(tmp_path)
    monkeypatch.setattr(submission, "FileCache", lambda **_kwargs: cache)
    monkeypatch.setattr(torrent_download, "FileCache", lambda **_kwargs: cache)
    monkeypatch.setattr(torrent_download, "TTLCache", Mock(return_value={}))
    monkeypatch.setattr(torrent_download.time, "sleep", Mock())
    monkeypatch.setattr(throttle, "_site_requests", RequestRateLimiter())
    query = Mock(get_sync=Mock(return_value=None), get_by_domain_sync=Mock(return_value=None))
    monkeypatch.setattr(throttle, "get_configured_site_query_service", lambda: query)
    helper = torrent_download.TorrentHelper()
    helper.add_invalid = Mock(wraps=helper.add_invalid)
    monkeypatch.setattr(submission, "TorrentHelper", lambda: helper)
    ticket_response = _response({"code": "0", "data": DOWNLOAD_URL})
    ticket = Mock(post=Mock(return_value=ticket_response), get=Mock(return_value=ticket_response))
    monkeypatch.setattr(submission, "_download_ports_snapshot", lambda: (ticket, Mock()))
    port = Mock(request=Mock(return_value=_response(CONTENT)))
    monkeypatch.setattr(torrent_download, "_require_torrent_port", lambda: port)
    chain = object.__new__(DownloadChain)
    chain.post_message = Mock()
    chain.download_failure_repository = Mock()
    chain._apply_resource_download_event = Mock(return_value=(None, None))
    module = object.__new__(IndexerModule)

    def dispatch(method: str, **kwargs: Any) -> Any:
        """
        保留真实索引模块解释旧配方，不通过模块管理器访问外部站点。
        """
        kwargs.pop("raise_exception", None)
        return getattr(module, method)(**kwargs)

    chain.run_module = Mock(side_effect=dispatch)
    torrent = TorrentInfo(site=7372, site_name="测试站", title="Series S01E39", enclosure=_ticket(),
                          page_url="https://tracker.example/detail/39", site_cookie="session=synthetic-cookie")
    context = Context(torrent_info=torrent, media_info=MediaInfo(title="Series", type=MediaType.TV),
                      meta_info=MetaInfo("Series S01E39"))
    media = Mock(supplement_tmdb_info=Mock(return_value=context.media_info))
    monkeypatch.setattr(submission, "MediaChain", lambda: media)
    log = Mock()
    monkeypatch.setattr(submission, "logger", log)
    return SimpleNamespace(chain=chain, torrent=torrent, context=context, ticket=ticket,
                           ticket_response=ticket_response, port=port, helper=helper, cache=cache, log=log)


@pytest.mark.parametrize("method", ["get", "post"])
@pytest.mark.parametrize("status,payload,options,detail", [
    (None, None, {}, "未收到响应"),
    (500, {"code": "1", "message": "upstream unavailable"}, {}, "upstream unavailable"),
    (200, {"code": "1", "msg": "authentication failed"}, {}, "authentication failed"),
    (200, {"data": ""}, {}, "下载地址"),
    (200, {"data": DOWNLOAD_URL, "success": False, "message": "permission denied"},
     {"success": "success"}, "permission denied"),
    (200, b"<html>temporary upstream error</html>", {}, "下载地址"),
    (200, b"", {}, "下载地址"),
    (200, ["unexpected JSON"], {}, "下载地址"),
    (200, {"data": 42}, {}, "下载地址"),
    (200, {"data": "not a download URL"}, {}, "下载地址"),
    (200, b'{"message":"authentication failed"}', {"result": None}, "下载地址"),
])
def test_legacy_ticket_failure_reports_reason_without_blacklisting(
        boundary: SimpleNamespace, method: str, status: Optional[int], payload: object,
        options: dict[str, Any], detail: str,
) -> None:
    """
    旧换票的无响应、错误 JSON 和 HTTP 失败均上报原因，不请求种子或污染黑名单。
    """
    response = None if status is None else _response(payload, status)
    getattr(boundary.ticket, method).return_value = response
    boundary.torrent.enclosure = _ticket(method=method, **options)
    errors: list[str] = []

    assert boundary.chain.download_torrent(boundary.torrent, on_failure=errors.append) == (None, "", [])

    assert len(errors) == 1 and errors[0].startswith("换票失败：") and detail in errors[0]
    if status is not None:
        assert f"HTTP {status}" in errors[0]
        response.close.assert_called_once()
    assert DownloadFailureOwner._download_failure_ttl(errors[0]) == 3600
    assert errors[0] in boundary.chain.post_message.call_args.args[0].text
    assert errors[0] in str(boundary.log.error.call_args_list)
    getattr(boundary.ticket, method).assert_called_once()
    boundary.port.request.assert_not_called()
    boundary.helper.add_invalid.assert_not_called()


@pytest.mark.parametrize("status,payload", [
    (404, b"expired ticket"), (401, b"unauthorized"), (403, b"forbidden"), (500, b"upstream error"),
    (200, {"message": "torrent not found"}), (200, b"<html>login required</html>"),
    (200, b"d4:info"), (200, b""),
])
def test_temporary_download_failure_does_not_mark_resource_corrupt(
        boundary: SimpleNamespace, status: int, payload: object,
) -> None:
    """
    临时地址的过期、鉴权、上游错误和无效内容使用短冷却，不冒充原资源损坏。
    """
    boundary.port.request.return_value = _response(payload, status)
    errors: list[str] = []

    assert boundary.chain.download_torrent(boundary.torrent, on_failure=errors.append) == (None, "", [])

    assert len(errors) == 1 and errors[0].startswith("临时下载地址失效：")
    assert "种子数据有误" not in errors[0]
    if status != 200:
        assert str(status) in errors[0]
    assert DownloadFailureOwner._download_failure_ttl(errors[0]) == 3600
    assert errors[0] in boundary.chain.post_message.call_args.args[0].text
    boundary.ticket.post.assert_called_once()
    boundary.port.request.assert_called_once()
    boundary.helper.add_invalid.assert_not_called()
    assert not boundary.helper.is_invalid(boundary.torrent.enclosure)
    assert not boundary.helper.is_invalid(DOWNLOAD_URL)


def test_retry_fetches_new_ticket_after_temporary_content_failure(boundary: SimpleNamespace) -> None:
    """
    临时响应无效后再次调用必须重新换票，成功内容仍按原合同解析和缓存。
    """
    new_url = DOWNLOAD_URL.replace("synthetic-ticket", "new-ticket")
    boundary.ticket.post.side_effect = [boundary.ticket_response, _response({"data": new_url})]
    boundary.port.request.side_effect = [_response({"message": "expired"}), _response(CONTENT)]

    assert boundary.chain.download_torrent(boundary.torrent) == (None, "", [])
    boundary.ticket.post.assert_called_once()
    assert boundary.chain.download_torrent(boundary.torrent) == (CONTENT, "", ["Series.mkv"])
    assert [call.kwargs["url"] for call in boundary.port.request.call_args_list] == [DOWNLOAD_URL, new_url]
    assert boundary.ticket.post.call_count == 2
    boundary.helper.add_invalid.assert_not_called()
    boundary.chain.post_message.assert_called_once()


@pytest.mark.parametrize("partial", [None, False, True])
def test_ticket_failure_reaches_preparation_and_batch_cooldown(boundary: SimpleNamespace, partial: Optional[bool]) -> None:
    """
    单资源准备、完整包和拆包读取都持久化真实换票原因，并只写一次一小时冷却。
    """
    boundary.ticket.post.return_value = _response({"message": "torrent not found"}, 404)
    if partial is None:
        prepared, error = boundary.chain._prepare_download_single(
            context=boundary.context, torrent_file=None, torrent_content=None, episodes=None,
            channel=None, source="Subscribe|7372", downloader=None, save_path=None, userid=None, username=None,
        )
        assert prepared is None
    else:
        options = dict(save_path=None, channel=None, source="Subscribe|7372", userid=None,
                       username=None, downloader=None, custom_words=None, governance=None)
        batch = _BatchDownloadRun(boundary.chain, [], {}, options, None)
        assert batch._read_torrent(boundary.context, partial=partial) is None
        error = boundary.chain.post_message.call_args.args[0].text.split("错误信息：", 1)[1].split("\n站点：", 1)[0]

    boundary.chain.download_failure_repository.record_failure.assert_called_once()
    write = boundary.chain.download_failure_repository.record_failure.call_args.args[0]
    assert write.error_message == error and error.startswith("换票失败：")
    assert (datetime.fromisoformat(write.next_retry_at) - datetime.fromisoformat(write.failed_at)).total_seconds() == 3600
    assert write.source == "Subscribe|7372"


def test_missing_content_result_still_reports_failure(boundary: SimpleNamespace) -> None:
    """
    旧调用或扩展返回空准备结果时也触发一次失败回调和通知，不静默退出。
    """
    boundary.chain._get_torrent_content = Mock(return_value=None)
    errors: list[str] = []
    assert boundary.chain.download_torrent(boundary.torrent, on_failure=errors.append) == (None, "", [])
    assert len(errors) == 1 and errors[0].startswith("换票失败：")
    boundary.chain.post_message.assert_called_once()
    boundary.helper.add_invalid.assert_not_called()


@pytest.mark.parametrize("cookie_allowed", [False, True])
def test_ticket_diagnostics_hide_credentials_and_report_cookie_usage(boundary: SimpleNamespace, cookie_allowed: bool) -> None:
    """
    日志只记录 Cookie 是否发送，错误摘要不泄露请求密钥、Cookie 或签名路径。
    """
    boundary.torrent.enclosure = _ticket(cookie=cookie_allowed, header={"x-api-key": "synthetic-api-key"})
    message = ("authentication failed synthetic-api-key session=synthetic-cookie "
               "https://tracker.example/dl/private-path-ticket?sign=private-query-ticket")
    boundary.ticket.post.return_value = _response({"code": "1", "message": message, "token": "private-body-token"})
    errors: list[str] = []
    boundary.chain.download_torrent(boundary.torrent, on_failure=errors.append)
    diagnostic = errors[0] + str(boundary.log.error.call_args_list) + boundary.chain.post_message.call_args.args[0].text
    assert "authentication failed" in diagnostic and "HTTP 200" in diagnostic
    for secret in ("synthetic-api-key", "synthetic-cookie", "private-path-ticket", "private-query-ticket", "private-body-token"):
        assert secret not in diagnostic
    assert f"携带 Cookie：{'是' if cookie_allowed else '否'}" in diagnostic
    assert boundary.ticket.post.call_args.kwargs["cookies"] == (boundary.torrent.site_cookie if cookie_allowed else None)


@pytest.mark.parametrize("stage", ["ticket", "content"])
@pytest.mark.parametrize("message,ttl", [
    ("相同種子當天最多下載10次", 86400),
    ("Request frequency limit, please try in 600 seconds.", 600),
])
def test_rate_limit_keeps_existing_site_cooldown(boundary: SimpleNamespace, stage: str, message: str, ttl: int) -> None:
    """
    换票和临时 GET 的明确限流仍优先于瞬时分类，并阻止冷却期内再次请求。
    """
    response = _response({"message": message})
    if stage == "ticket":
        boundary.ticket.post.return_value = response
    else:
        boundary.port.request.return_value = response
    errors: list[str] = []
    for _ in range(2):
        assert boundary.chain.download_torrent(boundary.torrent, on_failure=errors.append) == (None, "", [])
    assert errors == [f"站点限流：{message}"] * 2
    assert DownloadFailureOwner._download_failure_ttl(errors[0]) == ttl
    boundary.ticket.post.assert_called_once()
    assert boundary.port.request.call_count == (1 if stage == "content" else 0)
    boundary.helper.add_invalid.assert_not_called()


def test_direct_corrupt_torrent_keeps_resource_blacklist(boundary: SimpleNamespace) -> None:
    """
    直接链接耗尽重试仍无法解析时，保留原资源损坏原因和二十四小时黑名单。
    """
    url = "https://tracker.example/download.php?id=39"
    boundary.torrent.enclosure = url
    boundary.port.request.return_value = _response(b"d4:info")
    errors: list[str] = []
    assert boundary.chain.download_torrent(boundary.torrent, on_failure=errors.append) == (None, "", [])
    assert errors == ["种子数据有误，请确认链接是否正确"]
    assert DownloadFailureOwner._download_failure_ttl(errors[0]) == 86400
    assert boundary.helper.is_invalid(url)
    assert boundary.port.request.call_count == 3
    boundary.ticket.post.assert_not_called()
