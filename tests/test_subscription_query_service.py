from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.application.subscription.query import SubscriptionQueryService
from app.chain.subscribe import SubscribeChain
from app.domain.context import MediaInfo
from app.schemas.types import MediaSource, MediaType


def test_subscription_query_service_builds_complete_exists_identity() -> None:
    """存在性查询必须保留媒体、音乐实体、季和剧集组全部身份维度。"""
    repository = Mock()
    repository.exists.return_value = True
    service = SubscriptionQueryService(repository)
    media = MediaInfo(
        type=MediaType.TV,
        title="Demo",
        media_source=MediaSource.TMDB,
        media_id="123",
        episode_group="group-1",
    )

    assert service.exists(media, SimpleNamespace(begin_season=2)) is True
    repository.exists.assert_called_once_with(
        media_source=MediaSource.TMDB,
        media_id="123",
        music_type=None,
        season=2,
        episode_group="group-1",
    )


def test_subscription_query_service_filters_source_and_music_state() -> None:
    """来源查询不透传展示字段，音乐状态查询保持 R/P 联合列表语义。"""
    repository = Mock()
    expected = SimpleNamespace(id=1)
    repository.get_by.return_value = expected
    repository.list.return_value = [
        SimpleNamespace(type=MediaType.MOVIE.value),
        SimpleNamespace(type=MediaType.MUSIC.value),
    ]
    service = SubscriptionQueryService(repository)

    result = service.get_by_source({
        "id": 1,
        "name": "Demo",
        "type": MediaType.TV.value,
        "season": 1,
        "media_source": MediaSource.TMDB,
        "media_id": "123",
        "music_type": None,
    })

    assert result is expected
    repository.get_by.assert_called_once_with(
        type=MediaType.TV.value,
        season=1,
        media_source=MediaSource.TMDB,
        media_id="123",
        music_type=None,
    )
    assert service.has_music("R,P") is True
    repository.list.assert_called_once_with("R,P")


def test_subscribe_chain_facade_delegates_three_query_slices() -> None:
    """SubscribeChain 保持三个公开方法签名并仅负责来源解析和结果转发。"""
    service = Mock()
    service.exists.return_value = True
    service.get_by_source.return_value = SimpleNamespace(id=7)
    service.has_music.return_value = True
    media = MediaInfo(
        type=MediaType.MOVIE,
        title="Demo",
        media_source=MediaSource.TMDB,
        media_id="123",
    )
    source = (
        'Subscribe|{"type":"电影","season":null,'
        '"media_source":"themoviedb","media_id":"123"}'
    )

    with patch.object(SubscribeChain, "_subscription_query", return_value=service):
        chain = object.__new__(SubscribeChain)
        assert chain.exists(media) is True
        assert chain.get_subscribe_by_source(source).id == 7
        assert chain.has_music_subscribe() is True

    service.exists.assert_called_once_with(media, None)
    service.get_by_source.assert_called_once_with({
        "type": "电影",
        "season": None,
        "media_source": "themoviedb",
        "media_id": "123",
    })
    service.has_music.assert_called_once_with("R,P")
