"""订阅单轮新鲜事实租约测试。"""

from app.application.subscription.contract import SubscriptionSnapshot
from app.application.subscription.facts import FreshFactKey, FreshFactLease
from app.domain.context import MediaInfo
from app.schemas.types import MediaSource, MediaType


def _subscribe(**overrides) -> SubscriptionSnapshot:
    """构造具有明确媒体身份的电视剧订阅。"""
    values = {
        "id": 1,
        "name": "租约测试剧",
        "type": MediaType.TV.value,
        "media_source": MediaSource.TMDB,
        "media_id": "100",
        "season": 1,
        "episode_group": None,
        "state": "R",
    }
    values.update(overrides)
    return SubscriptionSnapshot(**values)


def test_fresh_fact_key_isolates_season_and_episode_group():
    """相同媒体的不同季或剧集组不得共享动态季集事实。"""
    default = FreshFactKey.from_subscribe(_subscribe())
    other_season = FreshFactKey.from_subscribe(_subscribe(season=2))
    other_group = FreshFactKey.from_subscribe(_subscribe(episode_group="group-a"))

    assert default != other_season
    assert default != other_group
    assert other_season != other_group


def test_fresh_fact_lease_loads_once_and_returns_isolated_copies():
    """相同事实本轮只加载一次，消费者清理对象不会污染后续租约命中。"""
    lease = FreshFactLease()
    subscribe = _subscribe()
    calls = []

    def _load() -> MediaInfo:
        """返回带完整季集和别名的可变媒体事实。"""
        calls.append(True)
        return MediaInfo(
            media_source=MediaSource.TMDB,
            media_id="100",
            type=MediaType.TV,
            title="租约测试剧",
            seasons={1: [1, 2, 3]},
            names=["Lease Show"],
        )

    first = lease.get_or_load(subscribe, _load)
    first.clear()
    second = lease.get_or_load(subscribe, _load)

    assert len(calls) == 1
    assert lease.loads == 1
    assert lease.hits == 1
    assert second.seasons == {1: [1, 2, 3]}
    assert second.names == ["Lease Show"]
    assert first is not second


def test_fresh_fact_lease_merges_failed_result_within_round():
    """相同媒体本轮识别失败后不应立即重复请求外部服务。"""
    lease = FreshFactLease()
    calls = []

    def _load():
        """记录一次失败的新鲜事实请求。"""
        calls.append(True)
        return None

    assert lease.get_or_load(_subscribe(), _load) is None
    assert lease.get_or_load(_subscribe(id=2), _load) is None
    assert len(calls) == 1
    assert lease.loads == 1
    assert lease.hits == 1


def test_fresh_fact_lease_does_not_share_missing_identity():
    """身份缺失订阅必须各自识别，避免仅按标题错误合并。"""
    lease = FreshFactLease()
    calls = []
    subscribe = _subscribe(media_source=None, media_id=None)

    def _load() -> MediaInfo:
        """返回本次标题识别结果。"""
        calls.append(True)
        return MediaInfo(type=MediaType.TV, title="标题识别结果")

    lease.get_or_load(subscribe, _load)
    lease.get_or_load(subscribe, _load)

    assert len(calls) == 2
    assert lease.loads == 2
    assert lease.hits == 0
