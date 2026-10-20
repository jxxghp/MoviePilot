"""
种子 GET 的有界重试、缓存与冷却策略离线回归。
"""

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from requests import Response

import app.application.torrent.download as download
import app.chain.download.failure as failure_owner
import app.chain.download.submission as submission
from app.chain.download import DownloadChain
from app.chain.download.failure import (
    DOWNLOAD_FAILURE_DUPLICATE_MESSAGE,
    DOWNLOAD_FAILURE_DUPLICATE_TTL_SECONDS,
    DownloadFailureOwner,
)
from app.db.adapters.download import TransactionalDownloadFailureRepository
from app.db.models.downloadfailure import DownloadFailure
from app.db.session import SessionFactory
from app.domain.context import Context, MediaInfo, TorrentInfo
from app.domain.metainfo import MetaInfo
from app.schemas.types import MediaType

URL = "https://tracker.example/download.php?id=39"
CONTENT = b"d4:infod4:name10:Series.mkv6:lengthi1eee"


def _response(status=200, content=CONTENT):
    """
    使用真实 Response 保证错误状态的布尔值不会掩盖状态码。
    """
    response = Response()
    response.status_code = status
    response._content = content
    response.encoding = "utf-8"
    return response


@pytest.fixture
def boundary(monkeypatch):
    """
    隔离 HTTP、缓存和等待，保留真实种子解析与用例编排。
    """
    port = Mock()
    monkeypatch.setattr(download, "_require_torrent_port", Mock(return_value=port))
    cache = Mock()
    cache.get.return_value = None
    monkeypatch.setattr(download, "FileCache", Mock(return_value=cache))
    sleep = Mock()
    monkeypatch.setattr(download.time, "sleep", sleep)
    monkeypatch.setattr(download, "TTLCache", Mock(return_value={}))
    helper = download.TorrentHelper()
    helper.add_invalid = Mock(wraps=helper.add_invalid)
    return helper, port, cache, sleep


@pytest.mark.parametrize("failure", [None, 403, 500, 502, 503, 525, b"", b"d4:info", b"<html>CF</html>"])
def test_transient_failure_recovers_without_invalid_cache(boundary, failure):
    """
    网络异常的 None、代理错误和截断响应均可恢复，保持访问参数。
    """
    helper, port, cache, sleep = boundary
    failed = _response(failure) if isinstance(failure, int) else (
        _response(content=failure) if failure is not None else None
    )
    port.request.side_effect = [failed, _response()]

    result = helper.download_torrent(URL, cookie="session=test", ua="TestAgent", referer="https://tracker.example/")

    assert result[1:5] == (CONTENT, "", ["Series.mkv"], "")
    assert port.request.call_count == 2
    assert port.request.call_args_list[0] == port.request.call_args_list[1]
    sleep.assert_called_once_with(1)
    helper.add_invalid.assert_not_called()
    cache.set.assert_called_once()


@pytest.mark.parametrize("status", [None, 403, 525])
def test_exhausted_retry_is_bounded_and_does_not_poison_url(boundary, status):
    """
    失败耗尽后保留最终原因，总共三次请求且不写 24 小时缓存。
    """
    helper, port, cache, sleep = boundary
    port.request.return_value = None if status is None else _response(status)

    result = helper.download_torrent(URL)

    assert result[1] is None
    assert result[4] == ("无法打开链接" if status is None else f"下载种子出错，状态码：{status}")
    assert port.request.call_count == 3
    assert [call.args[0] for call in sleep.call_args_list] == [1, 2]
    helper.add_invalid.assert_not_called()
    cache.set.assert_not_called()


@pytest.mark.parametrize("status", [401, 404, 410, 429])
def test_permanent_errors_and_rate_limit_are_not_retried(boundary, status):
    """
    确定失效地址保留无效缓存，429 保留站点流控语义且不短退避。
    """
    helper, port, _, sleep = boundary
    port.request.return_value = _response(status)

    assert helper.download_torrent(URL)[1] is None

    port.request.assert_called_once()
    sleep.assert_not_called()
    assert helper.add_invalid.call_count == (0 if status == 429 else 1)


@pytest.mark.parametrize("signed,cache_invalid", [(True, True), (False, False)])
def test_signed_and_indirect_links_keep_single_attempt(boundary, signed, cache_invalid):
    """
    签名刷新和换票继续由上层持有重试预算。
    """
    helper, port, _, sleep = boundary
    port.request.return_value = _response(403)
    url = URL + "&t=1&sign=old" if signed else URL

    assert helper.download_torrent(url, cache_invalid=cache_invalid)[1] is None

    port.request.assert_called_once()
    sleep.assert_not_called()
    helper.add_invalid.assert_not_called()


def test_first_download_confirmation_post_is_not_retried(boundary):
    """
    识别首次下载确认页后只提交一次 POST，不重复有副作用的操作。
    """
    helper, port, _, sleep = boundary
    page = '下载种子文件<form action="?"><input name="confirm" value="yes"></form>'.encode()
    port.request.side_effect = [_response(content=page), _response(503)]

    result = helper.download_torrent(URL)
    assert result[1] is None
    assert result[4] == "下载种子出错，状态码：503"

    assert [call.kwargs["method"] for call in port.request.call_args_list] == ["GET", "POST"]
    sleep.assert_not_called()
    helper.add_invalid.assert_not_called()


def test_first_download_confirmation_preserves_quota_error(boundary):
    """
    确认 POST 遇到站点限流时保留原因，不把限流页当作失效种子。
    """
    helper, port, _, _ = boundary
    page = '下载种子文件<form action="?"><input name="confirm" value="yes"></form>'.encode()
    message = "相同種子當天最多下載10次"
    port.request.side_effect = [_response(content=page), _response(content=json.dumps({"message": message}).encode())]

    assert helper.download_torrent(URL)[4] == f"站点限流：{message}"
    assert port.request.call_count == 2
    helper.add_invalid.assert_not_called()


def test_first_download_confirmation_empty_response_is_transient(boundary):
    """
    确认请求的空响应不证明种子无效，不能污染资源黑名单。
    """
    helper, port, _, _ = boundary
    page = '下载种子文件<form action="?"><input name="confirm" value="yes"></form>'.encode()
    port.request.side_effect = [_response(content=page), _response(content=b"")]

    assert helper.download_torrent(URL)[4] == "未下载到种子数据"
    assert port.request.call_count == 2
    helper.add_invalid.assert_not_called()


@pytest.mark.parametrize("form", [
    '<form action="?"><input value="yes" name="confirm"></form>',
    "<form method='POST' action='?'><input value='a&amp;b' name='token'></form>",
    '<form method="post"><input name="confirm" value="yes"></form>',
    '<form action="download.php?id=39" method="post"><input name="confirm" value="yes"></form>',
    '<form action="?"><input name="token" value="a&amp;b"><button name="confirm" value="yes">下载</button></form>',
])
def test_first_download_form_variants_are_confirmed(boundary, form):
    """
    按 HTML 表单语义提交确认，兼容属性顺序、引号、实体和提交按钮。
    """
    helper, port, cache, sleep = boundary
    page = ("下载种子文件" + form).encode()
    port.request.side_effect = [_response(content=page), _response()]

    assert helper.download_torrent(URL, cookie="session=test")[1] == CONTENT

    assert [call.kwargs["method"] for call in port.request.call_args_list] == ["GET", "POST"]
    posted = port.request.call_args.kwargs
    assert posted["url"] == URL
    assert posted["cookie"] == "session=test"
    assert posted["referer"] == URL
    assert posted["data"] == ({"token": "a&b", "confirm": "yes"} if "button" in form else
                              {"token": "a&b"} if "token" in form else {"confirm": "yes"})
    cache.set.assert_called_once()
    helper.add_invalid.assert_not_called()
    sleep.assert_not_called()


def test_first_download_form_never_posts_credentials_to_other_origin(boundary):
    """
    确认表单不能把站点 Cookie 和隐藏凭据发送到其他来源。
    """
    helper, port, _, _ = boundary
    page = '下载种子文件<form action="https://other.example/download.php"><input name="token" value="secret"></form>'
    port.request.return_value = _response(content=page.encode())

    assert helper.download_torrent(URL, cookie="session=test")[1] is None
    port.request.assert_called_once()
    helper.add_invalid.assert_called_once_with(URL)


def test_failed_confirmation_blacklists_original_url_after_redirect(boundary):
    """
    确认后仍返回 HTML 时屏蔽原资源地址，刷新候选使用同一个黑名单键。
    """
    helper, port, _, _ = boundary
    redirect = _response(302)
    redirect.headers["Location"] = "https://tracker.example/download.php?id=39&confirm=1"
    page = '下载种子文件<form action="?"><input name="confirm" value="yes"></form>'.encode()
    port.request.side_effect = [redirect, _response(content=page), _response(content=page)]

    assert helper.download_torrent(URL)[1] is None
    helper.add_invalid.assert_called_once_with(URL)
    assert helper.is_invalid(URL)


def test_chain_blacklist_suppresses_repeated_requests_and_notifications(boundary):
    """
    多轮下载同一失败资源只提醒首次，并阻止后续请求和错误通知。
    """
    _, port, _, _ = boundary
    port.request.return_value = _response(content=b"<html>invalid torrent</html>")
    chain = object.__new__(DownloadChain)
    chain.post_message = Mock()
    torrent = TorrentInfo(site=1, site_name="test", title="Series S01E39", enclosure=URL)

    for _ in range(5):
        assert chain.download_torrent(torrent)[0] is None

    assert port.request.call_count == 3
    chain.post_message.assert_called_once()


@pytest.mark.parametrize("error", ["种子数据有误，请确认链接是否正确",
                                   "种子数据有误，请确认链接是否正确，如为PT站点则需手工在站点下载一次种子"])
def test_invalid_torrent_data_has_resource_cooldown(error):
    """
    无效种子及确认失败使用资源级冷却，重启也不会退回每小时通知。
    """
    assert DownloadFailureOwner._download_failure_ttl(error) == 86400


def test_invalid_content_preparation_preserves_persisted_resource_cooldown(boundary, monkeypatch):
    """
    单任务准备保留无效种子原因，持久化冷却不能再被统一空内容错误覆盖。
    """
    _, port, _, _ = boundary
    port.request.return_value = _response(content=b"<html>invalid torrent</html>")
    chain = object.__new__(DownloadChain)
    chain.post_message = Mock()
    chain.download_failure_repository = Mock()
    chain._apply_resource_download_event = Mock(return_value=(None, None))
    context = Context(
        torrent_info=TorrentInfo(site=1, site_name="test", title="Series S01E39", enclosure=URL),
        media_info=MediaInfo(title="Series", type=MediaType.TV), meta_info=MetaInfo("Series S01E39"),
    )
    media = Mock()
    media.supplement_tmdb_info.return_value = context.media_info
    monkeypatch.setattr(submission, "MediaChain", Mock(return_value=media))

    prepared, error = chain._prepare_download_single(
        context=context, torrent_file=None, torrent_content=None, episodes=None,
        channel=None, source="Subscribe|1", downloader=None, save_path=None, userid=None, username=None,
    )

    assert prepared is None
    failure = chain.download_failure_repository.record_failure.call_args.args[0]
    assert failure.error_message == error == "种子数据有误，请确认链接是否正确"
    assert (datetime.fromisoformat(failure.next_retry_at)
            - datetime.fromisoformat(failure.failed_at)).total_seconds() == 86400


def _duplicate_backoff_context():
    """
    构造可生成稳定资源指纹的重复提交上下文。
    """
    return Context(
        torrent_info=TorrentInfo(site=1, site_name="test", title="Series S01", enclosure=URL),
        media_info=MediaInfo(title="Series", type=MediaType.TV),
        meta_info=MetaInfo("Series S01"),
    )


@pytest.mark.parametrize("source,recorded", [("Subscribe|1", True), ("MoviePilot|手动下载", False)])
def test_duplicate_submission_backoff_is_subscribe_only_and_long(source, recorded):
    """
    重复提交退避只为订阅来源登记台账，并使用覆盖重新选源周期的长冷却与中性成因。
    """
    chain = object.__new__(DownloadChain)
    chain.download_failure_repository = Mock()
    context = _duplicate_backoff_context()

    fingerprint = chain._record_duplicate_download_backoff(
        context=context, downloader="qb", source=source,
    )

    if not recorded:
        assert fingerprint is None
        chain.download_failure_repository.record_failure.assert_not_called()
        return
    write = chain.download_failure_repository.record_failure.call_args.args[0]
    assert write.error_message == DOWNLOAD_FAILURE_DUPLICATE_MESSAGE
    assert write.downloader == "qb"
    assert (datetime.fromisoformat(write.next_retry_at)
            - datetime.fromisoformat(write.failed_at)).total_seconds() == DOWNLOAD_FAILURE_DUPLICATE_TTL_SECONDS
    assert fingerprint == chain._build_download_failure_fingerprint(context, resource_backoff=True)


def test_duplicate_backoff_cools_only_that_resource_for_subscription_candidates(db, monkeypatch):
    """
    退避按资源指纹生效：同一媒体的其他种子仍可被候选选中。
    """
    db.watermark(DownloadFailure)
    chain = object.__new__(DownloadChain)
    chain.download_failure_repository = TransactionalDownloadFailureRepository(SessionFactory)
    cooled = _duplicate_backoff_context()
    cooled.media_info = MediaInfo(title="Series", type=MediaType.MOVIE)
    other = Context(
        torrent_info=TorrentInfo(site=1, site_name="test", title="Series S01 1080p",
                                 enclosure="https://tracker.example/download.php?id=40"),
        media_info=cooled.media_info,
        meta_info=MetaInfo("Series S01"),
    )
    fingerprint = chain._record_duplicate_download_backoff(
        context=cooled, downloader="qb", source="Subscribe|1",
    )
    chain.eventmanager = Mock()
    chain.eventmanager.send_event.return_value = None
    chain.download_single = Mock(return_value="new-hash")
    monkeypatch.setattr(download.TorrentHelper, "sort_torrents", lambda _self, contexts: contexts)

    downloads, _lefts = chain.batch_download(contexts=[cooled, other], source="Subscribe|1")

    assert fingerprint
    assert downloads == [other]
    chain.download_single.assert_called_once()
    assert chain.download_single.call_args.args[0] is other


def test_duplicate_backoff_survives_whole_season_episode_expansion(db):
    """
    整季种子解析后的退避，在下一轮尚未解析文件集数时仍须命中同一资源。
    """
    db.watermark(DownloadFailure)
    chain = object.__new__(DownloadChain)
    chain.download_failure_repository = TransactionalDownloadFailureRepository(SessionFactory)
    submitted = _duplicate_backoff_context()
    submitted.meta_info.set_episodes(begin=1, end=12)
    chain._record_duplicate_download_backoff(context=submitted, source="Subscribe|1")
    next_round = _duplicate_backoff_context()

    active = chain._active_download_failure_fingerprints(contexts=[next_round], source="Subscribe|1")

    assert chain._build_download_failure_fingerprint(next_round) in active


def test_cooldown_log_keeps_backoff_reason_neutral(monkeypatch):
    """
    候选跳过日志转述台账成因，不把重复提交退避说成添加下载失败。
    """
    logger = Mock()
    monkeypatch.setattr(failure_owner, "logger", logger)

    DownloadFailureOwner._log_download_failure_cooldown(
        Context(torrent_info=TorrentInfo(title="Series S01")),
        SimpleNamespace(error_message=DOWNLOAD_FAILURE_DUPLICATE_MESSAGE,
                       next_retry_at="2026-10-26 12:00:00"),
    )

    text = logger.info.call_args.args[0]
    assert DOWNLOAD_FAILURE_DUPLICATE_MESSAGE in text
    assert "将于 2026-10-26 12:00:00 后重试" in text
    assert "添加下载失败" not in text

    DownloadFailureOwner._log_download_failure_cooldown(
        Context(torrent_info=TorrentInfo(title="Series S01")),
        SimpleNamespace(error_message=None, next_retry_at=None),
    )

    assert logger.info.call_args.args[0] == "Series S01 暂时跳过该资源（原因：未知原因），本次继续尝试其他资源"


def test_redirected_get_retries_at_destination(boundary):
    """
    重定向目标上的瞬时错误也重试，并保留原始 URL 的成功缓存键。
    """
    helper, port, cache, sleep = boundary
    redirect = _response(302)
    redirect.headers["Location"] = "https://tracker.example/file.torrent"
    port.request.side_effect = [redirect, _response(525), _response()]

    assert helper.download_torrent(URL)[1] == CONTENT

    assert [call.kwargs["url"] for call in port.request.call_args_list] == [URL, redirect.headers["Location"], redirect.headers["Location"]]
    sleep.assert_called_once_with(1)
    cache.set.assert_called_once()


def test_second_retry_can_recover(boundary):
    """
    第二次退避后成功时只缓存最终合法种子。
    """
    helper, port, cache, sleep = boundary
    port.request.side_effect = [None, _response(content=b"d4:info"), _response()]
    assert helper.download_torrent(URL)[1] == CONTENT
    assert port.request.call_count == 3
    assert [call.args[0] for call in sleep.call_args_list] == [1, 2]
    cache.set.assert_called_once()


@pytest.mark.parametrize("content", [b"", b"d4:info", b"<html>CF</html>"])
def test_invalid_content_exhaustion_never_caches_response(boundary, content):
    """
    无效内容耗尽重试后进入黑名单，空响应仍按瞬时失败处理。
    """
    helper, port, cache, _ = boundary
    port.request.return_value = _response(content=content)
    assert helper.download_torrent(URL)[1] is None
    assert port.request.call_count == 3
    cache.set.assert_not_called()
    assert helper.add_invalid.call_count == (1 if content else 0)


@pytest.mark.parametrize("content", [CONTENT, b"magnet:?xt=urn:btih:abc"])
def test_valid_response_has_no_retry(boundary, content):
    """
    合法种子和磁力响应只发起一次请求。
    """
    helper, port, _, sleep = boundary
    port.request.return_value = _response(content=content)
    assert helper.download_torrent(URL)[1]
    port.request.assert_called_once()
    sleep.assert_not_called()


def test_valid_cache_bypasses_network(boundary):
    """
    已有合法种子缓存时不发起请求或退避。
    """
    helper, port, cache, sleep = boundary
    cache.get.return_value = CONTENT
    assert helper.download_torrent(URL)[1] == CONTENT
    port.request.assert_not_called()
    sleep.assert_not_called()


@pytest.mark.parametrize("error,ttl", [
    ("下载种子内容为空", 3600),
    ("无法打开链接", 3600),
    ("下载种子出错，状态码：525", 3600),
    ("无法读取种子文件", 86400),
    ("torrent not found", 86400),
])
def test_generic_empty_download_is_transient(error, ttl):
    """
    空内容不证明资源失效；明确文件损坏或删除继续按一天冷却。
    """
    assert DownloadFailureOwner._download_failure_ttl(error) == ttl


@pytest.mark.parametrize("message,ttl", [
    ("相同種子當天最多下載10次", 86400),
    ("相同种子当天最多下载10次", 86400),
    ("当前请求太多，请10秒后重试", 3600),
    ("請求過於頻繁，請2小時後重試", 7200),
    ("Too many requests, retry after 90 minutes", 5400),
])
@pytest.mark.parametrize("status", [200, 403, 429])
@pytest.mark.parametrize("field", ["message", "msg"])
def test_rate_limit_json_preserves_reason_without_retry(boundary, message, ttl, status, field):
    """
    站点明确限流优先于 HTTP 状态，不解析为种子、不重试或污染无效地址缓存。
    """
    helper, port, cache, sleep = boundary
    port.request.return_value = _response(status, json.dumps({"code": "1", field: message}, ensure_ascii=False).encode())
    parser = Mock(wraps=helper.get_fileinfo_from_torrent_content)
    helper.get_fileinfo_from_torrent_content = parser

    result = helper.download_torrent(URL)

    assert result[1] is None
    assert result[4] == f"站点限流：{message}"
    assert DownloadFailureOwner._download_failure_ttl(result[4]) == ttl
    port.request.assert_called_once()
    sleep.assert_not_called()
    parser.assert_not_called()
    cache.set.assert_not_called()
    helper.add_invalid.assert_not_called()


@pytest.mark.parametrize("content", [
    b'{"message":"torrent not found"}', b'{"message":"SUCCESS"}',
    b'{"message":10}', b'{"message":{"error":"too many requests"}}',
    b'{"message":"too many requests"', b'["too many requests"]',
])
def test_other_json_does_not_become_rate_limit(boundary, content):
    """
    无关错误、非文本字段和截断 JSON 保留原有恢复预算。
    """
    helper, port, _, sleep = boundary
    port.request.side_effect = [_response(content=content), _response()]
    assert helper.download_torrent(URL)[1] == CONTENT
    assert port.request.call_count == 2
    sleep.assert_called_once_with(1)


def test_rate_limit_at_redirect_destination_is_not_retried(boundary):
    """
    重定向落地返回配额错误时停止请求，不能继续消耗下载次数。
    """
    helper, port, _, sleep = boundary
    redirect = _response(302)
    redirect.headers["Location"] = "https://tracker.example/file.torrent"
    message = "相同種子當天最多下載10次"
    port.request.side_effect = [redirect, _response(content=json.dumps({"message": message}).encode())]
    assert helper.download_torrent(URL)[4] == f"站点限流：{message}"
    assert port.request.call_count == 2
    sleep.assert_not_called()


@pytest.mark.parametrize("link_type", ["ordinary", "signed", "indirect"])
def test_daily_quota_reaches_notification_and_persisted_cooldown(boundary, monkeypatch, link_type):
    """
    真实准备流程保留限流原因到通知和冷却记录，签名与换票也不得刷新重试。
    """
    _, port, cache, sleep = boundary
    message = "相同種子當天最多下載10次"
    port.request.return_value = _response(content=json.dumps({"code": "1", "message": message}).encode())
    chain = object.__new__(DownloadChain)
    chain.post_message = Mock()
    chain.search_site_torrents = Mock()
    chain.download_failure_repository = Mock()
    chain._apply_resource_download_event = Mock(return_value=(None, None))
    chain._record_music_album_track_keys = Mock()
    chain._resolve_media_download_dir = Mock(return_value=("local", Path("/downloads"), None))
    chain._resolve_indirect_download_url = Mock(return_value=URL)
    enclosure = {"ordinary": URL, "signed": URL + "&t=1&sign=old",
                 "indirect": "[encoded]https://api.m-team.cc/api/torrent/genDlToken"}[link_type]
    context = Context(
        torrent_info=TorrentInfo(site=1, site_name="馒头", title="Series S01E39",
                                 enclosure=enclosure),
        media_info=MediaInfo(title="Series", type=MediaType.TV), meta_info=MetaInfo("Series S01E39"),
    )
    media = Mock()
    media.supplement_tmdb_info.return_value = context.media_info
    monkeypatch.setattr(submission, "MediaChain", Mock(return_value=media))

    prepared, error = chain._prepare_download_single(
        context=context, torrent_file=None, torrent_content=None, episodes=None,
        channel=None, source="Subscribe|1", downloader=None, save_path=None,
        userid=None, username=None,
    )

    assert prepared is None
    assert error == f"站点限流：{message}"
    chain.download_failure_repository.record_failure.assert_called_once()
    failure = chain.download_failure_repository.record_failure.call_args.args[0]
    assert failure.error_message == error
    assert (datetime.fromisoformat(failure.next_retry_at)
            - datetime.fromisoformat(failure.failed_at)).total_seconds() == 86400
    chain.post_message.assert_called_once()
    assert error in chain.post_message.call_args.args[0].text
    chain.search_site_torrents.assert_not_called()
    port.request.assert_called_once()
    sleep.assert_not_called()
    cache.set.assert_not_called()
