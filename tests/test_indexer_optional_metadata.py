"""
验证站点索引、字幕与缓存适配器对缺失配置及可选后端的处理。
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.application.rss import RssHelper
from app.domain.context import Context, TorrentInfo
from app.modules.indexer import IndexerModule
from app.modules.indexer.spider.hddolby import HddolbySpider
from app.modules.subtitle import SubtitleModule
from app.modules.themoviedb.cache import TmdbCache


@pytest.mark.parametrize("context", [Context(), Context(torrent_info=TorrentInfo(site=1)), Context(torrent_info=TorrentInfo(site=2))])
def test_indexer_incomplete_subtitle_metadata_does_not_request(context, monkeypatch):
    """
    缺少资源、域名或详情页时，索引字幕入口不访问爬虫。
    """
    query = SimpleNamespace(get_sync=Mock(side_effect=[SimpleNamespace(domain=None)] if context.torrent_info and context.torrent_info.site == 1 else [SimpleNamespace(domain="https://site.test")]))
    monkeypatch.setattr("app.modules.indexer.get_configured_site_query_service", Mock(return_value=query))
    helper = SimpleNamespace(get_indexer=Mock(return_value={"parser": "mTorrent"}))
    monkeypatch.setattr("app.modules.indexer.SitesHelper", Mock(return_value=helper))
    spider = Mock(side_effect=AssertionError("详情信息缺失不能查询字幕"))
    monkeypatch.setattr("app.modules.indexer.MTorrentSpider", spider)
    assert object.__new__(IndexerModule).site_subtitle_links(context) is None
    spider.assert_not_called()


def test_hddolby_unconfigured_search_does_not_open_connection(monkeypatch):
    """
    未配置搜索地址时同步和异步入口均返回失败，不创建网络客户端。
    """
    spider = HddolbySpider({})
    request = Mock(side_effect=AssertionError("未配置地址不应发起请求"))
    monkeypatch.setattr("app.modules.indexer.spider.hddolby.RequestUtils", request)
    monkeypatch.setattr("app.modules.indexer.spider.hddolby.AsyncRequestUtils", request)
    assert spider.search("Demo") == (True, [])
    assert asyncio.run(spider.async_search("Demo")) == (True, [])
    request.assert_not_called()


def test_subtitle_page_without_site_domain_still_handles_empty_page(monkeypatch):
    """
    有详情地址但无域名的站点可沿用页面查询，空页面返回空结果而非解析错误。
    """
    query = SimpleNamespace(get_sync=Mock(return_value=SimpleNamespace(domain=None)))
    monkeypatch.setattr("app.modules.subtitle.get_configured_site_query_service", Mock(return_value=query))
    helper = Mock(side_effect=AssertionError("未配置域名不能查询站点索引"))
    monkeypatch.setattr("app.modules.subtitle.SitesHelper", helper)
    request = SimpleNamespace(get_res=Mock(return_value=SimpleNamespace(status_code=200, text="")))
    monkeypatch.setattr("app.modules.subtitle.RequestUtils", Mock(return_value=request))
    context = Context(torrent_info=TorrentInfo(site=1, page_url="https://site.test/details/1"))
    assert object.__new__(SubtitleModule).site_subtitle_links(context) == []
    request.get_res.assert_called_once_with("https://site.test/details/1")
    helper.assert_not_called()


def test_rss_link_without_site_configuration_is_a_readable_error(monkeypatch):
    """
    没有站点及默认 RSS 配置时明确失败，不发送无法确定目标的请求。
    """
    helper = RssHelper()
    monkeypatch.setattr(helper, "rss_link_conf", {})
    http = Mock()
    browser = Mock()
    monkeypatch.setattr("app.application.rss._require_rss_ports", Mock(return_value=(http, browser, Mock())))
    assert helper.get_rss_link("https://site.test", "cookie", "ua") == ("", "获取RSS链接失败：未找到站点配置 https://site.test")
    assert http.mock_calls == []
    assert browser.mock_calls == []


def test_tmdb_cache_without_file_backend_does_not_persist():
    """
    文件后端尚未装配时恢复和保存均停止，保留内存缓存而不访问空对象。
    """
    cache = object.__new__(TmdbCache)
    cache._cache = SimpleNamespace(is_redis=Mock(return_value=False))
    cache._file_cache = None
    cache._legacy_file_cache = None
    assert cache._restore() is None
    assert cache.save(force=True) is None


@pytest.mark.parametrize("value", [{}, [], "invalid", None])
def test_tmdb_cache_invalid_entry_returns_miss(value, monkeypatch):
    """
    空字典与非字典缓存条目均按未命中处理，不把无效类型交给识别调用方。
    """
    cache = object.__new__(TmdbCache)
    cache._cache = SimpleNamespace(get=Mock(return_value=value), is_redis=Mock(return_value=False))
    cache._file_cache = None
    cache._legacy_file_cache = None
    cache._expires_at = {}
    monkeypatch.setattr(cache, "_TmdbCache__get_key", Mock(return_value="demo"))
    assert cache.get(SimpleNamespace()) == {}
