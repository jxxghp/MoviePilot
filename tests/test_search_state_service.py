"""搜索状态应用服务测试。"""

import asyncio

from app.application.search.state import SearchStateService
from app.schemas.types import MediaSource, MediaType


def test_search_state_normalizes_identity_and_preserves_cache_contract():
    """搜索参数保存后应保留媒体身份和前端原有字段。"""
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

    assert saved == [("params", {
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
    })]
    assert service.load_params() == saved[0][1]


def test_search_state_async_paths_use_injected_ports():
    """异步保存和读取必须只使用注入的缓存端口。"""
    saved = {}

    async def save(value, key):
        """记录异步缓存写入。"""
        saved[key] = value

    async def load(key):
        """返回异步缓存内容。"""
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
    assert asyncio.run(service.async_load_params())["keyword"] == "hello"
