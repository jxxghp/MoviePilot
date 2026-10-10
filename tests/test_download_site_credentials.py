"""调用方省略站点凭据时的公共下载入口离线回归。"""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import app.chain.download.submission as submission
from app import schemas
from app.api.endpoints import download as endpoint
from app.application.site.contract import SiteSnapshot
from app.chain.download import DownloadChain
from app.domain.context import MediaInfo, TorrentInfo
from app.domain.metainfo import MetaInfo
from app.schemas.types import MediaType

URL = "https://tracker.example/download.php?id=39"
SITE = SiteSnapshot(id=8, name="测试站", url="https://tracker.example/", cookie="session=configured", ua="SiteAgent")
CONTENT = (None, b"torrent-content", "Movie", ["Movie.mkv"], "")


@pytest.fixture
def boundary(monkeypatch):
    """替换站点查询与种子获取边界，保留真实下载编排。"""
    chain = object.__new__(DownloadChain)
    chain.site_repository = Mock()
    chain.site_repository.get.return_value = SITE
    chain.site_repository.list.return_value = [SITE]
    chain.post_message = Mock()
    helper = Mock()
    helper.download_torrent.return_value = CONTENT
    monkeypatch.setattr(submission, "TorrentHelper", Mock(return_value=helper))
    return chain, helper


@pytest.mark.parametrize("site_id", [8, None])
@pytest.mark.parametrize("cookie", [None, ""])
def test_download_fills_missing_credentials_without_changing_candidate(boundary, site_id, cookie):
    """站点 ID 或唯一同来源配置补齐认证，同时保留候选动态字段与代理选择。"""
    chain, helper = boundary
    torrent = TorrentInfo(site=site_id, title="Movie", enclosure=URL, site_cookie=cookie, site_proxy=True)
    torrent.torrent_id = "39"
    assert chain.download_torrent(torrent) == CONTENT[1:4]
    request = helper.download_torrent.call_args.kwargs
    assert request["cookie"] == SITE.cookie
    assert request["ua"] == SITE.ua
    assert request["proxy"] is True
    assert request["url"] == URL
    assert torrent.site_cookie == cookie
    assert torrent.site_ua is None
    assert torrent.torrent_id == "39"
    if site_id is None:
        chain.site_repository.list.assert_called_once_with()
        chain.site_repository.get.assert_not_called()
    else:
        chain.site_repository.get.assert_called_once_with(8)
        chain.site_repository.list.assert_not_called()


def test_explicit_cookie_keeps_caller_credentials(boundary):
    """调用方显式提供凭据时不查询或覆盖站点配置。"""
    chain, helper = boundary
    torrent = TorrentInfo(site=8, enclosure=URL, site_cookie="session=caller", site_ua="CallerAgent")
    chain.download_torrent(torrent)
    assert helper.download_torrent.call_args.kwargs["cookie"] == "session=caller"
    assert helper.download_torrent.call_args.kwargs["ua"] == "CallerAgent"
    chain.site_repository.get.assert_not_called()
    chain.site_repository.list.assert_not_called()


def test_missing_cookie_preserves_explicit_user_agent(boundary):
    """Cookie 兜底不覆盖调用方显式 UA。"""
    chain, helper = boundary
    chain.download_torrent(TorrentInfo(site=8, enclosure=URL, site_ua="CallerAgent"))
    assert helper.download_torrent.call_args.kwargs["cookie"] == SITE.cookie
    assert helper.download_torrent.call_args.kwargs["ua"] == "CallerAgent"


@pytest.mark.parametrize("url", ["https://TRACKER.EXAMPLE:443/", "https://tracker.example/path/"])
def test_origin_match_normalizes_host_and_default_port(boundary, url):
    """主机大小写、显式默认端口和站点路径不影响同来源匹配。"""
    chain, helper = boundary
    chain.site_repository.get.return_value = replace(SITE, url=url)
    chain.download_torrent(TorrentInfo(site=8, enclosure=URL))
    assert helper.download_torrent.call_args.kwargs["cookie"] == SITE.cookie


@pytest.mark.parametrize("configured_url", [
    "https://other.example/", "https://tracker.example.evil.test/", "https://download.tracker.example/",
    "http://tracker.example/", "https://tracker.example:8443/", "https://user@tracker.example/",
    "https://[invalid", "https://tracker.example:invalid/", "", None,
])
@pytest.mark.parametrize("site_id", [8, None])
def test_credential_fallback_requires_same_origin(boundary, configured_url, site_id):
    """域名、协议、端口或内嵌账号不匹配时不能补发配置凭据。"""
    chain, helper = boundary
    site = replace(SITE, url=configured_url)
    chain.site_repository.get.return_value = site
    chain.site_repository.list.return_value = [site]
    chain.download_torrent(TorrentInfo(site=site_id, enclosure=URL))
    assert helper.download_torrent.call_args.kwargs["cookie"] is None


@pytest.mark.parametrize("sites", [[], [SITE, replace(SITE, id=9)]])
def test_domain_lookup_requires_unique_site(boundary, sites):
    """无 ID 且同来源配置有歧义或未命中时，不猜测使用哪个账号。"""
    chain, helper = boundary
    chain.site_repository.list.return_value = sites
    chain.download_torrent(TorrentInfo(enclosure=URL))
    assert helper.download_torrent.call_args.kwargs["cookie"] is None


@pytest.mark.parametrize("site", [None, replace(SITE, cookie=None), replace(SITE, cookie="")])
def test_missing_site_or_cookie_keeps_existing_download_behavior(boundary, site):
    """未配置 Cookie 的站点继续原有无 Cookie 下载，不改用其他站点账号。"""
    chain, helper = boundary
    chain.site_repository.get.return_value = site
    chain.download_torrent(TorrentInfo(site=8, enclosure=URL))
    assert helper.download_torrent.call_args.kwargs["cookie"] is None
    chain.site_repository.list.assert_not_called()


def test_site_lookup_error_is_logged_without_secret_details(boundary, monkeypatch):
    """兜底查询失败不泄露异常中的凭据，也不阻断原有获取流程。"""
    chain, helper = boundary
    chain.site_repository.get.side_effect = RuntimeError("secret-database-url")
    log = Mock()
    monkeypatch.setattr(submission, "logger", log)
    chain.download_torrent(TorrentInfo(site=8, enclosure=URL))
    assert helper.download_torrent.call_args.kwargs["cookie"] is None
    log.warning.assert_called_once_with("读取种子下载站点凭据失败：RuntimeError")


def test_magnet_does_not_query_site_credentials(boundary):
    """磁力链接直接交给下载器，不读取或携带站点 Cookie。"""
    chain, helper = boundary
    magnet = "magnet:?xt=urn:btih:test"
    assert chain.download_torrent(TorrentInfo(site=8, enclosure=magnet)) == (magnet, "", [])
    chain.site_repository.get.assert_not_called()
    chain.site_repository.list.assert_not_called()
    helper.download_torrent.assert_not_called()


def test_indirect_download_keeps_cookie_free_final_request(boundary):
    """换票请求沿用调用方参数，最终种子获取仍不带 Cookie。"""
    chain, helper = boundary
    chain._resolve_indirect_download_url = Mock(return_value=URL)
    enclosure = "[encoded]https://tracker.example/api"
    assert chain.download_torrent(TorrentInfo(site=8, enclosure=enclosure)) == CONTENT[1:4]
    chain._resolve_indirect_download_url.assert_called_once_with(url=enclosure, ua=None, cookie=None)
    assert helper.download_torrent.call_args.kwargs["cookie"] is None
    chain.site_repository.get.assert_not_called()
    chain.site_repository.list.assert_not_called()


@pytest.mark.parametrize("operation", ["add", "download"])
def test_api_routes_missing_cookie_to_common_download_boundary(boundary, monkeypatch, operation):
    """两种 REST 下载入口的最小种子参数均能在公共获取边界补齐 Cookie。"""
    chain, helper = boundary
    media = MediaInfo(title="Movie", type=MediaType.MOVIE)
    monkeypatch.setattr(endpoint, "DownloadChain", lambda: chain)
    monkeypatch.setattr(endpoint, "_resolve_add_media", lambda *_args: (MetaInfo("Movie"), media, None))

    def submit(context, **_kwargs):
        """在下载器提交边界前执行真实种子获取，避免外部副作用。"""
        content, _, _ = chain.download_torrent(context.torrent_info)
        return "download-39" if content else None

    chain.download_single = submit
    candidate = schemas.TorrentInfo(title="Movie", site=8, enclosure=URL)
    kwargs = {"torrent_in": candidate, "current_user": SimpleNamespace(name="tester")}
    if operation == "download":
        kwargs["media_in"] = schemas.MediaInfo(title="Movie", type=MediaType.MOVIE)
    response = getattr(endpoint, operation)(**kwargs)
    assert response.success is True
    assert response.data["download_id"] == "download-39"
    assert helper.download_torrent.call_args.kwargs["cookie"] == SITE.cookie
    assert candidate.site_cookie is None
