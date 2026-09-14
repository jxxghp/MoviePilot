from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.application.subscription.contract import (
    SubscriptionHistorySnapshot,
    SubscriptionIdentity,
    SubscriptionSnapshot,
)
from app.application.subscription.query import SubscriptionQueryService
from app.chain.subscribe.facade import SubscribeChain
from app.domain.context import MediaInfo
from app.schemas.types import MediaSource, MediaType


@pytest.mark.asyncio
async def test_subscription_history_count_preserves_owner_scope() -> None:
    """订阅历史总数必须复用列表的媒体类型和 owner 范围。"""
    repository = Mock()
    history_repository = AsyncMock()
    history_repository.async_count_by_type.return_value = 8
    history_repository.async_count_by_type_and_username.return_value = 3
    service = SubscriptionQueryService(
        repository,
        history_repository=history_repository,
    )

    assert await service.count_history(MediaType.MOVIE.value) == 8
    assert await service.count_history(
        MediaType.MOVIE.value,
        username="alice",
    ) == 3
    history_repository.async_count_by_type.assert_awaited_once_with(
        MediaType.MOVIE.value
    )
    history_repository.async_count_by_type_and_username.assert_awaited_once_with(
        MediaType.MOVIE.value,
        "alice",
    )


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
        SubscriptionIdentity(
            media_source=MediaSource.TMDB,
            media_id="123",
            season=2,
            episode_group="group-1",
        )
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
        SubscriptionIdentity(
            media_source=MediaSource.TMDB,
            media_id="123",
            type=MediaType.TV.value,
            season=1,
        )
    )
    assert service.has_music("R,P") is True
    repository.list.assert_called_once_with("R,P")


def test_get_by_source_upgrades_legacy_tmdbid_identity() -> None:
    """v2 下载记录只有 tmdbid 时，应补成 themoviedb + media_id 再查订阅。"""
    repository = Mock()
    expected = SimpleNamespace(id=22)
    repository.get_by.return_value = expected
    service = SubscriptionQueryService(repository)

    result = service.get_by_source({
        "id": 22,
        "name": "阿滋漫画大王",
        "type": MediaType.TV.value,
        "season": 1,
        "tmdbid": 12143,
        "imdbid": "tt0339955",
        "tvdbid": 79077,
    })

    assert result is expected
    repository.get_by.assert_called_once_with(
        SubscriptionIdentity(
            media_source=MediaSource.TMDB,
            media_id="12143",
            type=MediaType.TV.value,
            season=1,
        )
    )


def test_get_by_source_skips_incomplete_legacy_identity() -> None:
    """来源既无 media_id 也无旧 tmdbid 时，不得把半对身份传给仓储。"""
    repository = Mock()
    service = SubscriptionQueryService(repository)

    assert service.get_by_source({
        "type": MediaType.TV.value,
        "season": 1,
        "name": "Demo",
    }) is None
    repository.get_by.assert_not_called()


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


@pytest.mark.asyncio
async def test_public_subscription_view_derives_album_progress_without_track_keys() -> None:
    """公开订阅响应应返回累计曲目数，但不能泄漏内部音轨稳定键。"""
    record = SubscriptionSnapshot(
        id=31,
        name="叶惠美",
        type=MediaType.MUSIC.value,
        media_source=MediaSource.MusicBrainz,
        media_id="release-group-31",
        music_type="album",
        total_tracks=3,
        downloaded_tracks=["[1,1]", "[1,1]", "[1,2]", ""],
    )
    repository = Mock()
    repository.async_list = AsyncMock(return_value=[record])
    service = SubscriptionQueryService(repository, async_repository=repository)

    [result] = await service.list_public()

    assert result.completed_tracks == 2
    assert "downloaded_tracks" not in result.model_dump()


@pytest.mark.asyncio
async def test_public_subscription_history_does_not_invent_album_progress() -> None:
    """不含活动音轨事实的订阅历史不能伪造为零首进度。"""
    record = SubscriptionHistorySnapshot(
        id=32,
        name="叶惠美",
        type=MediaType.MUSIC.value,
        music_type="album",
        total_tracks=3,
    )
    repository = Mock()
    history_repository = Mock()
    history_repository.async_list_by_type = AsyncMock(return_value=[record])
    service = SubscriptionQueryService(
        repository,
        history_repository=history_repository,
    )

    [result] = await service.list_history(MediaType.MUSIC.value)

    assert result.completed_tracks is None
