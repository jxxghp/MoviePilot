"""三种媒体、三种 I/O 模式必须使用同一搜索执行及资源证据处理流程。"""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.chain.search import SearchChain, execution, media
from app.domain.context import MediaInfo, MusicInfo, TorrentInfo
from app.domain.meta.metamusic import MetaMusic
from app.schemas.types import MediaSource, MediaType


@pytest.mark.parametrize("mtype", [MediaType.MOVIE, MediaType.TV, MediaType.MUSIC])
@pytest.mark.parametrize("mode", ["sync", "async", "stream"])
def test_all_media_use_filtered_results_to_stop_keyword_search(monkeypatch, mtype, mode):
    """已召回但被过滤的资源不能终止任何媒体类型的后续查询。"""
    chain = SearchChain()
    chain.runtime_config = replace(chain.runtime_config, search_multiple_name=False)
    title = "Example Album" if mtype == MediaType.MUSIC else "Example Movie"
    if mtype == MediaType.MUSIC:
        target = MusicInfo(media_source=MediaSource.MusicBrainz, media_id="album", music_type="album",
                           title=title, artists=["Artist"], year=2024)
        resource_title = "Artist - Example Album (2024) FLAC"
    else:
        target = MediaInfo(media_source=MediaSource.TMDB, media_id="1", tmdb_id=1,
                           title=title, names=[title], type=mtype, year="2024")
        if mtype == MediaType.TV:
            target.season_years = {1: "2024"}
        resource_title = f"Example.Movie.2024{' S01E01' if mtype == MediaType.TV else ''}.1080p"
    calls = []

    def search(**kwargs):
        """首轮未达到过滤要求，第二轮返回相同作品的可用资源。"""
        calls.append(kwargs["keyword"])
        return [TorrentInfo(title=resource_title, category=mtype.value,
                            labels=[] if len(calls) == 1 else ["SITE_ACCEPT"])]

    async def async_search(**kwargs):
        """异步端口复用相同站点数据。"""
        return search(**kwargs)

    async def stream(**kwargs):
        """流式端口复用相同站点数据。"""
        yield {"items": search(**kwargs), "value": 100}

    async def supplement(mediainfo):
        """不在单元测试中访问远端附加信息。"""
        return mediainfo

    async def sleep(_delay):
        """跳过测试中的退避等待。"""

    provider = SimpleNamespace(supplement_media_info=lambda mediainfo: mediainfo,
                               async_supplement_media_info=supplement)
    monkeypatch.setattr(media, "MediaChain", lambda: provider)
    monkeypatch.setattr(execution.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(execution.asyncio, "sleep", sleep)
    chain._prepare_params = lambda **_kwargs: (None, ["first", "second", "third"])
    chain._SearchChain__search_all_sites = search
    chain._SearchChain__async_search_all_sites = async_search
    chain._SearchChain__async_search_all_sites_stream = stream
    params = {"mediainfo": target, "rule_groups": [], "filter_params": {"include": "SITE_ACCEPT"}}
    if mode == "sync":
        contexts = chain.process(**params)
    elif mode == "async":
        contexts = asyncio.run(chain.async_process(**params))
    else:
        async def collect():
            """收集预览和最终上下文，验证未匹配预览不携带目标身份。"""
            return [event async for event in chain.async_process_stream(**params)]

        events = asyncio.run(collect())
        for event in events:
            if event["type"] == "append":
                assert all(item["media_info"] is None for item in event["items"])
        assert events[-1]["candidate_items"] == 2
        contexts = events[-1]["contexts"]
    assert calls == ["first", "second"]
    assert len(contexts) == 1
    assert contexts[0].match_status == "exact"
    assert contexts[0].media_info.media_id == target.media_id
    assert contexts[0].meta_info.media_id is None
    assert isinstance(contexts[0].meta_info, MetaMusic) == (mtype == MediaType.MUSIC)
