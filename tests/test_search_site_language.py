"""多站点媒体搜索必须在提前停止前向英文站点提交可用关键词。"""

import asyncio
from concurrent.futures import Future
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.chain.search import media as media_module
from app.chain.search import provider as provider_module
from app.chain.search.facade import SearchChain
from app.domain.context import Context, MediaInfo, TorrentInfo
from app.schemas.types import MediaType


def _assert_english_site_is_searched_before_chinese_match_stops_search(
    monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    """中文站点首轮命中时，英文站点及其续页仍应使用英文名完成同轮查询。"""
    chain = object.__new__(SearchChain)
    chain.runtime_config = SimpleNamespace(search_threadpool_size=2, search_multiple_name=False)
    media = MediaInfo(tmdb_id=157336, title="星际穿越", en_title="Interstellar",
                      names=["星际穿越", "Interstellar"], type=MediaType.MOVIE)
    sites = [{"id": 1, "name": "中文站"}, {"id": 2, "name": "FileList", "language": "en"}]
    requests: list[tuple[int, str, int]] = []
    continuations: list[tuple[int, str]] = []

    def submit(function: Any, *args: Any, **kwargs: Any) -> Future[Any]:
        """就地执行同步任务，避免测试引入真实线程池生命周期。"""
        future: Future[Any] = Future()
        future.set_result(function(*args, **kwargs))
        return future

    def search_site(*, site: dict[str, Any], keyword: str, page: int, **_kwargs: Any) -> list[TorrentInfo]:
        """模拟英文站拒绝中文、两站第一页各返回一条可匹配资源。"""
        requests.append((site["id"], keyword, page))
        if page or (site["id"] == 2 and keyword != "Interstellar"):
            return []
        return [TorrentInfo(site=site["id"], title="Interstellar 2014 1080p",
                            enclosure=f"https://site{site['id']}.example/download?id=1")]

    def should_continue(*, site: dict[str, Any], keyword: str, page_results: list[TorrentInfo]) -> bool:
        """记录分页判定使用的关键词，确保它与实际请求一致。"""
        continuations.append((site["id"], keyword))
        return bool(page_results)

    def parse_result(*, torrents: list[TorrentInfo], **_kwargs: Any) -> list[Context]:
        """把两站资源标记为已匹配，触发首轮结束后的提前停止。"""
        return [Context(torrent_info=torrent, media_info=media) for torrent in torrents]

    media_chain = SimpleNamespace(
        supplement_media_info=lambda **kwargs: kwargs["mediainfo"],
        async_supplement_media_info=AsyncMock(side_effect=lambda **kwargs: kwargs["mediainfo"]),
    )
    monkeypatch.setattr(media_module, "MediaChain", lambda: media_chain)
    monkeypatch.setattr(provider_module, "ThreadHelper", lambda: SimpleNamespace(submit=submit))
    monkeypatch.setattr(provider_module, "ProgressHelper", MagicMock())
    monkeypatch.setattr(provider_module, "AsyncProgressHelper", lambda *_args: SimpleNamespace(
        start=AsyncMock(), update=AsyncMock(), end=AsyncMock(),
    ))
    chain._sync_indexers = lambda _sites: sites
    chain._async_indexers = AsyncMock(return_value=sites)
    chain._prepare_params = lambda **_kwargs: (None, ["星际穿越", "Interstellar"])
    chain._build_search_pages = lambda _page: [0, 1, 2]
    chain._should_continue_search_pages = should_continue
    chain.search_plugin_torrents = MagicMock(return_value=[])
    chain.async_search_plugin_torrents = AsyncMock(return_value=[])
    chain.search_site_torrents = search_site
    chain.async_search_site_torrents = AsyncMock(side_effect=search_site)
    chain._parse_result = parse_result

    async def stream_results() -> list[dict[str, Any]]:
        """收集流式入口最终结果，确保中间预览不影响提前停止判断。"""
        return [event async for event in chain.async_process_stream(mediainfo=media, sites=[1, 2])]

    if mode == "sync":
        contexts = chain.process(mediainfo=media, sites=[1, 2])
        assert {context.torrent_info.site for context in contexts} == {1, 2}
        chain.search_plugin_torrents.assert_called_once()
    elif mode == "async":
        contexts = asyncio.run(chain.async_process(mediainfo=media, sites=[1, 2]))
        assert {context.torrent_info.site for context in contexts} == {1, 2}
        chain.async_search_plugin_torrents.assert_awaited_once()
    else:
        events = asyncio.run(stream_results())
        replacement = next(event for event in events if event.get("type") == "replace")
        assert {item["torrent_info"]["site"] for item in replacement["items"]} == {1, 2}
        chain.async_search_plugin_torrents.assert_awaited_once()

    assert sorted(requests) == [(1, "星际穿越", 0), (1, "星际穿越", 1),
                                (2, "Interstellar", 0), (2, "Interstellar", 1)]
    assert set(continuations) == {(1, "星际穿越"), (2, "Interstellar")}


def test_sync_english_site_is_searched_before_early_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    """同步搜索必须先完成英文站点查询再决定是否停止换词。"""
    _assert_english_site_is_searched_before_chinese_match_stops_search(monkeypatch, "sync")


def test_async_english_site_is_searched_before_early_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    """异步搜索必须先完成英文站点查询再决定是否停止换词。"""
    _assert_english_site_is_searched_before_chinese_match_stops_search(monkeypatch, "async")


def test_stream_english_site_is_searched_before_early_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    """流式搜索必须保留英文站点资源和一致的最终过滤结果。"""
    _assert_english_site_is_searched_before_chinese_match_stops_search(monkeypatch, "stream")


def test_site_language_fallback_preserves_search_boundaries() -> None:
    """仅为拒绝中文的站点替换媒体名称，IMDb、英文原词和无媒体输入保持原样。"""
    media = MediaInfo(en_title="Interstellar", original_title="Original title", names=["Alias"])
    english = {"language": "en"}
    assert provider_module._site_keyword(english, "星际穿越", media) == "Interstellar"
    assert provider_module._site_keyword({}, "星际穿越", media) == "星际穿越"
    assert provider_module._site_keyword(english, "Interstellar S02", media) == "Interstellar S02"
    assert provider_module._site_keyword(english, "tt0816692", media) == "tt0816692"
    assert provider_module._site_keyword(english, "星际穿越", None) == "星际穿越"
    assert provider_module._site_keyword(english, "", media) == ""
    media.en_title = "中文"
    assert provider_module._site_keyword(english, "星际穿越", media) == "Original title"
    media.original_title = "中文原名"
    assert provider_module._site_keyword(english, "星际穿越", media) == "Alias"
    media.names = ["中文别名"]
    assert provider_module._site_keyword(english, "星际穿越", media) == "星际穿越"
