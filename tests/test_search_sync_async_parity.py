"""搜索链同步、异步与流式入口的共享业务决策回归测试。"""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest

from app.chain.search import media as media_module
from app.chain.search import title as title_module
from app.chain.search.facade import SearchChain
from app.domain.context import MediaInfo, TorrentInfo
from app.domain.metainfo import MetaInfo
from app.schemas.types import MediaSource, MediaType


def _media() -> MediaInfo:
    """构造具备稳定来源身份和别名的测试媒体。"""
    return MediaInfo(
        media_source=MediaSource.TMDB,
        media_id="100",
        tmdb_id=100,
        title="测试电影",
        names=["Test Movie"],
        type=MediaType.MOVIE,
        year="2026",
    )


def _torrent(title: str) -> TorrentInfo:
    """构造一条无需外部 provider 的测试资源。"""
    return TorrentInfo(title=title, description="description")


def _chain() -> SearchChain:
    """绕过运行时组合根构造可局部注入的搜索链。"""
    return object.__new__(SearchChain)


def test_id_search_sync_async_share_request_and_missing_plan(monkeypatch):
    """ID 搜索双入口应产生完全相同的识别、缓存和缺集参数。"""
    chain = _chain()
    target = _media()
    sync_calls: list[dict[str, Any]] = []
    async_calls: list[dict[str, Any]] = []
    saved_sync: list[dict[str, Any]] = []
    saved_async: list[dict[str, Any]] = []
    result = [SimpleNamespace(name="context")]

    class FakeMediaChain:
        """记录同步与异步识别参数并返回同一媒体快照。"""

        def recognize_media(self, **kwargs):
            """返回同步识别结果。"""
            sync_calls.append(kwargs)
            return target

        async def async_recognize_media(self, **kwargs):
            """返回异步识别结果。"""
            async_calls.append(kwargs)
            return target

    def process(**kwargs):
        """记录同步处理计划。"""
        sync_calls.append(kwargs)
        return result

    async def async_process(**kwargs):
        """记录异步处理计划。"""
        async_calls.append(kwargs)
        return result

    async def async_save_params(**kwargs):
        """记录异步缓存参数。"""
        saved_async.append(kwargs)

    async def async_save_results(_contexts):
        """模拟异步结果缓存。"""

    monkeypatch.setattr(media_module, "MediaChain", FakeMediaChain)
    chain.cancel_ai_recommend = lambda: None
    chain.save_last_search_params = lambda **kwargs: saved_sync.append(kwargs)
    chain.async_save_last_search_params = async_save_params
    chain.process = process
    chain.async_process = async_process
    chain._save_results = lambda _contexts: None
    chain._async_save_results = async_save_results

    kwargs = {
        "media_source": MediaSource.TMDB,
        "media_id": "100",
        "mtype": MediaType.TV,
        "area": "imdbid",
        "season": 2,
        "sites": [1, 3],
        "cache_local": True,
        "music_type": "recording",
    }
    sync_result = chain.search_by_id(**kwargs)
    async_result = asyncio.run(chain.async_search_by_id(**kwargs))

    assert sync_result is result
    assert async_result is result
    assert sync_calls[0] == async_calls[0] == {
        "media_source": MediaSource.TMDB,
        "media_id": "100",
        "mtype": MediaType.TV,
        "music_type": "recording",
    }
    assert sync_calls[1] == async_calls[1]
    assert sync_calls[1]["sites"] == [1, 3]
    assert sync_calls[1]["area"] == "imdbid"
    missing = sync_calls[1]["no_exists"]
    assert list(missing) == ["tmdb:100"]
    assert list(missing["tmdb:100"]) == [2]
    assert saved_sync == saved_async


def test_imdb_search_falls_back_to_media_title_without_imdb_id():
    """IMDb 搜索缺少 IMDb ID 且没有调用方关键字时应请求媒体标题。"""
    target = _media()

    assert SearchChain._torrent_keyword(None, target, "imdbid") == target.title
    assert SearchChain._torrent_keyword("自定义标题", target, "imdbid") == "自定义标题"


def test_media_process_sync_async_share_keyword_stop_decision(monkeypatch):
    """双入口应按相同关键字顺序搜索，并在首个有效结果后同时停止。"""
    chain = _chain()
    target = _media()
    found = _torrent("测试电影 2026 1080p")
    sync_keywords: list[str] = []
    async_keywords: list[str] = []
    sync_sleeps: list[int] = []
    async_sleeps: list[int] = []
    parsed: list[list[TorrentInfo]] = []

    class FakeMediaChain:
        """为同步与异步处理提供等价的附加信息结果。"""

        def supplement_media_info(self, mediainfo):
            """返回同步附加信息。"""
            return mediainfo

        async def async_supplement_media_info(self, mediainfo):
            """返回异步附加信息。"""
            return mediainfo

    def search_all_sites(**kwargs):
        """第二个关键字开始返回资源。"""
        keyword = kwargs["keyword"]
        sync_keywords.append(keyword)
        return [found] if keyword == "second" else []

    async def async_search_all_sites(**kwargs):
        """第二个关键字开始异步返回资源。"""
        keyword = kwargs["keyword"]
        async_keywords.append(keyword)
        return [found] if keyword == "second" else []

    def parse_result(**kwargs):
        """记录进入统一结果处理边界的资源顺序。"""
        parsed.append(list(kwargs["torrents"]))
        return kwargs["torrents"]

    async def fake_async_sleep(delay):
        """记录异步关键字退避但不实际等待。"""
        async_sleeps.append(delay)

    monkeypatch.setattr(media_module, "MediaChain", FakeMediaChain)
    monkeypatch.setattr(media_module.random, "randint", lambda _start, _end: 1)
    monkeypatch.setattr(media_module.time, "sleep", sync_sleeps.append)
    monkeypatch.setattr(media_module.asyncio, "sleep", fake_async_sleep)
    chain.runtime_config = SimpleNamespace(search_multiple_name=False)
    chain._copy_media_input = deepcopy
    chain._prepare_params = lambda **_kwargs: (None, ["first", "second", "third"])
    chain._SearchChain__search_all_sites = search_all_sites
    chain._SearchChain__async_search_all_sites = async_search_all_sites
    chain._parse_result = parse_result

    sync_result = chain.process(target)
    async_result = asyncio.run(chain.async_process(target))

    assert sync_keywords == async_keywords == ["first", "second"]
    assert sync_sleeps == async_sleeps == [1]
    assert parsed == [[found], [found]]
    assert sync_result == async_result == [found]


def test_keyword_resolution_searches_all_names_when_enabled():
    """开启多名称搜索时共享状态机应完整执行并稳定聚合各关键字结果。"""
    first = _torrent("First Result")
    third = _torrent("Third Result")
    expected = {
        "first": [first],
        "second": [],
        "third": [third],
    }
    sync_keywords: list[str] = []
    async_keywords: list[str] = []

    def execute_sync(request):
        """记录同步驱动顺序并返回关键字结果。"""
        sync_keywords.append(request.keyword)
        return expected[request.keyword]

    async def execute_async(request):
        """记录异步驱动顺序并返回关键字结果。"""
        async_keywords.append(request.keyword)
        return expected[request.keyword]

    keywords = ["first", "second", "third"]
    sync_result = media_module._run_keyword_search_sync(
        media_module._keyword_search_resolution(keywords, search_multiple_name=True),
        execute_sync,
    )
    async_result = asyncio.run(
        media_module._run_keyword_search_async(
            media_module._keyword_search_resolution(
                keywords, search_multiple_name=True
            ),
            execute_async,
        )
    )

    assert sync_keywords == async_keywords == keywords
    assert sync_result == async_result
    assert sync_result.torrents == [first, third]
    assert sync_result.stopped_early is False


def test_media_process_stream_shares_keyword_order_and_stop_decision(monkeypatch):
    """流入口应复用双入口的关键字顺序、退避次数和首结果短路决策。"""
    chain = _chain()
    target = _media()
    found = _torrent("测试电影 2026 1080p")
    stream_keywords: list[str] = []
    sleeps: list[int] = []
    parsed: list[list[TorrentInfo]] = []

    class FakeMediaChain:
        """为流处理提供无需外部服务的附加信息结果。"""

        async def async_supplement_media_info(self, mediainfo):
            """返回异步附加信息。"""
            return mediainfo

    async def search_stream(**kwargs):
        """第二个关键字输出资源，供共享状态机执行短路。"""
        keyword = kwargs["keyword"]
        stream_keywords.append(keyword)
        yield {
            "site_id": len(stream_keywords),
            "items": [found] if keyword == "second" else [],
        }

    async def fake_async_sleep(delay):
        """记录流关键字退避但不实际等待。"""
        sleeps.append(delay)

    def parse_result(**kwargs):
        """记录流最终过滤前的资源顺序并返回空上下文。"""
        parsed.append(list(kwargs["torrents"]))
        return []

    async def collect_events():
        """完整消费媒体搜索流。"""
        return [
            event
            async for event in chain.async_process_stream(mediainfo=target)
        ]

    monkeypatch.setattr(media_module, "MediaChain", FakeMediaChain)
    monkeypatch.setattr(media_module.random, "randint", lambda _start, _end: 1)
    monkeypatch.setattr(media_module.asyncio, "sleep", fake_async_sleep)
    chain.runtime_config = SimpleNamespace(search_multiple_name=False)
    chain._copy_media_input = deepcopy
    chain._prepare_params = lambda **_kwargs: (None, ["first", "second", "third"])
    chain._SearchChain__async_search_all_sites_stream = search_stream
    chain._parse_result = parse_result

    events = asyncio.run(collect_events())

    assert stream_keywords == ["first", "second"]
    assert sleeps == [1]
    assert parsed == [[found]]
    assert [event["type"] for event in events] == [
        "append",
        "append",
        "progress",
        "replace",
        "done",
    ]
    assert events[-1]["candidate_items"] == 1


def test_media_process_empty_keyword_plan_skips_all_provider_io(monkeypatch):
    """空关键字计划应由共享状态机直接完成，且三入口均不触发 provider。"""
    chain = _chain()
    target = _media()
    parsed: list[list[TorrentInfo]] = []

    class FakeMediaChain:
        """为三入口提供稳定的媒体附加信息。"""

        def supplement_media_info(self, mediainfo):
            """返回同步附加信息。"""
            return mediainfo

        async def async_supplement_media_info(self, mediainfo):
            """返回异步附加信息。"""
            return mediainfo

    def unexpected_provider(**_kwargs):
        """标记空计划错误触发了同步 provider。"""
        raise AssertionError("空关键字计划不应触发同步 provider")

    async def unexpected_async_provider(**_kwargs):
        """标记空计划错误触发了异步 provider。"""
        raise AssertionError("空关键字计划不应触发异步 provider")

    async def unexpected_stream_provider(**_kwargs):
        """标记空计划错误触发了流式 provider。"""
        yield {"items": []}
        raise AssertionError("空关键字计划不应触发流式 provider")

    def parse_result(**kwargs):
        """记录三入口交付给结果解析边界的空资源。"""
        parsed.append(list(kwargs["torrents"]))
        return []

    async def collect_events():
        """完整消费空关键字媒体搜索流。"""
        return [
            event
            async for event in chain.async_process_stream(mediainfo=target)
        ]

    monkeypatch.setattr(media_module, "MediaChain", FakeMediaChain)
    chain.runtime_config = SimpleNamespace(search_multiple_name=False)
    chain._copy_media_input = deepcopy
    chain._prepare_params = lambda **_kwargs: (None, [])
    chain._SearchChain__search_all_sites = unexpected_provider
    chain._SearchChain__async_search_all_sites = unexpected_async_provider
    chain._SearchChain__async_search_all_sites_stream = unexpected_stream_provider
    chain._parse_result = parse_result

    assert chain.process(target) == []
    assert asyncio.run(chain.async_process(target)) == []
    events = asyncio.run(collect_events())

    assert parsed == [[], [], []]
    assert [event["type"] for event in events] == [
        "progress",
        "replace",
        "done",
    ]
    assert events[-1]["candidate_items"] == 0


def test_id_search_stream_failure_preserves_order_and_skips_result_cache(
    monkeypatch,
):
    """识别失败时流入口应先保存请求状态，只输出错误且不保存空结果。"""
    chain = _chain()
    order: list[str] = []

    class FakeMediaChain:
        """模拟异步识别失败。"""

        async def async_recognize_media(self, **_kwargs):
            """记录识别顺序并返回失败。"""
            order.append("recognize")
            return None

    async def save_params(**_kwargs):
        """记录请求状态保存顺序。"""
        order.append("params")

    async def save_results(_contexts):
        """若失败路径错误保存结果则留下可断言记录。"""
        order.append("results")

    async def collect_events():
        """完整消费识别失败的搜索流。"""
        return [
            event
            async for event in chain.async_search_by_id_stream(
                media_source=MediaSource.TMDB,
                media_id="404",
                cache_local=True,
            )
        ]

    monkeypatch.setattr(media_module, "MediaChain", FakeMediaChain)
    chain.cancel_ai_recommend = lambda: order.append("cancel")
    chain.async_save_last_search_params = save_params
    chain._async_save_results = save_results

    events = asyncio.run(collect_events())

    assert order == ["cancel", "params", "recognize"]
    assert events == [
        {"type": "error", "success": False, "message": "媒体信息识别失败"}
    ]


def test_title_search_sync_async_share_plan_filter_and_projection():
    """标题搜索双入口应共享 provider 参数、过滤决策和可替换元数据投影。"""
    chain = _chain()
    keep = _torrent("Movie 2026 1080p")
    drop = _torrent("Movie 2026 2160p")
    provider_calls: list[dict[str, Any]] = []
    filter_calls: list[list[TorrentInfo]] = []
    meta_calls: list[str] = []

    def search_all_sites(**kwargs):
        """记录同步 provider 参数。"""
        provider_calls.append(kwargs)
        return [keep, drop]

    async def async_search_all_sites(**kwargs):
        """记录异步 provider 参数。"""
        provider_calls.append(kwargs)
        return [keep, drop]

    def filter_torrents(torrents, rule_groups=None):
        """记录共享过滤输入并保留首条资源。"""
        assert rule_groups == ["quality"]
        filter_calls.append(list(torrents))
        return [keep]

    def build_meta(torrent, _mtype):
        """模拟插件或测试替换的稳定元数据构造点。"""
        meta_calls.append(torrent.title)
        return MetaInfo(title=f"mapped:{torrent.title}")

    chain._SearchChain__search_all_sites = search_all_sites
    chain._SearchChain__async_search_all_sites = async_search_all_sites
    chain._filter_title_search_torrents = filter_torrents
    chain._build_title_search_meta = build_meta

    sync_result = chain.search_by_title(
        title="Movie", page=2, sites=[1], mtype=MediaType.MOVIE, rule_groups=["quality"]
    )
    async_result = asyncio.run(
        chain.async_search_by_title(
            title="Movie",
            page=2,
            sites=[1],
            mtype=MediaType.MOVIE,
            rule_groups=["quality"],
        )
    )

    assert provider_calls == [
        {"keyword": "Movie", "sites": [1], "page": 2, "mtype": MediaType.MOVIE},
        {"keyword": "Movie", "sites": [1], "page": 2, "mtype": MediaType.MOVIE},
    ]
    assert filter_calls == [[keep, drop], [keep, drop]]
    assert meta_calls == [keep.title, keep.title]
    assert [context.meta_info.title for context in sync_result] == [
        context.meta_info.title for context in async_result
    ] == [f"mapped:{keep.title}"]


def test_title_search_sync_async_share_empty_result_decisions(monkeypatch):
    """标题双入口应共享无候选与过滤为空的警告，并跳过结果缓存。"""
    chain = _chain()
    candidate = _torrent("Movie 2026 1080p")
    provider_result: list[TorrentInfo] = []
    warnings: list[str] = []
    filter_calls: list[list[TorrentInfo]] = []
    saved_results: list[list[Any]] = []

    def search_all_sites(**_kwargs):
        """返回当前场景的同步候选快照。"""
        return list(provider_result)

    async def async_search_all_sites(**_kwargs):
        """返回当前场景的异步候选快照。"""
        return list(provider_result)

    def filter_torrents(torrents, rule_groups=None):
        """记录过滤输入并模拟候选全部被规则排除。"""
        assert rule_groups == ["quality"]
        filter_calls.append(list(torrents))
        return []

    async def save_params(**_kwargs):
        """模拟异步请求参数缓存。"""

    async def save_results(contexts):
        """记录不应发生的异步结果缓存。"""
        saved_results.append(list(contexts))

    monkeypatch.setattr(title_module.logger, "warning", warnings.append)
    chain._SearchChain__search_all_sites = search_all_sites
    chain._SearchChain__async_search_all_sites = async_search_all_sites
    chain._filter_title_search_torrents = filter_torrents
    chain._build_title_search_meta = lambda torrent, _mtype: MetaInfo(
        title=torrent.title
    )
    chain.cancel_ai_recommend = lambda: None
    chain.save_last_search_params = lambda **_kwargs: None
    chain.async_save_last_search_params = save_params
    chain._save_results = lambda contexts: saved_results.append(list(contexts))
    chain._async_save_results = save_results

    for torrents, warning in (
        ([], "Movie 未搜索到资源"),
        ([candidate], "Movie 没有符合过滤规则的资源"),
    ):
        provider_result[:] = torrents
        warning_count = len(warnings)
        assert chain.search_by_title(
            title="Movie",
            cache_local=True,
            rule_groups=["quality"],
        ) == []
        assert asyncio.run(
            chain.async_search_by_title(
                title="Movie",
                cache_local=True,
                rule_groups=["quality"],
            )
        ) == []
        assert warnings[warning_count:] == [warning, warning]

    assert filter_calls == [[candidate], [candidate]]
    assert saved_results == []


def test_title_stream_preserves_provider_and_result_order():
    """标题流应按 provider 批次顺序追加，并以相同顺序发布累计完成结果。"""
    chain = _chain()
    first = _torrent("Movie 2026 1080p")
    second = _torrent("Movie 2026 2160p")
    provider_params: list[dict[str, Any]] = []

    async def search_stream(**kwargs):
        """按固定顺序输出两个 provider 批次。"""
        provider_params.append(kwargs)
        yield {"type": "append", "site_id": 1, "items": [first]}
        yield {"type": "append", "site_id": 2, "items": [second]}

    async def collect_events():
        """完整消费标题搜索流。"""
        return [
            event
            async for event in chain.async_search_by_title_stream(
                title="Movie", sites=[1, 2], rule_groups=[]
            )
        ]

    chain._SearchChain__async_search_all_sites_stream = search_stream
    chain._filter_title_search_torrents = lambda torrents, rule_groups=None: torrents
    chain._build_title_search_meta = (
        lambda torrent, _mtype: MetaInfo(title=torrent.title)
    )

    events = asyncio.run(collect_events())

    assert provider_params == [
        {"keyword": "Movie", "sites": [1, 2], "page": 0, "mtype": None}
    ]
    assert [event["type"] for event in events] == ["append", "append", "done"]
    assert [event["total_items"] for event in events] == [1, 2, 2]
    assert [
        item["torrent_info"]["title"] for item in events[-1]["items"]
    ] == [first.title, second.title]
    assert events[-1]["candidate_items"] == 2


def test_id_search_resolution_freezes_success_and_failure_effect_order():
    """ID 搜索状态机应唯一决定成功保存顺序与识别失败短路。"""
    recognition_params = {"media_source": MediaSource.TMDB, "media_id": "100"}
    cache_params = {**recognition_params, "sites": [1]}
    contexts = [SimpleNamespace(name="context")]

    success = media_module._id_search_resolution(
        recognition_params=recognition_params,
        cache_params=cache_params,
        season=2,
        sites=[1],
        area="title",
        cache_local=True,
        failure_keyword="tmdb:100",
    )
    assert isinstance(next(success), media_module._IdSearchCacheRequest)
    assert isinstance(success.send(None), media_module._IdSearchRecognizeRequest)
    process_request = success.send(_media())
    assert isinstance(process_request, media_module._IdSearchProcessRequest)
    assert list(process_request.params["no_exists"]["tmdb:100"]) == [2]
    save_request = success.send(contexts)
    assert isinstance(save_request, media_module._IdSearchSaveRequest)
    assert save_request.contexts is contexts
    with pytest.raises(StopIteration) as success_completed:
        success.send(None)
    assert success_completed.value.value.contexts is contexts

    failure = media_module._id_search_resolution(
        recognition_params=recognition_params,
        cache_params=cache_params,
        season=None,
        sites=None,
        area="title",
        cache_local=False,
        failure_keyword="tmdb:100",
    )
    assert isinstance(next(failure), media_module._IdSearchRecognizeRequest)
    with pytest.raises(StopIteration) as failure_completed:
        failure.send(None)
    assert failure_completed.value.value.contexts == []
    assert failure_completed.value.value.warning == "tmdb:100 媒体信息识别失败！"


def test_title_search_resolution_owns_filter_short_circuit_and_save_order():
    """标题状态机应在过滤失败时短路，并仅保存成功投影。"""
    search_params = {"keyword": "Movie", "sites": [1], "page": 0}
    cache_params = {"keyword": "Movie", "area": "title"}
    torrent = _torrent("Movie 2026")
    contexts = [SimpleNamespace(name="context")]

    success = title_module._title_search_resolution(
        title="Movie",
        search_params=search_params,
        cache_params=cache_params,
        cache_local=True,
        mtype=MediaType.MOVIE,
        rule_groups=["quality"],
    )
    assert isinstance(next(success), title_module._TitleSearchCacheRequest)
    assert isinstance(success.send(None), title_module._TitleSearchProviderRequest)
    resolve_request = success.send([torrent])
    assert isinstance(resolve_request, title_module._TitleSearchResolveRequest)
    save_request = success.send(
        title_module._TitleSearchResult(contexts=contexts)
    )
    assert isinstance(save_request, title_module._TitleSearchSaveRequest)
    assert save_request.contexts is contexts
    with pytest.raises(StopIteration) as success_completed:
        success.send(None)
    assert success_completed.value.value.contexts is contexts

    failure = title_module._title_search_resolution(
        title="Movie",
        search_params=search_params,
        cache_params=cache_params,
        cache_local=True,
        mtype=MediaType.MOVIE,
        rule_groups=["quality"],
    )
    next(failure)
    failure.send(None)
    failure.send([torrent])
    with pytest.raises(StopIteration) as completed:
        failure.send(
            title_module._TitleSearchResult(
                contexts=[],
                warning="Movie 没有符合过滤规则的资源",
            )
        )
    assert completed.value.value.contexts == []
