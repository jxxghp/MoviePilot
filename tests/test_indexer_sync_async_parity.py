import asyncio

import pytest

import app.modules.indexer as indexer_module
from app.modules.indexer import IndexerModule
from app.schemas.types import MediaType


def _install_search_observers(monkeypatch, events: list[tuple]) -> None:
    """安装只记录业务顺序且不访问外部资源的搜索边界替身"""

    def search_check(site, keyword=None):
        events.append(("check", site["name"], keyword))
        return True

    def clear_search_text(keyword):
        events.append(("normalize", keyword))
        return "clean keyword"

    def statistic(*, site, error_flag=False, seconds=0):
        events.append(("statistic", site["name"], error_flag, seconds))

    async def async_statistic(*, site, error_flag=False, seconds=0):
        events.append(("statistic", site["name"], error_flag, seconds))

    def parse_result(*, site, result_array, seconds):
        events.append(("parse", site["name"], tuple(result_array), seconds))
        return list(result_array)

    def parse_subtitle_result(*, site, result_array, seconds):
        events.append(("parse", site["name"], tuple(result_array), seconds))
        return list(result_array)

    monkeypatch.setattr(
        IndexerModule,
        "_IndexerModule__search_check",
        staticmethod(search_check),
    )
    monkeypatch.setattr(
        IndexerModule,
        "_IndexerModule__clear_search_text",
        staticmethod(clear_search_text),
    )
    monkeypatch.setattr(
        IndexerModule,
        "_IndexerModule__indexer_statistic",
        staticmethod(statistic),
    )
    monkeypatch.setattr(
        IndexerModule,
        "_IndexerModule__async_indexer_statistic",
        staticmethod(async_statistic),
    )
    monkeypatch.setattr(
        IndexerModule,
        "_IndexerModule__parse_result",
        staticmethod(parse_result),
    )
    monkeypatch.setattr(
        IndexerModule,
        "_IndexerModule__parse_subtitle_result",
        staticmethod(parse_subtitle_result),
    )


@pytest.mark.parametrize(
    ("parser_name", "expected_arguments"),
    [
        ("TNodeSpider", {"keyword": "clean keyword", "page": 3}),
        (
            "TorrentLeech",
            {"keyword": "clean keyword", "mtype": MediaType.MOVIE, "page": 3},
        ),
        (
            "mTorrent",
            {"keyword": "clean keyword", "mtype": MediaType.MOVIE, "page": 3},
        ),
        (
            "SunnyPT",
            {
                "keyword": "clean keyword",
                "mtype": MediaType.MOVIE,
                "cat": "movie",
                "page": 3,
            },
        ),
        (
            "Yema",
            {"keyword": "clean keyword", "mtype": MediaType.MOVIE, "page": 3},
        ),
        (
            "Haidan",
            {"keyword": "clean keyword", "mtype": MediaType.MOVIE},
        ),
        (
            "HDDolby",
            {"keyword": "clean keyword", "mtype": MediaType.MOVIE, "page": 3},
        ),
        (
            "RousiPro",
            {
                "keyword": "clean keyword",
                "mtype": MediaType.MOVIE,
                "cat": "movie",
                "page": 3,
            },
        ),
    ],
)
def test_torrent_search_uses_identical_parser_selection_and_arguments(
    monkeypatch,
    parser_name,
    expected_arguments,
):
    """各专用解析器的同步、异步入口必须消费同一参数投影"""
    events = []
    calls = []
    _install_search_observers(monkeypatch, events)

    class FakeSpider:
        """记录专用解析器收到的同步与异步调用"""

        def __init__(self, site):
            self.site = site

        def search(self, **kwargs):
            calls.append(("sync", self.site, kwargs))
            events.append(("io", kwargs))
            return False, [{"title": "hit"}]

        async def async_search(self, **kwargs):
            calls.append(("async", self.site, kwargs))
            events.append(("io", kwargs))
            return False, [{"title": "hit"}]

    monkeypatch.setitem(indexer_module.SPIDER_PARSER_CLASSES, parser_name, FakeSpider)
    site = {"id": 1, "name": "Parity", "parser": parser_name}
    module = object.__new__(IndexerModule)

    sync_result = module.search_torrents(
        site=site,
        keyword="Raw.Keyword",
        mtype=MediaType.MOVIE,
        cat="movie",
        page=3,
    )
    sync_events = list(events)
    events.clear()
    async_result = asyncio.run(module.async_search_torrents(
        site=site,
        keyword="Raw.Keyword",
        mtype=MediaType.MOVIE,
        cat="movie",
        page=3,
    ))

    assert sync_result == async_result == [{"title": "hit"}]
    assert calls == [
        ("sync", site, expected_arguments),
        ("async", site, expected_arguments),
    ]
    assert sync_events == events
    assert [event[0] for event in events] == [
        "check",
        "normalize",
        "io",
        "statistic",
        "parse",
    ]


@pytest.mark.parametrize(
    ("error_flag", "raw_result"),
    [
        (True, []),
        (False, []),
        (False, [{"title": "hit"}]),
    ],
)
def test_torrent_search_preserves_result_and_status_parity(
    monkeypatch,
    error_flag,
    raw_result,
):
    """成功、解析失败和空结果都必须以相同顺序完成统计与结果整理"""
    events = []
    _install_search_observers(monkeypatch, events)

    class FakeSpider:
        """返回参数化状态的专用解析器替身"""

        def __init__(self, _site):
            pass

        def search(self, **_kwargs):
            events.append(("io",))
            return error_flag, list(raw_result)

        async def async_search(self, **_kwargs):
            events.append(("io",))
            return error_flag, list(raw_result)

    monkeypatch.setitem(indexer_module.SPIDER_PARSER_CLASSES, "TNodeSpider", FakeSpider)
    site = {"id": 1, "name": "Parity", "parser": "TNodeSpider"}
    module = object.__new__(IndexerModule)

    sync_result = module.search_torrents(site=site, keyword="Raw.Keyword")
    sync_events = list(events)
    events.clear()
    async_result = asyncio.run(module.async_search_torrents(site=site, keyword="Raw.Keyword"))

    assert sync_result == async_result == raw_result
    assert sync_events == events
    assert events[-2][0:3] == ("statistic", "Parity", error_flag)
    assert events[-1][0:3] == ("parse", "Parity", tuple(raw_result))


def test_torrent_search_preserves_exception_fallback_parity(monkeypatch):
    """解析器抛错后两种入口都沿用空结果与非解析错误统计语义"""
    events = []
    _install_search_observers(monkeypatch, events)

    class FailingSpider:
        """同步与异步均抛出相同操作错误的解析器替身"""

        def __init__(self, _site):
            pass

        def search(self, **_kwargs):
            events.append(("io",))
            raise RuntimeError("broken")

        async def async_search(self, **_kwargs):
            events.append(("io",))
            raise RuntimeError("broken")

    monkeypatch.setitem(indexer_module.SPIDER_PARSER_CLASSES, "TNodeSpider", FailingSpider)
    site = {"id": 1, "name": "Parity", "parser": "TNodeSpider"}
    module = object.__new__(IndexerModule)

    sync_result = module.search_torrents(site=site, keyword="Raw.Keyword")
    sync_events = list(events)
    events.clear()
    async_result = asyncio.run(module.async_search_torrents(site=site, keyword="Raw.Keyword"))

    assert sync_result == async_result == []
    assert sync_events == events
    assert events[-2][0:3] == ("statistic", "Parity", False)
    assert events[-1][0:3] == ("parse", "Parity", ())


def test_generic_torrent_search_uses_identical_request_projection(monkeypatch):
    """普通页面 Spider 的同步、异步入口必须收到同一完整请求"""
    events = []
    calls = []
    _install_search_observers(monkeypatch, events)

    def spider_search(**kwargs):
        calls.append(("sync", kwargs))
        events.append(("io", kwargs))
        return False, [{"title": "generic"}]

    async def async_spider_search(**kwargs):
        calls.append(("async", kwargs))
        events.append(("io", kwargs))
        return False, [{"title": "generic"}]

    monkeypatch.setattr(
        IndexerModule,
        "_IndexerModule__spider_search",
        staticmethod(spider_search),
    )
    monkeypatch.setattr(
        IndexerModule,
        "_IndexerModule__async_spider_search",
        staticmethod(async_spider_search),
    )
    site = {"id": 1, "name": "Generic", "parser": "NexusPhp"}
    module = object.__new__(IndexerModule)

    sync_result = module.search_torrents(
        site=site,
        keyword="Raw.Keyword",
        mtype=MediaType.TV,
        cat="series",
        page=2,
    )
    sync_events = list(events)
    events.clear()
    async_result = asyncio.run(module.async_search_torrents(
        site=site,
        keyword="Raw.Keyword",
        mtype=MediaType.TV,
        cat="series",
        page=2,
    ))

    expected_arguments = {
        "search_word": "clean keyword",
        "indexer": site,
        "mtype": MediaType.TV,
        "cat": "series",
        "page": 2,
    }
    assert sync_result == async_result == [{"title": "generic"}]
    assert calls == [("sync", expected_arguments), ("async", expected_arguments)]
    assert sync_events == events


def test_rejected_torrent_search_skips_normalization_io_and_statistics(monkeypatch):
    """搜索许可拒绝后同步、异步入口都必须立即停止后续工作"""
    events = []

    def search_check(_site, _keyword=None):
        events.append(("check",))
        return False

    monkeypatch.setattr(
        IndexerModule,
        "_IndexerModule__search_check",
        staticmethod(search_check),
    )
    site = {"id": 1, "name": "Rejected", "parser": "TNodeSpider"}
    module = object.__new__(IndexerModule)

    assert module.search_torrents(site=site, keyword="Raw.Keyword") == []
    assert asyncio.run(module.async_search_torrents(site=site, keyword="Raw.Keyword")) == []
    assert events == [("check",), ("check",)]


def test_subtitle_search_uses_one_request_and_preserves_order(monkeypatch):
    """字幕同步、异步入口必须共享许可、参数和状态整理顺序"""
    events = []
    calls = []
    _install_search_observers(monkeypatch, events)

    def spider_search(**kwargs):
        calls.append(("sync", kwargs))
        events.append(("io", kwargs))
        return True, []

    async def async_spider_search(**kwargs):
        calls.append(("async", kwargs))
        events.append(("io", kwargs))
        return True, []

    monkeypatch.setattr(
        IndexerModule,
        "_IndexerModule__spider_search",
        staticmethod(spider_search),
    )
    monkeypatch.setattr(
        IndexerModule,
        "_IndexerModule__async_spider_search",
        staticmethod(async_spider_search),
    )
    site = {"id": 1, "name": "Subtitle", "subtitles": {"search": {}}}
    module = object.__new__(IndexerModule)

    sync_result = module.search_subtitles(site=site, keyword="Raw.Keyword", page=4)
    sync_events = list(events)
    events.clear()
    async_result = asyncio.run(module.async_search_subtitles(
        site=site,
        keyword="Raw.Keyword",
        page=4,
    ))

    expected_arguments = {
        "search_word": "clean keyword",
        "indexer": site,
        "page": 4,
        "search_type": "subtitles",
    }
    assert sync_result == async_result == []
    assert calls == [("sync", expected_arguments), ("async", expected_arguments)]
    assert sync_events == events
    assert [event[0] for event in events] == [
        "check",
        "normalize",
        "io",
        "statistic",
        "parse",
    ]
    assert events[-2][0:3] == ("statistic", "Subtitle", True)


def test_disabled_subtitle_search_skips_all_follow_up_work(monkeypatch):
    """未启用字幕能力时两种入口都不得触发许可检查、I/O 或统计"""
    events = []
    _install_search_observers(monkeypatch, events)
    site = {"id": 1, "name": "Subtitle", "subtitles": None}
    module = object.__new__(IndexerModule)

    assert module.search_subtitles(site=site, keyword="Raw.Keyword") == []
    assert asyncio.run(module.async_search_subtitles(site=site, keyword="Raw.Keyword")) == []
    assert events == []
