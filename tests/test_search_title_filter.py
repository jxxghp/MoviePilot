import asyncio
import importlib.machinery
from types import SimpleNamespace

from app.testing.bootstrap import ensure_optional_stub

ensure_optional_stub("qbittorrentapi", TorrentFilesList=list)
ensure_optional_stub("transmission_rpc", File=object)
ensure_optional_stub("psutil", __spec__=importlib.machinery.ModuleSpec("psutil", loader=None))

from app.chain import search as search_module
from app.chain.search import SearchChain
from app.core.context import TorrentInfo
from app.schemas.types import SystemConfigKey


def _make_chain() -> SearchChain:
    """
    构造不触发外部依赖初始化的搜索链实例。
    """
    chain = object.__new__(SearchChain)
    chain.cancel_ai_recommend = lambda: None
    chain.save_last_search_params = lambda **_kwargs: None
    chain.save_cache = lambda _cache, _filename: None
    chain.async_save_last_search_params = lambda **_kwargs: None
    chain.async_save_cache = lambda _cache, _filename: None
    return chain


def _patch_search_filter_rule_groups(monkeypatch, rule_groups: list[str]) -> None:
    """
    固定搜索默认过滤规则组，避免读取真实系统配置。
    """
    oper = SimpleNamespace(
        get=lambda key: rule_groups if key == SystemConfigKey.SearchFilterRuleGroups else None
    )
    monkeypatch.setattr(search_module, "SystemConfigOper", lambda: oper)


def test_search_by_title_applies_default_search_filter_rule_groups(monkeypatch):
    """
    标题搜索应在组装上下文前应用默认搜索过滤规则。
    """
    chain = _make_chain()
    keep = TorrentInfo(title="Movie 2026 1080p WEB-DL", description="")
    drop = TorrentInfo(title="Movie 2026 2160p REMUX", description="")
    filter_calls = []

    chain._SearchChain__search_all_sites = lambda **_kwargs: [keep, drop]

    def filter_torrents(**kwargs):
        """
        记录过滤参数并模拟排除 REMUX 资源。
        """
        filter_calls.append(kwargs)
        return [keep]

    chain.filter_torrents = filter_torrents
    _patch_search_filter_rule_groups(monkeypatch, ["exclude-remux"])

    contexts = chain.search_by_title(title="Movie")

    assert [context.torrent_info for context in contexts] == [keep]
    assert len(filter_calls) == 1
    assert filter_calls[0]["rule_groups"] == ["exclude-remux"]
    assert filter_calls[0]["torrent_list"] == [keep, drop]
    assert filter_calls[0]["mediainfo"] is None


def test_async_search_by_title_stream_filters_batches_before_yield(monkeypatch):
    """
    标题搜索流应只向前端输出过滤后的批次和最终结果。
    """
    chain = _make_chain()
    keep = TorrentInfo(title="Movie 2026 1080p WEB-DL", description="")
    drop = TorrentInfo(title="Movie 2026 2160p REMUX", description="")
    filter_calls = []

    async def search_stream(**_kwargs):
        """
        模拟站点页完成后返回一批混合资源。
        """
        yield {
            "type": "append",
            "stage": "searching",
            "value": 100,
            "text": "done",
            "items": [keep, drop],
            "site": "测试站点",
            "site_id": 1,
            "page": 0,
            "finished": 1,
            "total": 1,
            "total_items": 2,
        }

    def filter_torrents(**kwargs):
        """
        记录过滤参数并模拟排除 REMUX 资源。
        """
        filter_calls.append(kwargs)
        return [keep]

    async def collect_events():
        """
        收集标题搜索流全部事件。
        """
        return [
            event
            async for event in chain.async_search_by_title_stream(title="Movie")
        ]

    chain._SearchChain__async_search_all_sites_stream = search_stream
    chain.filter_torrents = filter_torrents
    _patch_search_filter_rule_groups(monkeypatch, ["exclude-remux"])

    events = asyncio.run(collect_events())

    append_event = events[0]
    done_event = events[-1]
    assert append_event["type"] == "append"
    assert append_event["total_items"] == 1
    assert [item["torrent_info"]["title"] for item in append_event["items"]] == [keep.title]
    assert done_event["type"] == "done"
    assert done_event["total_items"] == 1
    assert [item["torrent_info"]["title"] for item in done_event["items"]] == [keep.title]
    assert len(filter_calls) == 1
    assert filter_calls[0]["rule_groups"] == ["exclude-remux"]
    assert filter_calls[0]["torrent_list"] == [keep, drop]
    assert filter_calls[0]["mediainfo"] is None
