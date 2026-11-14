"""
订阅入库事实同步：整理入库后立即把交付集号写回订阅。
"""

from dataclasses import replace
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import Mock

import pytest

from app.application.chain.events import restore_transfer_result, snapshot_transfer_result
from app.application.subscription.contract import SubscriptionSnapshot
from app.chain.subscribe.facade import SubscribeChain
from app.chain.subscribe.library import SubscribeLibraryOwner
from app.domain.context import MediaInfo
from app.domain.metainfo import MetaInfo
from app.runtime.events import Event
from app.schemas.types import EventType, MediaSource, MediaType


def _build_subscribe(**overrides: Any) -> SubscriptionSnapshot:
    """
    构造一份尚未同步入库事实的电视剧订阅快照。
    """
    payload: Dict[str, Any] = {
        "id": 1,
        "name": "测试剧",
        "type": MediaType.TV.value,
        "media_source": MediaSource.TMDB,
        "media_id": "874",
        "season": 1,
        "total_episode": 12,
        "lack_episode": 12,
        "note": [],
        "state": "R",
    }
    payload.update(overrides)
    return SubscriptionSnapshot(**payload)


def _build_owner(subscribe: Optional[SubscriptionSnapshot]) -> Tuple[SubscribeLibraryOwner, List[Tuple[Any, ...]]]:
    """
    构造隔离的入库事实同步组件，并返回记录写入的容器。
    """
    repository = Mock()
    repository.get_by = Mock(return_value=subscribe)
    applied: List[Tuple[Any, ...]] = []
    owner = object.__new__(SubscribeLibraryOwner)
    owner.subscription_repository = repository
    owner._SubscribeChain__apply_subscribe_update = (  # noqa: SLF001
        lambda current, fields, scene="progress": applied.append((current, fields, scene)) or current
    )
    return owner, applied


def _build_event(filename: str, media_id: str = "874", end_episode: Optional[int] = None) -> Event:
    """
    构造一次成功整理的完成事件。

    多集交付的结束集号显式给出，避免依赖标题解析器对集号区间的支持。
    """
    meta = MetaInfo(filename)
    if end_episode is not None:
        meta.end_episode = end_episode
    return Event(
        EventType.TransferComplete,
        {
            "mediainfo": MediaInfo(
                title="测试剧",
                type=MediaType.TV,
                media_source=MediaSource.TMDB,
                media_id=media_id,
            ),
            "meta": meta,
        },
    )


def test_subscribe_chain_transfer_complete_wrapper_delegates() -> None:
    """
    SubscribeChain 的公开事件包装器委托到入库事实 owner。
    """
    chain = object.__new__(SubscribeChain)
    event = Event(EventType.TransferComplete, {})
    chain._sync_library_progress_after_transfer = Mock()

    chain.sync_library_progress_after_transfer(event)

    chain._sync_library_progress_after_transfer.assert_called_once_with(event)


def test_transfer_complete_records_delivered_episodes_as_library_facts() -> None:
    """
    整理入库的剧集立即写回订阅入库集号，缺失集数同步收缩。
    """
    subscribe = _build_subscribe()
    owner, applied = _build_owner(subscribe)

    owner._sync_library_progress_after_transfer(_build_event("Test.Show.S01E05.mkv"))

    assert len(applied) == 1
    fields = applied[0][1]
    assert fields["note"] == [5]
    assert fields["lack_episode"] == 11
    assert fields["last_update"]
    assert applied[0][2] == "transfer"


def test_transfer_complete_keeps_aggregate_consistent_with_episode_facts() -> None:
    """
    入库集号与缺失集数来自同一份事实，客户端计算的已入库集数不会与明细矛盾。
    """
    subscribe = _build_subscribe(total_episode=12, lack_episode=9, note=[1, 2, 3])
    owner, applied = _build_owner(subscribe)

    owner._sync_library_progress_after_transfer(_build_event("Test.Show.S01E04.mkv"))

    fields = applied[0][1]
    assert fields["note"] == [1, 2, 3, 4]
    assert fields["lack_episode"] == 8
    assert (subscribe.total_episode or 0) - fields["lack_episode"] == len(fields["note"])


def test_transfer_complete_records_multi_episode_delivery() -> None:
    """
    一次交付多集时按交付范围补写全部入库集号。
    """
    subscribe = _build_subscribe()
    owner, applied = _build_owner(subscribe)

    owner._sync_library_progress_after_transfer(_build_event("Test.Show.S01E05.mkv", end_episode=7))

    fields = applied[0][1]
    assert fields["note"] == [5, 6, 7]
    assert fields["lack_episode"] == 9


def test_transfer_complete_ignores_episode_outside_subscription_range() -> None:
    """
    超出订阅目标范围的集号不写入订阅入库事实。
    """
    subscribe = _build_subscribe(total_episode=6)
    owner, applied = _build_owner(subscribe)

    owner._sync_library_progress_after_transfer(_build_event("Test.Show.S01E09.mkv"))

    assert applied == []


def test_transfer_complete_ignores_already_recorded_episode() -> None:
    """
    已记录的入库集号不重复写入，缺失集数也不重复收缩。
    """
    subscribe = _build_subscribe(note=[5], lack_episode=11)
    owner, applied = _build_owner(subscribe)

    owner._sync_library_progress_after_transfer(_build_event("Test.Show.S01E05.mkv"))

    assert applied == []


def test_transfer_complete_ignores_other_season_subscription() -> None:
    """
    身份相同但季号不同的订阅不被写入别季剧集。
    """
    subscribe = _build_subscribe(season=2)
    owner, applied = _build_owner(subscribe)

    owner._sync_library_progress_after_transfer(_build_event("Test.Show.S01E05.mkv"))

    assert applied == []


def test_transfer_complete_keeps_washed_episode_priority_facts() -> None:
    """
    洗版订阅已按集记录的集号不重复写入入库集号，缺失集数也不重复收缩。
    """
    subscribe = _build_subscribe(best_version=1, episode_priority={"5": 100}, lack_episode=11)
    owner, applied = _build_owner(subscribe)

    owner._sync_library_progress_after_transfer(_build_event("Test.Show.S01E05.mkv"))

    assert applied == []


def test_transfer_complete_records_washed_episode_without_priority() -> None:
    """
    洗版订阅尚未记录该集时，交付事实补写入库集号并收缩缺失集数。
    """
    subscribe = _build_subscribe(best_version=1, episode_priority={}, lack_episode=12)
    owner, applied = _build_owner(subscribe)

    owner._sync_library_progress_after_transfer(_build_event("Test.Show.S01E05.mkv"))

    fields = applied[0][1]
    assert fields["note"] == [5]
    assert fields["lack_episode"] == 11


def test_transfer_complete_without_matching_subscription_is_ignored() -> None:
    """
    没有匹配订阅时不做任何写入。
    """
    owner, applied = _build_owner(None)

    owner._sync_library_progress_after_transfer(_build_event("Test.Show.S01E05.mkv"))

    assert applied == []


def test_transfer_complete_records_movie_delivery() -> None:
    """
    电影整理入库后将订阅记录为已入库 [1]。
    """
    subscribe = _build_subscribe(type=MediaType.MOVIE.value, total_episode=0, lack_episode=0, season=None)
    owner, applied = _build_owner(subscribe)
    event = Event(
        EventType.TransferComplete,
        {
            "mediainfo": MediaInfo(
                title="测试电影",
                type=MediaType.MOVIE,
                media_source=MediaSource.TMDB,
                media_id="874",
            ),
            "meta": MetaInfo("Test.Movie.2026.mkv"),
        },
    )

    owner._sync_library_progress_after_transfer(event)

    assert len(applied) == 1
    fields = applied[0][1]
    assert fields["note"] == [1]
    assert fields["last_update"]
    assert applied[0][2] == "transfer"


def test_transfer_complete_ignores_already_recorded_movie() -> None:
    """
    已入库的电影不重复写入。
    """
    subscribe = _build_subscribe(
        type=MediaType.MOVIE.value, total_episode=0, lack_episode=0, season=None, note=[1]
    )
    owner, applied = _build_owner(subscribe)
    event = Event(
        EventType.TransferComplete,
        {
            "mediainfo": MediaInfo(
                title="测试电影",
                type=MediaType.MOVIE,
                media_source=MediaSource.TMDB,
                media_id="874",
            ),
            "meta": MetaInfo("Test.Movie.2026.mkv"),
        },
    )

    owner._sync_library_progress_after_transfer(event)

    assert applied == []


@pytest.mark.parametrize("media_id", ["", "0"])
def test_transfer_complete_without_media_identity_is_ignored(media_id: str) -> None:
    """
    缺少完整媒体身份时无法定位订阅，不做任何写入。
    """
    subscribe = _build_subscribe()
    owner, applied = _build_owner(subscribe)
    event = Event(
        EventType.TransferComplete,
        {
            "mediainfo": MediaInfo(
                title="测试剧",
                type=MediaType.TV,
                media_source=MediaSource.TMDB,
                media_id=media_id,
            ),
            "meta": MetaInfo("Test.Show.S01E05.mkv"),
        },
    )

    owner._sync_library_progress_after_transfer(event)

    assert applied == []


def test_transfer_complete_syncs_replayed_durable_payload() -> None:
    """
    整理事件经 outbox 快照重放后仍能识别媒体身份并同步入库事实。
    """
    subscribe = _build_subscribe()
    owner, applied = _build_owner(subscribe)
    payload = restore_transfer_result(
        snapshot_transfer_result(
            {
                "mediainfo": MediaInfo(
                    title="测试剧",
                    type=MediaType.TV,
                    media_source=MediaSource.TMDB,
                    media_id="874",
                ),
                "meta": MetaInfo("Test.Show.S01E05.mkv"),
            }
        )
    )

    owner._sync_library_progress_after_transfer(Event(EventType.TransferComplete, payload))

    assert len(applied) == 1
    assert applied[0][1]["note"] == [5]


@pytest.mark.asyncio
async def test_calendar_reports_delivered_episode_as_in_library(monkeypatch) -> None:
    """
    入库事实同步后，订阅日历把已交付的剧集投影为已入库而不是未入库。
    """
    from datetime import date

    from app.chain.subscribe.calendar import SubscribeCalendarChain
    from app.schemas.calendar import CalendarDate, CalendarEpisode
    from app.schemas.subscribe import Subscribe

    async def schedule(_source, _media_id, _mtype, _season, _group):
        """
        用固定播出日期模拟已缓存的媒体源。
        """
        return [CalendarDate(date="2026-10-01", episodes=[CalendarEpisode(episode_number=5)])]

    monkeypatch.setattr(SubscribeCalendarChain, "_schedule", staticmethod(schedule))
    subscribe = _build_subscribe()
    owner, applied = _build_owner(subscribe)
    owner._sync_library_progress_after_transfer(_build_event("Test.Show.S01E05.mkv"))
    synced = replace(subscribe, **applied[0][1])

    page = await SubscribeCalendarChain().window(
        [Subscribe.model_validate(synced.to_dict())],
        date(2026, 10, 1),
        date(2026, 11, 1),
        1,
        50,
    )

    calendar_subscribe = page.items[0].subscribe
    assert calendar_subscribe.note == [5]
    assert calendar_subscribe.lack_episode == 11
    assert calendar_subscribe.completed_episode == 1
