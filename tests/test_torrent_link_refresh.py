"""临时下载凭证更新与有界恢复的离线回归。"""

import base64
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import app.application.torrent.download as torrent_download
import app.chain.download.submission as submission
import app.chain.torrents as torrents_module
from app.chain.download import DownloadChain
from app.chain.torrents import TorrentsChain
from app.domain import torrent as torrent_rules
from app.domain.context import Context, MediaInfo, TorrentInfo
from app.domain.metainfo import MetaInfo
from app.runtime.config import ConfigModel, settings
from app.schemas.types import MediaType

SITE = {"id": 1, "name": "测试站", "domain": "https://tracker.example/"}
OLD_URL = "https://tracker.example/download.php?id=39&t=100&sign=old"
NEW_URL = "https://tracker.example/download.php?id=39&t=200&sign=new"
FAILED = (None, None, "", [], "下载种子出错，状态码：404")
DOWNLOADED = (None, b"torrent-content", "Series", ["Series.mkv"], "")


def _torrent(**kwargs) -> TorrentInfo:
    """构造稳定种子身份与可轮换下载凭证。"""
    fields = {
        "site": 1,
        "site_name": "测试站",
        "title": "Series S01E39 2160p WEB-DL",
        "description": "同一资源",
        "enclosure": OLD_URL,
        "page_url": "https://tracker.example/details.php?id=39&hit=1",
        "site_cookie": "session=old",
        "site_ua": "OldAgent",
        "category": MediaType.TV.value,
    }
    fields.update(kwargs)
    return TorrentInfo(**fields)


@pytest.fixture
def refresh_chain(monkeypatch):
    """隔离资源缓存、识别和索引器，仅执行真实刷新编排。"""
    chain = object.__new__(TorrentsChain)
    chain.load_cache = Mock(return_value={})
    chain.save_cache = Mock()
    chain.browse = Mock(side_effect=lambda **kwargs: [] if kwargs.get("page") else chain.rss())
    chain.rss = Mock(return_value=[])
    chain._build_refresh_context = Mock(side_effect=lambda torrent, _stype: Context(torrent_info=torrent))
    sites = Mock()
    sites.get_indexers.return_value = [SITE]
    monkeypatch.setattr(torrents_module, "SitesHelper", Mock(return_value=sites))
    monkeypatch.setattr(torrents_module.TorrentHelper, "is_invalid", lambda _self, _url: False)
    return chain


@pytest.mark.parametrize("stype", ["spider", "rss"])
@pytest.mark.parametrize("category", [MediaType.TV.value, MediaType.MUSIC.value])
def test_refresh_replaces_download_metadata_without_losing_recognition(refresh_chain, stype, category):
    """无新资源时也保存最新链接和访问参数，保留媒体识别结果与缓存位置。"""
    cached = Context(
        torrent_info=_torrent(category=category),
        meta_info=MetaInfo("Series S01E39"),
        media_info=MediaInfo(title="人工校正标题"),
        candidate_recognized=True,
        media_recognize_fail_count=2,
    )
    latest = replace(cached.torrent_info, enclosure=NEW_URL, site_cookie="session=new",
                     site_ua="NewAgent", site_proxy=True, seeders=20)
    video_file, music_file = refresh_chain.cache_files(stype)
    cache_file = music_file if category == MediaType.MUSIC.value else video_file
    refresh_chain.load_cache.side_effect = lambda filename: {"tracker.example": [cached]} if filename == cache_file else {}
    refresh_chain.rss.return_value = [latest]

    result = refresh_chain.refresh(stype=stype, sites=[1])

    assert result["tracker.example"] == [cached]
    assert cached.torrent_info is latest
    assert cached.media_info.title == "人工校正标题"
    assert cached.candidate_recognized is True
    assert cached.media_recognize_fail_count == 2
    refresh_chain._build_refresh_context.assert_not_called()
    saved = {call.args[1]: call.args[0] for call in refresh_chain.save_cache.call_args_list}
    assert saved[cache_file]["tracker.example"][0].torrent_info.enclosure == NEW_URL


def test_refresh_keeps_distinct_ids_with_identical_titles(refresh_chain):
    """同标题的不同种子不能相互覆盖，抓取两页的同一种子只缓存一次。"""
    cached = Context(torrent_info=_torrent())
    latest = _torrent(enclosure=NEW_URL)
    another = _torrent(enclosure="https://tracker.example/download.php?id=41",
                       page_url="https://tracker.example/details.php?id=41")
    refresh_chain.load_cache.side_effect = lambda filename: {"tracker.example": [cached]} if filename == refresh_chain.cache_file else {}
    refresh_chain.rss.return_value = [latest, another, another]

    result = refresh_chain.refresh(stype="spider", sites=[1])

    assert [item.torrent_info for item in result["tracker.example"]] == [latest, another]
    refresh_chain._build_refresh_context.assert_called_once_with(another, "spider")


def test_refresh_rebuilds_changed_title_for_same_id(refresh_chain):
    """同一种子的标题变化必须重新识别，并移除旧候选。"""
    cached = Context(torrent_info=_torrent())
    latest = _torrent(title="Series S01E39-E40 2160p WEB-DL", enclosure=NEW_URL)
    cache = {"tracker.example": [cached]}
    refresh_chain.load_cache.side_effect = lambda filename: cache if filename == refresh_chain.cache_file else {}
    refresh_chain.rss.return_value = [latest]

    result = refresh_chain.refresh(stype="spider", sites=[1])

    assert [item.torrent_info for item in result["tracker.example"]] == [latest]
    refresh_chain._build_refresh_context.assert_called_once_with(latest, "spider")


@pytest.fixture
def download_chain(monkeypatch):
    """在下载文件和站点查询边界打桩，禁止真实网络和下载器提交。"""
    chain = object.__new__(DownloadChain)
    chain.post_message = Mock()
    chain.search_site_torrents = Mock(return_value=[])
    helper = Mock()
    helper.download_torrent.return_value = FAILED
    monkeypatch.setattr(submission, "TorrentHelper", Mock(return_value=helper))
    sites = Mock()
    sites.get_indexers.return_value = [SITE]
    monkeypatch.setattr(submission, "SitesHelper", Mock(return_value=sites), raising=False)
    return chain, helper


def test_expired_signed_link_refreshes_same_resource_before_failure(download_chain):
    """旧签名失败后只下载同站同 ID 的新链接，成功时不发失败通知。"""
    chain, helper = download_chain
    torrent = _torrent()
    refreshed = _torrent(enclosure=NEW_URL, site_cookie="session=new", site_ua="NewAgent", site_proxy=True)
    wrong_site = replace(refreshed, site=2)
    wrong_id = replace(refreshed, page_url="https://tracker.example/details.php?id=40")
    chain.search_site_torrents.return_value = [wrong_site, wrong_id, refreshed]
    helper.download_torrent.side_effect = [FAILED, DOWNLOADED]

    result = chain.download_torrent(torrent)

    assert result == DOWNLOADED[1:4]
    chain.search_site_torrents.assert_called_once_with(site=SITE, keyword=torrent.title)
    assert helper.download_torrent.call_count == 2
    request = helper.download_torrent.call_args.kwargs
    assert request["url"] == NEW_URL
    assert request["cookie"] == "session=new"
    assert request["ua"] == "NewAgent"
    assert request["proxy"] is True
    assert all(call.kwargs["cache_invalid"] is False for call in helper.download_torrent.call_args_list)
    assert torrent.enclosure == NEW_URL
    chain.post_message.assert_not_called()


def test_signed_link_recovery_is_bounded_and_reports_final_error(download_chain):
    """新链接仍失败时停止恢复，并只通知最终失败。"""
    chain, helper = download_chain
    chain.search_site_torrents.return_value = [_torrent(enclosure=NEW_URL)]
    helper.download_torrent.side_effect = [FAILED, (*FAILED[:4], "下载种子出错，状态码：403")]

    assert chain.download_torrent(_torrent()) == (None, "", [])

    assert helper.download_torrent.call_count == 2
    assert chain.search_site_torrents.call_count == 1
    assert chain.post_message.call_count == 1
    assert "403" in chain.post_message.call_args.args[0].text


@pytest.mark.parametrize("enclosure,error", [
    ("https://tracker.example/download.php?id=39", FAILED[-1]),
    (OLD_URL, "触发站点流控，请稍后重试"),
    (OLD_URL, "无法打开链接"),
    (OLD_URL, "下载种子出错，状态码：500"),
])
def test_recovery_does_not_repeat_ordinary_or_transient_failures(download_chain, enclosure, error):
    """普通链接和流控、网络、服务端错误不触发额外搜索或重试。"""
    chain, helper = download_chain
    helper.download_torrent.return_value = (*FAILED[:4], error)

    assert chain.download_torrent(_torrent(enclosure=enclosure)) == (None, "", [])

    helper.download_torrent.assert_called_once()
    chain.search_site_torrents.assert_not_called()
    assert helper.download_torrent.call_args.kwargs["cache_invalid"] is (enclosure != OLD_URL)


@pytest.mark.parametrize("fresh", [None, OLD_URL])
def test_recovery_requires_new_link_for_exact_resource(download_chain, fresh):
    """无匹配结果或仍为旧链接时不再下载，也不根据标题替换其他种子。"""
    chain, helper = download_chain
    chain.search_site_torrents.return_value = [_torrent(enclosure=fresh)] if fresh else []

    assert chain.download_torrent(_torrent()) == (None, "", [])

    helper.download_torrent.assert_called_once()
    chain.post_message.assert_called_once()


def test_valid_signed_link_does_not_search(download_chain):
    """仍然有效的签名链接只执行原本的一次下载。"""
    chain, helper = download_chain
    helper.download_torrent.return_value = DOWNLOADED

    assert chain.download_torrent(_torrent()) == DOWNLOADED[1:4]

    chain.search_site_torrents.assert_not_called()
    helper.download_torrent.assert_called_once()


def test_indirect_download_failure_does_not_use_signed_link_recovery(download_chain):
    """已有换票流程失败时不再次搜索站点，继续不携带 Cookie 下载临时地址。"""
    chain, helper = download_chain
    chain._resolve_indirect_download_url = Mock(return_value=OLD_URL)

    assert chain.download_torrent(_torrent(enclosure="[encoded]https://tracker.example/token")) == (None, "", [])

    chain.search_site_torrents.assert_not_called()
    helper.download_torrent.assert_called_once()
    assert helper.download_torrent.call_args.kwargs["cache_invalid"] is False
    assert helper.download_torrent.call_args.kwargs["cookie"] is None


@pytest.mark.parametrize("enclosure", [None, "magnet:?xt=urn:btih:abc"])
def test_missing_and_magnet_links_do_not_fetch(download_chain, enclosure):
    """空地址与磁力地址沿用无需请求站点的原有返回契约。"""
    chain, helper = download_chain
    assert chain.download_torrent(_torrent(enclosure=enclosure)) == (enclosure, "", [])
    chain.search_site_torrents.assert_not_called()
    helper.download_torrent.assert_not_called()


@pytest.mark.parametrize("failure", ["exception", "missing_site", "missing_id"])
def test_unavailable_refresh_preserves_original_failure(download_chain, monkeypatch, failure):
    """索引器不可用或身份缺失时保留原失败，不扩大搜索范围。"""
    chain, helper = download_chain
    torrent = _torrent()
    if failure == "exception":
        chain.search_site_torrents.side_effect = RuntimeError("secret signed URL")
    elif failure == "missing_site":
        torrent.site = 2
    else:
        torrent.page_url = None
        torrent.enclosure = "https://tracker.example/download.php?t=100&sign=old"
    warning = Mock()
    monkeypatch.setattr(submission.logger, "warning", warning)

    assert chain.download_torrent(torrent) == (None, "", [])

    helper.download_torrent.assert_called_once()
    assert "404" in chain.post_message.call_args.args[0].text
    assert "secret signed URL" not in str(warning.call_args_list)


@pytest.mark.parametrize("final_status", [200, 404])
def test_real_torrent_parse_and_cooldown_follow_final_attempt(monkeypatch, final_status):
    """回放旧链接 404 与新链接响应，通过真实种子解析验证冷却只发生在最终失败后。"""
    chain = object.__new__(DownloadChain)
    chain.post_message = Mock()
    chain._record_download_failure = Mock()
    chain._apply_resource_download_event = Mock(return_value=(None, None))
    chain._record_music_album_track_keys = Mock()
    chain._resolve_media_download_dir = Mock(return_value=("local", Path("/downloads"), None))
    chain.search_site_torrents = Mock(return_value=[_torrent(enclosure=NEW_URL)])
    sites = Mock()
    sites.get_indexers.return_value = [SITE]
    monkeypatch.setattr(submission, "SitesHelper", Mock(return_value=sites))
    context = Context(torrent_info=_torrent(), media_info=MediaInfo(title="Series"),
                      meta_info=MetaInfo("Series S01E39"))
    media = Mock()
    media.supplement_tmdb_info.return_value = context.media_info
    monkeypatch.setattr(submission, "MediaChain", Mock(return_value=media))
    content = b"d4:infod4:name10:Series.mkv6:lengthi1eee"
    port = Mock()
    port.request.side_effect = [
        SimpleNamespace(status_code=404),
        SimpleNamespace(status_code=final_status, content=content),
    ]
    monkeypatch.setattr(torrent_download, "_require_torrent_port", Mock(return_value=port))
    cache = Mock()
    cache.get.return_value = None
    monkeypatch.setattr(torrent_download, "FileCache", Mock(return_value=cache))
    invalid = Mock()
    monkeypatch.setattr(torrent_download.TorrentHelper, "add_invalid", invalid)

    prepared, error = chain._prepare_download_single(
        context=context, torrent_file=None, torrent_content=None, episodes=None,
        channel=None, source="Subscribe|1", downloader=None, save_path=None,
        userid=None, username=None,
    )

    assert [call.kwargs["url"] for call in port.request.call_args_list] == [OLD_URL, NEW_URL]
    invalid.assert_not_called()
    if final_status == 200:
        assert error is None
        assert prepared.torrent_content == content
        assert prepared.file_list == ["Series.mkv"]
        chain._record_download_failure.assert_not_called()
        chain.post_message.assert_not_called()
        assert cache.set.call_args.args[1] == content
    else:
        assert prepared is None
        assert error == "下载种子内容为空"
        chain._record_download_failure.assert_called_once()
        chain.post_message.assert_called_once()
        cache.set.assert_not_called()


def test_cache_without_stable_id_keeps_legacy_deduplication():
    """没有稳定 ID 时继续按标题去重，但仍更新新的访问参数。"""
    old = _torrent(page_url=None, enclosure="https://tracker.example/old.torrent")
    cached = [Context(torrent_info=old)]
    fresh = replace(old, enclosure="https://tracker.example/new.torrent")
    assert TorrentsChain._refresh_cached_torrents([fresh], cached) == []
    assert cached[0].torrent_info is fresh
    missing = replace(fresh, enclosure=None)
    assert TorrentsChain._refresh_cached_torrents([missing], cached) == []
    assert cached[0].torrent_info is fresh


def test_grouped_tracker_keeps_individual_torrents():
    """GPW、DIC Music 等分组页面必须按 torrentid 区分组内不同资源。"""
    first = _torrent(page_url="https://tracker.example/torrents.php?id=12&torrentid=39")
    cached = [Context(torrent_info=first)]
    updated = replace(first, enclosure=NEW_URL)
    another = replace(first, page_url="https://tracker.example/torrents.php?id=12&torrentid=40",
                      enclosure="https://tracker.example/torrents.php?action=download&id=40")

    assert torrent_rules.resource_identity(first) == ("1", "id=39")
    assert torrent_rules.resource_identity(another) == ("1", "id=40")
    assert TorrentsChain._refresh_cached_torrents([updated, another], cached) == [another]
    assert cached[0].torrent_info is updated


def test_mteam_indirect_request_metadata_can_refresh_in_cache():
    """馒头采用普通缓存时也会更新换票请求，同时保留媒体识别结果。"""
    old = _torrent(page_url="https://kp.m-team.cc/detail/39",
                   enclosure="[old-params]https://api.m-team.cc/api/torrent/genDlToken")
    cached = [Context(torrent_info=old, media_info=MediaInfo(title="识别结果"))]
    fresh = replace(old, enclosure="[new-params]https://api.m-team.cc/api/torrent/genDlToken")

    assert TorrentsChain._refresh_cached_torrents([fresh], cached) == []
    assert cached[0].torrent_info is fresh
    assert cached[0].media_info.title == "识别结果"


def test_mteam_default_refresh_keeps_existing_cache_and_recognition(refresh_chain, monkeypatch):
    """馒头默认增量合并缓存，首页之外的旧资源和已有识别结果不会丢失。"""
    monkeypatch.setattr(settings, "NO_CACHE_SITE_KEY", ConfigModel.model_fields["NO_CACHE_SITE_KEY"].default)
    site = {**SITE, "domain": "https://kp.m-team.cc/"}
    torrents_module.SitesHelper().get_indexers.return_value = [site]
    old = _torrent(page_url="https://kp.m-team.cc/detail/39",
                   enclosure="[old]https://api.m-team.cc/api/torrent/genDlToken")
    cached = Context(torrent_info=old, media_info=MediaInfo(title="已识别"))
    older = Context(torrent_info=replace(old, page_url="https://kp.m-team.cc/detail/40"))
    cache = {"m-team.cc": [older, cached]}
    refresh_chain.load_cache.side_effect = lambda filename: cache if filename == refresh_chain.cache_file else {}
    fresh = replace(old, enclosure="[new]https://api.m-team.cc/api/torrent/genDlToken")
    refresh_chain.rss.return_value = [fresh]

    result = refresh_chain.refresh(stype="spider", sites=[1])

    assert result["m-team.cc"] == [older, cached]
    assert cached.torrent_info is fresh
    assert cached.media_info.title == "已识别"
    refresh_chain._build_refresh_context.assert_not_called()
    assert torrent_rules.resource_identity(old) != torrent_rules.resource_identity(older.torrent_info)


@pytest.mark.parametrize("keys,domain,expected", [
    ("", "m-team.cc", False),
    (" , , ", "tracker.example", False),
    (" m-team, ", "m-team.cc", True),
    (" m-team, ", "tracker.example", False),
])
def test_no_cache_rule_only_matches_explicit_nonempty_keywords(monkeypatch, keys, domain, expected):
    """空项不再匹配所有站点，显式例外规则继续生效。"""
    monkeypatch.setattr(settings, "NO_CACHE_SITE_KEY", keys)
    chain = object.__new__(TorrentsChain)
    assert chain._is_no_cache_site(domain) is expected


def test_cached_mteam_request_obtains_new_token_for_each_download(download_chain, monkeypatch):
    """同一缓存换票请求连续下载时，每次均从接口获取新的临时链接。"""
    chain, helper = download_chain
    request = {"method": "post", "params": {"id": "39"}, "result": "data"}
    encoded = base64.b64encode(json.dumps(request).encode()).decode()
    torrent = _torrent(enclosure=f"[{encoded}]https://api.m-team.cc/api/torrent/genDlToken")
    responses = [Mock(), Mock()]
    responses[0].json.return_value = {"data": "https://api.m-team.cc/download/first"}
    responses[1].json.return_value = {"data": "https://api.m-team.cc/download/second"}
    http = Mock()
    http.post.side_effect = responses
    monkeypatch.setattr(submission, "_download_ports_snapshot", Mock(return_value=(http, Mock())))
    helper.download_torrent.return_value = DOWNLOADED

    assert chain.download_torrent(torrent) == DOWNLOADED[1:4]
    assert chain.download_torrent(torrent) == DOWNLOADED[1:4]

    assert http.post.call_count == 2
    assert [call.kwargs["url"] for call in helper.download_torrent.call_args_list] == [
        "https://api.m-team.cc/download/first", "https://api.m-team.cc/download/second",
    ]
    for response in responses:
        response.close.assert_called_once()
    chain.search_site_torrents.assert_not_called()


@pytest.mark.parametrize("field,prefix", [("torrent_id", "id"), ("info_hash", "hash")])
def test_explicit_torrent_identity_is_stable(field, prefix):
    """索引器提供的显式种子 ID 或 info hash 优先于 URL。"""
    torrent = _torrent()
    setattr(torrent, field, "stable")
    assert torrent_rules.resource_identity(torrent) == ("1", f"{prefix}=stable")


@pytest.mark.parametrize("url,expected", [
    ("https://tracker.example/download?tid=39&t=1&sign=test", ("tracker.example", "id=39")),
    ("https://tracker.example/download?hash=abc", ("tracker.example", "hash=abc")),
    ("https://tracker.example/download?id=39&id=40", None),
    ("https://[invalid", None),
    ("magnet:?xt=urn:btih:abc", None),
    (None, None),
])
def test_resource_identity_requires_unambiguous_site_resource(url, expected):
    """无站点 ID 时域名隔离身份，含糊或无身份的地址不能用于恢复匹配。"""
    torrent = _torrent(site=None, site_name=None, page_url=None, enclosure=url)
    assert torrent_rules.resource_identity(torrent) == expected


@pytest.mark.parametrize("url,expected", [
    (OLD_URL, True),
    ("https://tracker.example/download?id=39&t=1&sign=", False),
    ("https://tracker.example/download?id=39&sign=test", False),
    ("https://[invalid", False),
    ("[encoded]https://tracker.example/api", False),
    (None, False),
])
def test_only_known_timestamp_signature_pattern_is_expiring(url, expected):
    """签名模式判断不把空参数、普通链接或换票协议误认为时效链接。"""
    assert torrent_rules.is_expiring_download_url(url) is expected
