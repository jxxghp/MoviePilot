from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import schemas
from app.api.response import ResponseAPIRouter
from app.core.context import MediaInfo as CoreMediaInfo
from app.schemas.types import MediaSource, MediaType


def test_media_search_response_preserves_core_collection_fields() -> None:
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
    response = TestClient(app).get("/media/search")

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
