"""
批量卡片状态的顺序、权限、轻量投影及查询窗口边界。
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.dependencies.auth import get_current_active_user_async
from app.api.dependencies.history import get_mediaserver_query_service
from app.api.dependencies.subscription import get_subscription_query_service
from app.api.endpoints import media
from app.schemas.media import MediaStatusQuery
from app.schemas.subscribe import Subscribe


@pytest.fixture
def services():
    """
    构造相互隔离的本地媒体库与订阅查询替身，避免访问数据库和外部服务。
    """
    return (
        SimpleNamespace(find_item_id=AsyncMock(return_value="local-item")),
        SimpleNamespace(
            list_by_media_identity=AsyncMock(return_value=[]),
            list_by_video_metadata=AsyncMock(return_value=[]),
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [False, True])
async def test_batch_status_keeps_owner_modes_identity_and_input_order(services, admin):
    """
    同媒体不同季只返回可见订阅模式，完整洗版不退化为普通洗版，且结果顺序对应输入。
    """
    library, subscriptions = services
    subscriptions.list_by_media_identity.return_value = [
        Subscribe(id=1, username="alice", type="电视剧", season=1, best_version=0),
        Subscribe(id=2, username="alice", type="电视剧", season=2, best_version=1, best_version_full=1),
        Subscribe(id=3, username="bob", type="电视剧", season=3, best_version=1),
        Subscribe(id=4, username=None, type="电视剧", season=4),
    ]
    items = [
        MediaStatusQuery(media_source="themoviedb", media_id="874", title="批量剧集", mtype="电视剧", season=season)
        for season in [3, 1]
    ]
    result = await media.media_status(items, library, subscriptions, SimpleNamespace(name="alice", is_superuser=admin))
    assert [item.subscribed for item in result] == [admin, True]
    expected = {1: "normal", 2: "best_version_full"}
    if admin:
        expected.update({3: "best_version", 4: "normal"})
    assert result[0].seasons == expected
    assert result[0].item_id == "local-item"
    assert set(result[0].model_dump()) == {"item_id", "subscribed", "seasons"}
    assert [call.kwargs["season"] for call in library.find_item_id.await_args_list] == [3, 1]
    assert subscriptions.list_by_media_identity.await_args.args[1] == "874"


@pytest.mark.asyncio
async def test_batch_status_preserves_video_metadata_fallback_and_music_entity(services):
    """
    影视保留跨来源标题年份回退，音乐只按实体身份查询且不调用影视媒体库。
    """
    library, subscriptions = services
    subscriptions.list_by_video_metadata.return_value = [Subscribe(id=7, username="alice", type="电影")]
    actor = SimpleNamespace(name="alice", is_superuser=False)
    movie, music = await media.media_status([
        MediaStatusQuery(media_source="douban", media_id="movie-874", title="电影", year="2026", mtype="电影"),
        MediaStatusQuery(media_source="musicbrainz", media_id="album-874", mtype="音乐", music_type="album"),
    ], library, subscriptions, actor)
    assert movie.subscribed is True
    assert music.subscribed is False and music.item_id is None
    library.find_item_id.assert_awaited_once()
    subscriptions.list_by_video_metadata.assert_awaited_once()
    assert subscriptions.list_by_video_metadata.await_args.kwargs["year"] == "2026"
    assert subscriptions.list_by_media_identity.await_args.args[1:] == ("album-874", "album")


def test_batch_status_http_bounds_and_identity_validation(services):
    """
    HTTP 层限制一次最多五十张卡片，拒绝半对媒体身份并返回标准数据 envelope。
    """
    library, subscriptions = services
    app = FastAPI()
    app.include_router(media.router, prefix="/media")
    app.dependency_overrides[get_mediaserver_query_service] = lambda: library
    app.dependency_overrides[get_subscription_query_service] = lambda: subscriptions
    app.dependency_overrides[get_current_active_user_async] = lambda: SimpleNamespace(name="alice", is_superuser=False)
    with TestClient(app) as client:
        for payload in [[], [{}] * 51, [{"media_source": "themoviedb"}], [{"media_source": "themoviedb", "media_id": "0"}]]:
            assert client.post("/media/status", json=payload).status_code == 422
        response = client.post("/media/status", json=[{"title": "电影", "mtype": "电影"}])
    assert response.status_code == 200
    assert response.json()["data"] == [{"item_id": "local-item", "subscribed": False, "seasons": {}}]
