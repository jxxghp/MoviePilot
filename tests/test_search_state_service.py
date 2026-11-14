"""
搜索状态应用服务测试。
"""

import asyncio

from app.application.search.state import SearchStateService
from app.schemas.types import MediaSource, MediaType


def test_search_state_normalizes_identity_and_preserves_cache_contract():
    """
    搜索参数保存后应保留媒体身份和前端原有字段。
    """
    saved = []
    service = SearchStateService(
        save_cache=lambda value, key: saved.append((key, value)),
        load_cache=lambda _key: saved[-1][1],
        async_save_cache=lambda value, key: None,
        async_load_cache=lambda _key: None,
        params_key="params",
        result_key="results",
        subtitle_result_key="subtitles",
    )

    service.save_params(
        keyword="tmdb:123",
        media_source=None,
        media_id=None,
        mtype=MediaType.MOVIE,
        sites=[1, 2],
    )
    assert saved[0] == (
        "params",
        {
            "keyword": "",
            "media_source": str(MediaSource.TMDB),
            "media_id": "123",
            "type": MediaType.MOVIE.value,
            "area": "title",
            "title": "",
            "year": "",
            "season": "",
            "episode": "",
            "sites": "1,2",
            "result_type": "torrent",
        },
    )
    assert service.load_params() == saved[0][1]

    service.save_results(["sync-result"])
    assert saved[1] == ("results", ["sync-result"])


def test_search_state_async_paths_use_injected_ports():
    """
    异步保存和读取必须只使用注入的缓存端口。
    """
    saved = {}

    async def save(value, key):
        """
        记录异步缓存写入。
        """
        saved[key] = value

    async def load(key):
        """
        返回异步缓存内容。
        """
        return saved.get(key)

    service = SearchStateService(
        save_cache=lambda *_args: None,
        load_cache=lambda _key: None,
        async_save_cache=save,
        async_load_cache=load,
        params_key="params",
        result_key="results",
        subtitle_result_key="subtitles",
    )

    asyncio.run(service.async_save_params(keyword="hello"))
    asyncio.run(service.async_save_results(["resource"]))
    asyncio.run(service.async_save_subtitle_results(["subtitle"]))
    assert asyncio.run(service.async_load_params())["keyword"] == "hello"
    assert asyncio.run(service.async_load_results()) == ["resource"]
    assert asyncio.run(service.async_load_subtitle_results()) == ["subtitle"]


def test_scoped_context_is_atomic_and_never_falls_back_to_legacy_results():
    """
    并发用户只读取自己的完整上下文；缺失记录不读取全局参数或结果。
    """
    from app.schemas.search import SearchDiagnosticCounts, SearchDiagnostics, SearchLastContextData

    saved = {"params": {"keyword": "legacy"}, "results": ["unowned"]}
    writes = []

    async def save(value, key):
        """
        每次写入一个完整快照，模拟缓存原子替换。
        """
        await asyncio.sleep(0)
        writes.append(key)
        saved[key] = value

    async def load(key):
        """
        返回独立缓存键，暴露意外访问全局键的实现错误。
        """
        assert key not in {"params", "results"}
        return saved.get(key)

    service = SearchStateService(
        save_cache=lambda *_args: None, load_cache=lambda _key: None,
        async_save_cache=save, async_load_cache=load,
        params_key="params", result_key="results", subtitle_result_key="subtitles",
    )

    async def execute():
        """
        交错发布多个用户和同一用户的快照，所有字段必须保持同一编号。
        """
        assert (await service.async_load_context(3)).model_dump() == {"params": {}, "results": [], "diagnostics": None}
        contexts = [SearchLastContextData(
            params={"keyword": name}, results=[], diagnostics=SearchDiagnostics(
                search_id=name, scope="page", stage_counts=SearchDiagnosticCounts(),
            ),
        ) for name in ("first", "second", "third")]
        await asyncio.gather(service.async_save_context(1, contexts[0]), service.async_save_context(2, contexts[1]))
        assert (await service.async_load_context(1)).diagnostics.search_id == "first"
        assert (await service.async_load_context(2)).params["keyword"] == "second"
        await service.async_save_context(1, contexts[2])
        latest = await service.async_load_context(1)
        assert latest.params["keyword"] == latest.diagnostics.search_id == "third"
        assert (await service.async_load_context(2)).diagnostics.search_id == "second"

    asyncio.run(execute())
    assert writes == ["params_context_1", "params_context_2", "params_context_1"]
