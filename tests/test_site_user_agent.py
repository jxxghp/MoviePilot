"""
站点访问默认 User-Agent 配置与三级优先级回退回归测试。
"""

import base64
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from app.application import rss as rss_module
from app.application.configuration import ChainRuntimeConfig
from app.application.rss import RssHelper, ensure_site_rss
from app.application.site.contract import SiteRepository, SiteSnapshot, SiteWriteResult
from app.chain.download import DownloadChain
from app.chain.site import SiteChain
from app.chain.torrents import TorrentsChain
from app.domain import site as site_rules
from app.domain.context import MediaInfo, SubtitleInfo, TorrentInfo
from app.modules.indexer import IndexerModule
from app.modules.indexer.spider import SiteSpider
from app.modules.indexer.spider.fluxtorrent import FluxTorrentSpider
from app.modules.indexer.spider.mtorrent import MTorrentSpider
from app.runtime.config import Settings
from app.schemas.types import MediaSource, MediaType
from app.startup.composition.configuration import build_chain_runtime_config


@pytest.mark.parametrize(
    ("site_ua", "configured_ua", "default_ua", "expected"),
    [
        ("  Site-Custom/1.0  ", "Global-Site/2.0", "MoviePilot/3.2.11", "Site-Custom/1.0"),
        (None, "  Global-Site/2.0  ", "MoviePilot/3.2.11", "Global-Site/2.0"),
        ("", "Global-Site/2.0", "MoviePilot/3.2.11", "Global-Site/2.0"),
        ("   ", "Global-Site/2.0", "MoviePilot/3.2.11", "Global-Site/2.0"),
        # 兼容旧版站点资源在站点未配置 UA 时自动回填宿主默认标识的场景
        ("MoviePilot/3.2.11", "Global-Site/2.0", "MoviePilot/3.2.11", "Global-Site/2.0"),
        (None, None, "MoviePilot/3.2.11", "MoviePilot/3.2.11"),
        ("", "   ", "MoviePilot/3.2.11", "MoviePilot/3.2.11"),
        ("MoviePilot/3.2.11", None, "MoviePilot/3.2.11", "MoviePilot/3.2.11"),
    ],
)
def test_resolve_site_user_agent_follows_priority_order(
    site_ua: str | None,
    configured_ua: str | None,
    default_ua: str,
    expected: str,
) -> None:
    """
    站点访问 User-Agent 必须严格遵循“站点配置 > 通用配置 > 默认值”。
    """
    assert (
        site_rules.resolve_site_user_agent(site_ua, configured_ua, default_ua)
        == expected
    )


def test_settings_site_user_agent_persists_clears_and_validates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    SITE_USER_AGENT 支持裁剪保存、置空清除以及非法 HTTP 请求头字符拦截，并映射至运行配置快照。
    """
    env_file = tmp_path / "app.env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("SITE_USER_AGENT", raising=False)
    monkeypatch.setattr("app.runtime.config.get_env_path", lambda: env_file)

    instance = Settings(_env_file=env_file, _secrets_dir=None)
    assert instance.SITE_USER_AGENT is None

    updated, _message = instance.update_setting(
        "SITE_USER_AGENT",
        "  Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/138.0.0.0  ",
    )
    assert updated is True
    assert (
        instance.SITE_USER_AGENT
        == "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/138.0.0.0"
    )
    chain_config = build_chain_runtime_config(instance)
    assert chain_config.site_user_agent == instance.SITE_USER_AGENT

    invalid_ok, invalid_msg = instance.update_setting(
        "SITE_USER_AGENT",
        "Mozilla/5.0\r\nInjected: 1",
    )
    assert invalid_ok is False
    assert "非法字符" in invalid_msg

    non_latin1_ok, non_latin1_msg = instance.update_setting(
        "SITE_USER_AGENT",
        "浏览器标识",
    )
    assert non_latin1_ok is False
    assert "非法字符" in non_latin1_msg

    cleared, _clear_msg = instance.update_setting("SITE_USER_AGENT", "   ")
    assert cleared is True
    assert instance.SITE_USER_AGENT is None
    assert "SITE_USER_AGENT" not in env_file.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("site_ua", "configured_ua", "expected_ua"),
    [
        ("Site-UA/1.0", "Global-Site-UA/1.0", "Site-UA/1.0"),
        (None, "Global-Site-UA/1.0", "Global-Site-UA/1.0"),
        ("", None, "Default-MP-UA/3.0"),
    ],
)
def test_site_chain_connectivity_and_icon_use_priority_user_agent(
    monkeypatch: pytest.MonkeyPatch,
    site_ua: str | None,
    configured_ua: str | None,
    expected_ua: str,
) -> None:
    """
    站点通用连通性测试、图标抓取与 CookieCloud 新站点探测均按三级优先级传递 User-Agent。
    """
    captured_uas: list[str | None] = []

    def fake_open_response(**kwargs):
        """
        记录每次站点 HTTP 请求携带的 User-Agent 并返回已登录页面响应。
        """
        captured_uas.append(kwargs.get("ua"))
        response = Mock(
            status_code=200,
            reason="OK",
            text='<html><head><link rel="icon" href="/fav.ico"></head><body><a href="logout.php">退出</a></body></html>',
            content=b"icon",
        )
        response.__bool__ = Mock(return_value=True)
        context = MagicMock()
        context.__enter__.return_value = response
        return context

    monkeypatch.setattr("app.chain.site._open_site_response", fake_open_response)
    runtime_cfg = ChainRuntimeConfig(
        media_extensions=(".mkv",),
        user_agent="Default-MP-UA/3.0",
        site_user_agent=configured_ua,
    )
    monkeypatch.setattr(
        SiteChain,
        "runtime_config",
        property(lambda _self: runtime_cfg),
    )

    site_snapshot = SiteSnapshot(
        id=1,
        name="示例站点",
        domain="pt.example",
        url="https://pt.example/",
        cookie="uid=1",
        ua=site_ua,
        public=0,
        render=0,
    )
    chain = SiteChain()
    repository = Mock()
    repository.get_by_domain.return_value = site_snapshot
    chain.site_repository = repository
    monkeypatch.setattr(
        "app.chain.site.SitesHelper",
        Mock(
            return_value=Mock(
                get_indexer=Mock(
                    return_value={"name": "示例站点", "domain": "https://pt.example/"}
                )
            )
        ),
    )
    monkeypatch.setattr(
        chain,
        "run_module",
        Mock(return_value=SimpleNamespace(supported=False, success=False, message="")),
    )

    ok, msg = chain.test("https://pt.example/")
    assert (ok, msg) == (True, "连接成功")
    assert captured_uas == [expected_ua]

    captured_uas.clear()
    chain.cache_site_icon(SimpleNamespace(event_data={"domain": "pt.example"}))
    assert captured_uas == [expected_ua, expected_ua]

    captured_uas.clear()
    used_proxy, probe_res = chain._cookiecloud_connect(
        "https://pt.example/login.php",
        "uid=1",
        {"name": "示例站点", "ua": site_ua},
    )
    assert used_proxy is False
    assert probe_res is not None
    assert captured_uas == [expected_ua]


def test_rss_generation_and_torrents_chain_respect_site_user_agent_priority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    RSS 自动生成、订阅解析以及变更通用 UA 后的退避重置均遵循三级优先级。
    """
    clock = SimpleNamespace(now=0.0)
    owner = rss_module._RssLinkRecovery(clock=lambda: clock.now)
    monkeypatch.setattr(rss_module, "_rss_link_recovery", owner)

    state = {"site_ua": None, "configured_ua": "Global-RSS-UA/1.0"}
    monkeypatch.setattr(
        rss_module,
        "get_chain_runtime_config_snapshot",
        lambda: SimpleNamespace(
            user_agent="Default-MP-UA/3.0",
            site_user_agent=state["configured_ua"],
            proxy=None,
            proxy_server=None,
        ),
    )
    monkeypatch.setattr(
        "app.chain.torrents.get_chain_runtime_config_snapshot",
        lambda: SimpleNamespace(
            user_agent="Default-MP-UA/3.0",
            site_user_agent=state["configured_ua"],
        ),
    )

    site_snapshot = SiteSnapshot(
        id=10,
        name="RSS站点",
        domain="rss.example",
        url="https://rss.example/",
        rss=None,
        cookie="c=1",
        ua=None,
        is_active=True,
        public=0,
    )
    repository = Mock(spec=SiteRepository)
    repository.get_by_domain.side_effect = lambda _domain: replace(
        site_snapshot, ua=state["site_ua"]
    )
    repository.update_rss_if_unchanged.return_value = SiteWriteResult(True)

    helper = Mock(spec=RssHelper)
    helper.get_rss_link.return_value = ("", "模拟被拦截")
    failed = ensure_site_rss("rss.example", repository, helper=helper)
    assert failed.url is None
    assert helper.get_rss_link.call_args.kwargs["ua"] == "Global-RSS-UA/1.0"

    # 未到期时修改通用站点默认 UA，摘要应改变并立即允许重新生成而无需等待 5 分钟退避
    state["configured_ua"] = "Updated-Global-RSS-UA/2.0"
    helper.get_rss_link.reset_mock()
    helper.get_rss_link.return_value = ("https://rss.example/rss.xml?passkey=ok", "")
    helper.parse.return_value = [{"title": "Demo.2026.1080p", "enclosure": "https://rss.example/1.torrent"}]
    recovered = ensure_site_rss("rss.example", repository, helper=helper)
    assert recovered.url == "https://rss.example/rss.xml?passkey=ok"
    assert helper.get_rss_link.call_args.kwargs["ua"] == "Updated-Global-RSS-UA/2.0"
    assert helper.parse.call_args.kwargs["ua"] == "Updated-Global-RSS-UA/2.0"

    parse_mock = Mock(return_value=[])
    monkeypatch.setattr("app.chain.torrents.RssHelper.parse", parse_mock)
    TorrentsChain._parse_rss_url({"ua": "Site-Explicit-UA"}, "https://rss.example/rss.xml")
    assert parse_mock.call_args.kwargs["ua"] == "Site-Explicit-UA"
    parse_mock.reset_mock()
    TorrentsChain._parse_rss_url({"ua": "Default-MP-UA/3.0"}, "https://rss.example/rss.xml")
    assert parse_mock.call_args.kwargs["ua"] == "Updated-Global-RSS-UA/2.0"


def test_download_chain_torrent_and_subtitle_use_priority_user_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    种子直接下载、间接换票下载与字幕下载在候选未携带自定义 UA 时回退通用站点默认 UA。
    """
    runtime_cfg = ChainRuntimeConfig(
        media_extensions=(".mkv",),
        user_agent="Default-MP-UA/3.0",
        site_user_agent="Global-Download-UA/1.0",
    )
    monkeypatch.setattr(
        DownloadChain,
        "runtime_config",
        property(lambda _self: runtime_cfg),
    )
    monkeypatch.setattr(
        "app.chain.download.submission.get_chain_runtime_config_snapshot",
        lambda: runtime_cfg,
    )

    chain = object.__new__(DownloadChain)
    chain.site_repository = Mock()
    chain.site_repository.get.return_value = None
    chain.site_repository.list.return_value = []
    chain.post_message = Mock()

    helper = Mock()
    helper.is_invalid.return_value = False
    helper.download_torrent.return_value = (None, b"torrent-bytes", "Folder", ["a.mkv"], "")
    monkeypatch.setattr(
        "app.chain.download.submission.TorrentHelper",
        Mock(return_value=helper),
    )

    chain.download_torrent(
        TorrentInfo(site=1, title="Movie", enclosure="https://pt.example/dl/1.torrent")
    )
    assert helper.download_torrent.call_args.kwargs["ua"] == "Global-Download-UA/1.0"

    # 换票请求同样应按三级优先级携带 UA
    http_port = Mock()
    ticket_resp = Mock(status_code=200, text='{"data":"https://pt.example/signed.torrent"}', content=b"1")
    ticket_resp.json.return_value = {"data": "https://pt.example/signed.torrent"}
    http_port.post.return_value = ticket_resp
    monkeypatch.setattr(
        "app.chain.download.submission._download_ports_snapshot",
        lambda: (http_port, Mock()),
    )
    monkeypatch.setattr("app.chain.download.submission.wait_for_site_request", lambda *_args: None)
    monkeypatch.setattr(
        chain,
        "run_module",
        Mock(return_value="https://pt.example/signed.torrent"),
    )
    encoded = base64.b64encode(json.dumps({"method": "post"}).encode("utf-8")).decode("utf-8")
    resolved_url = chain._resolve_indirect_download_url(
        f"[{encoded}]https://pt.example/api/dl",
        ua=None,
        site_id=1,
    )
    assert resolved_url == "https://pt.example/signed.torrent"
    assert http_port.post.call_args.kwargs["ua"] == "Global-Download-UA/1.0"

    # 字幕下载验证
    sub_http = Mock()
    sub_resp = Mock(status_code=200, content=b"1\n00:00:01,000 --> 00:00:02,000\nhi\n")
    sub_http.get.return_value = sub_resp
    monkeypatch.setattr(
        "app.chain.download.subtitle._download_ports_snapshot",
        lambda: (sub_http, Mock()),
    )
    media_info = MediaInfo(type=MediaType.MOVIE, title="Demo", year="2026")
    media_chain = Mock(
        recognize_media=Mock(return_value=media_info),
        supplement_tmdb_info=Mock(return_value=media_info),
    )
    monkeypatch.setattr(
        "app.chain.download.subtitle.MediaChain",
        Mock(return_value=media_chain),
    )
    monkeypatch.setattr(
        chain,
        "_resolve_media_download_dir",
        Mock(return_value=(Mock(), Path("/tmp/demo"), None)),
    )
    monkeypatch.setattr(
        chain,
        "_save_subtitle_response",
        Mock(return_value=(True, "ok", ["/tmp/demo/a.srt"])),
    )
    ok, _msg, files = chain.download_subtitle(
        SubtitleInfo(enclosure="https://pt.example/sub/1.srt", site_ua=None),
        MediaSource.TMDB,
        "123",
    )
    assert ok is True
    assert files == ["/tmp/demo/a.srt"]
    assert sub_http.get.call_args.kwargs["ua"] == "Global-Download-UA/1.0"


def test_indexer_spiders_and_userdata_follow_priority_user_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    通用爬虫、专站爬虫、搜索结果与站点用户数据解析器均遵循三级优先级。
    """
    settings_map: dict[str, str | None] = {
        "USER_AGENT": "MoviePilot/3.2.11",
        "SITE_USER_AGENT": "Global-Browser-UA/1.0",
        "NORMAL_USER_AGENT": "Builtin-Normal-UA/1.0",
        "PROXY": None,
    }
    getter = settings_map.__getitem__

    assert site_rules.resolve_site_user_agent_from_settings(None, getter) == "Global-Browser-UA/1.0"
    assert site_rules.resolve_site_user_agent_from_settings("MoviePilot/3.2.11", getter) == "Global-Browser-UA/1.0"
    assert site_rules.resolve_site_user_agent_from_settings("Site-Explicit-UA", getter) == "Site-Explicit-UA"

    monkeypatch.setattr("app.modules.indexer.get_runtime_setting", getter)
    monkeypatch.setattr("app.modules.indexer.spider.get_runtime_setting", getter)
    monkeypatch.setattr("app.modules.indexer.spider.fluxtorrent.get_runtime_setting", getter)
    monkeypatch.setattr("app.modules.indexer.spider.mtorrent.get_runtime_setting", getter)

    spider = SiteSpider({"domain": "https://pt.example/", "ua": "MoviePilot/3.2.11"})
    assert spider.ua == "Global-Browser-UA/1.0"

    mt_spider = MTorrentSpider({"domain": "https://kp.m-team.cc/", "ua": None})
    assert mt_spider._ua == "Global-Browser-UA/1.0"

    # FluxTorrent：站点配置 > 通用站点配置 > 专站浏览器默认值
    assert FluxTorrentSpider.get_user_agent("Site-Flux-UA") == "Site-Flux-UA"
    assert FluxTorrentSpider.get_user_agent(None) == "Global-Browser-UA/1.0"
    assert FluxTorrentSpider.get_user_agent("MoviePilot/3.2.11") == "Global-Browser-UA/1.0"
    settings_map["SITE_USER_AGENT"] = None
    assert FluxTorrentSpider.get_user_agent("MoviePilot/3.2.11") == "Builtin-Normal-UA/1.0"

    settings_map["SITE_USER_AGENT"] = "Global-Browser-UA/1.0"
    torrents = IndexerModule._IndexerModule__parse_result(
        {"id": 1, "name": "PT", "ua": "MoviePilot/3.2.11"},
        [{"title": "Demo.2026", "enclosure": "https://pt.example/1.torrent"}],
        seconds=1,
    )
    assert torrents[0].site_ua == "Global-Browser-UA/1.0"

    subtitles = IndexerModule._IndexerModule__parse_subtitle_result(
        {"id": 1, "name": "PT", "ua": None},
        [{"title": "Demo.srt", "enclosure": "https://pt.example/1.srt", "language": "zh", "size": 100}],
        seconds=1,
    )
    assert subtitles[0].site_ua == "Global-Browser-UA/1.0"
