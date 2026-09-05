from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app import schemas
from app.api.endpoints import media as media_endpoint
from app.api.endpoints import mediaserver as mediaserver_endpoint
from app.api.response import ResponseAPIRouter
from app.domain.context import MediaInfo as CoreMediaInfo
from app.domain.context import MusicInfo as CoreMusicInfo
from app.schemas.types import MediaSource, MediaType


@pytest.mark.asyncio
async def test_media_search_response_preserves_core_collection_fields() -> None:
    """媒体搜索响应模型应保留 Core MediaInfo 合集输出的全部兼容字段。"""
    media = CoreMediaInfo(tmdb_info={
        "id": 42,
        "media_type": MediaType.COLLECTION,
        "collection_id": 42,
        "name": "示例合集",
        "original_name": "Example Collection",
    })
    media.hk_title = "香港标题"
    media.tw_title = "台灣標題"
    media.sg_title = "新加坡标题"
    media.logo_path = "/logo.png"
    media.content_rating = "PG-13"
    media.season_years = {1: "2024"}
    payload = media.to_dict()
    assert payload["media_source"] == MediaSource.TMDB.value

    router = ResponseAPIRouter()

    @router.get("/media/search", response_model=schemas.MediaSearchResults)
    def search_media() -> list[dict]:
        """返回代表性的 Core MediaInfo 合集序列化结果。"""
        return [payload]

    app = FastAPI()
    app.include_router(router)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/media/search")

    assert response.status_code == 200
    result = response.json()["data"][0]
    assert result["hk_title"] == "香港标题"
    assert result["tw_title"] == "台灣標題"
    assert result["sg_title"] == "新加坡标题"
    assert result["logo_path"] == "/logo.png"
    assert result["content_rating"] == "PG-13"
    assert result["season_years"] == {"1": "2024"}
    assert "tmdb_info" in result
    assert "douban_info" in result
    assert "bangumi_info" in result
    assert "anilist_info" in result


@pytest.mark.asyncio
async def test_media_response_accepts_cross_source_credit_shapes() -> None:
    """媒体响应应同时保留豆瓣姓名字符串和 TMDB 演职员对象。"""
    router = ResponseAPIRouter()

    @router.get("/recommend", response_model=list[schemas.MediaInfo])
    def recommend_media() -> list[dict]:
        """返回跨来源的演职员字段形态。"""
        return [
            {
                "media_source": MediaSource.Douban,
                "media_id": "1292052",
                "title": "示例电影",
                "actors": ["演员甲", {"id": 2, "name": "演员乙"}],
                "directors": ["导演甲", {"id": 3, "name": "导演乙"}],
            }
        ]

    app = FastAPI()
    app.include_router(router)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/recommend")

    assert response.status_code == 200
    media = response.json()["data"][0]
    assert media["actors"][0] == "演员甲"
    assert media["actors"][1]["id"] == 2
    assert media["actors"][1]["name"] == "演员乙"
    assert media["directors"][0] == "导演甲"
    assert media["directors"][1]["id"] == 3
    assert media["directors"][1]["name"] == "导演乙"


@pytest.mark.asyncio
async def test_douban_media_response_filters_unknown_season_years() -> None:
    """豆瓣榜单的未知季年份不应导致整个媒体列表响应校验失败。"""
    media = CoreMediaInfo(douban_info={
        "id": "1292052",
        "title": "示例电影",
        "type": "movie",
        "season_years": {1: None, 2: 2025},
    })
    router = ResponseAPIRouter()

    @router.get("/discover/douban_movies", response_model=list[schemas.MediaInfo])
    def discover_douban_movies() -> list[dict]:
        """返回包含未知季年份的豆瓣榜单条目。"""
        return [media.to_dict()]

    app = FastAPI()
    app.include_router(router)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/discover/douban_movies")

    assert response.status_code == 200
    assert response.json()["data"][0]["season_years"] == {"2": "2025"}


@pytest.mark.asyncio
async def test_media_detail_response_accepts_music_numeric_year(monkeypatch) -> None:
    """通用详情接口应按音乐模型序列化 MusicBrainz 的整数年份。"""
    media_id = "b79e2ffd-7e44-4dbf-91f9-167c05d1fc91"
    music = CoreMusicInfo(
        media_source=MediaSource.MusicBrainz,
        media_id=media_id,
        title="示例单曲",
        artists=["示例歌手"],
        album="示例专辑",
        year=2004,
    )
    media_chain = Mock()
    media_chain.async_recognize_media = AsyncMock(return_value=music)
    media_chain.async_obtain_images = AsyncMock(return_value=None)
    monkeypatch.setattr(media_endpoint, "MediaChain", Mock(return_value=media_chain))

    app = FastAPI()
    app.dependency_overrides[media_endpoint.verify_token] = lambda: None
    app.include_router(media_endpoint.router, prefix="/api/v1/media")
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get(
            f"/api/v1/media/{media_id}",
            params={"media_source": "musicbrainz", "type_name": "音乐"},
        )

    assert response.status_code == 200
    result = response.json()["data"]
    assert result["type"] == MediaType.MUSIC.value
    assert result["music_type"] == "recording"
    assert result["artists"] == ["示例歌手"]
    assert result["year"] == 2004


@pytest.mark.asyncio
async def test_media_response_accepts_legacy_source_key() -> None:
    """媒体身份重构前缓存的旧格式条目（source + media_id）应被归一化并正常响应。"""
    router = ResponseAPIRouter()

    @router.get("/recommend", response_model=list[schemas.MediaInfo])
    def recommend_media() -> list[dict]:
        """返回媒体身份重构前缓存的旧格式推荐条目。"""
        return [
            {
                "source": "themoviedb",
                "media_id": "1368337",
                "type": "电影",
                "title": "奥德赛",
                "year": "2026",
                "tmdb_id": 1368337,
            }
        ]

    app = FastAPI()
    app.include_router(router)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/recommend")

    assert response.status_code == 200
    media = response.json()["data"][0]
    assert media["media_source"] == "themoviedb"
    assert media["media_id"] == "1368337"


@pytest.mark.asyncio
async def test_media_exists_not_found_is_a_successful_query() -> None:
    """媒体库未命中是查询结果，不应被统一客户端识别为接口失败。"""

    class EmptyMediaServerQueryService:
        """返回未命中的媒体库查询桩。"""

        async def find_item_id(self, **_kwargs):
            """模拟媒体库中不存在目标媒体。"""
            return None

    response = await mediaserver_endpoint.exists_local(
        title="未入库电影",
        year="2026",
        mtype="电影",
        media_source=None,
        media_id=None,
        season=None,
        service=EmptyMediaServerQueryService(),
        _=None,
    )

    assert response.success is True
    assert response.data == {"item": {}}
