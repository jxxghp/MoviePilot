"""订阅执行治理固定快照重放。"""

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.application.subscription.contract import SubscriptionSnapshot
from app.chain.subscribe.facade import SubscribeChain
from app.domain.context import Context, MediaInfo, TorrentInfo
from app.domain.metainfo import MetaInfo
from app.schemas.mediaserver import NotExistMediaInfo
from app.schemas.types import MediaSource, MediaType

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "subscription_governance_match_replay.json"


class _ReplaySubscriptionRepository:
    """保存单条订阅快照，供 Match 重放读取更新后的事实。"""

    def __init__(self, subscribe: SubscriptionSnapshot) -> None:
        self.current = subscribe

    def list(self, _state: str = None) -> list[SubscriptionSnapshot]:
        """返回本轮唯一订阅。"""
        return [self.current]

    def get(self, _subscribe_id: int) -> SubscriptionSnapshot:
        """返回当前已更新快照。"""
        return self.current

    def update(self, update_data: dict) -> SubscriptionSnapshot:
        """应用测试内的订阅字段更新。"""
        self.current = replace(self.current, **update_data)
        return self.current


class _ReplaySubscriptionListRepository:
    """提供多条订阅快照，验证批次级执行合同。"""

    def __init__(self, subscribes: list[SubscriptionSnapshot]) -> None:
        self.subscribes = subscribes
        self._by_id = {subscribe.id: subscribe for subscribe in subscribes}

    def list(self, _state: str = None) -> list[SubscriptionSnapshot]:
        """返回当前批次的全部订阅快照。"""
        return self.subscribes

    def get(self, subscribe_id: int) -> SubscriptionSnapshot | None:
        """返回取得订阅准入后的最新快照。"""
        return self._by_id.get(subscribe_id)


class _ReplayTorrentHelper:
    """让无关候选稳定停在身份冲突边界。"""

    @staticmethod
    def match_torrent(**_kwargs) -> bool:
        """无关候选不得通过标题复核。"""
        return False

    def filter_torrent(self, **_kwargs) -> bool:
        """候选若意外进入过滤阶段则保持可观察。"""
        return True


def _load_replay_cases() -> list[dict]:
    """读取版本化固定输入快照。"""
    return json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))


def _build_subscribe(case: dict) -> SubscriptionSnapshot:
    """按固定快照构造已下载 12 集的普通电视剧订阅。"""
    return SubscriptionSnapshot(
        id=101,
        name="增长中的剧集",
        year="2026",
        type=MediaType.TV.value,
        media_source=MediaSource.TMDB,
        media_id="100",
        season=1,
        total_episode=case["initial_total_episode"],
        start_episode=1,
        lack_episode=case["initial_lack_episode"],
        note=list(range(1, 13)),
        state="R",
        sites=[],
        best_version=0,
        manual_total_episode=0,
    )


def _build_unrelated_candidate() -> Context:
    """构造不会命中目标订阅但可触发完整 Match 流程的候选。"""
    return Context(
        meta_info=MetaInfo(title="无关剧集 S01E01"),
        media_info=MediaInfo(
            media_source=MediaSource.TMDB,
            media_id="999",
            type=MediaType.TV,
            title="无关剧集",
            year="2026",
        ),
        torrent_info=TorrentInfo(
            title="无关剧集 S01E01",
            site=1,
            site_name="ReplaySite",
            pri_order=100,
        ),
        resource_source="rss",
        match_source=MediaSource.TMDB.value,
        candidate_recognized=True,
    )


@pytest.mark.parametrize("case", _load_replay_cases(), ids=lambda case: case["name"])
def test_match_replays_fresh_episode_completion_contract(case, monkeypatch):
    """日常 Match 必须用本轮新鲜季集事实决定 12 集完成或 13 集继续订阅。"""
    repository = _ReplaySubscriptionRepository(_build_subscribe(case))
    recognition_calls = []
    completions = []
    fresh_media = MediaInfo(
        media_source=MediaSource.TMDB,
        media_id="100",
        type=MediaType.TV,
        title="增长中的剧集",
        year="2026",
        seasons={1: list(range(1, case["fresh_total_episode"] + 1))},
    )

    class _ReplayMediaChain:
        """记录 Match 的媒体识别参数并返回固定新鲜事实。"""

        def recognize_media(self, **kwargs) -> MediaInfo:
            """返回当前场景的新鲜季集快照。"""
            recognition_calls.append(kwargs)
            return fresh_media

        @staticmethod
        def recognize_by_meta(*_args, **_kwargs) -> MediaInfo:
            """候选已有明确身份，本场景不应重新识别。"""
            raise AssertionError("明确身份候选不应重新识别")

        @staticmethod
        def supplement_media_info(mediainfo: MediaInfo) -> MediaInfo:
            """保持固定媒体事实，不引入其他来源。"""
            return mediainfo

    chain = SubscribeChain()
    chain.subscription_repository = repository
    chain._SubscribeChain__apply_subscribe_update = (
        lambda _subscribe, update_data, **_kwargs: repository.update(dict(update_data))
    )
    chain._SubscribeChain__finish_subscribe = (
        lambda subscribe, **_kwargs: completions.append(subscribe)
    )

    def _resolve_missing(*, subscribe, mediakey, **_kwargs):
        """按当前总集数重放媒体库缺失事实。"""
        if subscribe.total_episode == 12:
            return True, {}
        return False, {
            mediakey: {
                1: NotExistMediaInfo(
                    season=1,
                    episodes=[13],
                    total_episode=13,
                    start_episode=1,
                )
            }
        }

    chain.resolve_subscribe_missing = _resolve_missing
    monkeypatch.setattr("app.chain.subscribe.match.MediaChain", _ReplayMediaChain)
    monkeypatch.setattr("app.chain.subscribe.match.TorrentHelper", _ReplayTorrentHelper)
    monkeypatch.setattr(
        "app.chain.subscribe.match.get_configured_system_config",
        lambda: SimpleNamespace(get=lambda _key: []),
    )
    monkeypatch.setattr(
        "app.chain.subscribe.query.get_configured_system_config",
        lambda: SimpleNamespace(get=lambda _key: []),
    )

    chain.match({"replay.example": [_build_unrelated_candidate()]})

    assert len(recognition_calls) == 1
    assert recognition_calls[0]["cache"] is False
    assert repository.current.total_episode == case["expected_total_episode"]
    assert repository.current.lack_episode == case["expected_lack_episode"]
    assert bool(completions) is case["expected_completed"]


def test_match_skips_external_facts_when_index_has_no_possible_candidates(monkeypatch):
    """本轮只有明确冲突候选时，不得为订阅调用 TMDB 或媒体服务器准备。"""
    subscribe = _build_subscribe(_load_replay_cases()[0])
    repository = _ReplaySubscriptionRepository(subscribe)
    candidate = _build_unrelated_candidate()
    candidate.meta_info.media_source = MediaSource.TMDB
    candidate.meta_info.media_id = "999"
    chain = SubscribeChain()
    chain.subscription_repository = repository
    chain.resolve_subscribe_missing = lambda **_kwargs: pytest.fail("不应查询媒体库缺失事实")

    class _UnexpectedMediaChain:
        """任何媒体识别调用都表示候选门禁失效。"""

        def recognize_media(self, **_kwargs):
            """阻止无候选订阅读取外部媒体事实。"""
            pytest.fail("不应读取 TMDB 新鲜事实")

        def recognize_by_meta(self, *_args, **_kwargs):
            """明确候选身份不应重新识别。"""
            pytest.fail("不应重新识别明确候选")

    monkeypatch.setattr("app.chain.subscribe.match.MediaChain", _UnexpectedMediaChain)
    monkeypatch.setattr(
        "app.chain.subscribe.query.get_configured_system_config",
        lambda: SimpleNamespace(get=lambda _key: []),
    )

    chain.match({"replay.example": [candidate]})


def test_metadata_reconcile_reuses_fresh_fact_without_candidate_batch(monkeypatch):
    """独立元数据巡检应复用同一次新鲜识别执行完成对账。"""
    subscribe = _build_subscribe(_load_replay_cases()[1])
    repository = _ReplaySubscriptionRepository(subscribe)
    recognition_calls = []
    reconciled = []
    fresh_media = MediaInfo(
        media_source=MediaSource.TMDB,
        media_id="100",
        type=MediaType.TV,
        title="增长中的剧集",
        year="2026",
        seasons={1: list(range(1, 14))},
    )

    class _ReplayMediaChain:
        """为独立完成对账返回固定新鲜媒体事实。"""

        def recognize_media(self, **kwargs) -> MediaInfo:
            """记录识别参数并返回 13 集事实。"""
            recognition_calls.append(kwargs)
            return fresh_media

    chain = SubscribeChain()
    chain.subscription_repository = repository
    chain._SubscribeChain__apply_subscribe_update = (
        lambda _subscribe, update_data, **_kwargs: repository.update(dict(update_data))
    )
    chain.reconcile_subscription_completion = (
        lambda **kwargs: reconciled.append(kwargs)
    )
    monkeypatch.setattr("app.chain.subscribe.refresh.MediaChain", _ReplayMediaChain)

    chain.check_and_reconcile()

    assert len(recognition_calls) == 1
    assert recognition_calls[0]["cache"] is False
    assert repository.current.total_episode == 13
    assert repository.current.lack_episode == 1
    assert len(reconciled) == 1
    assert reconciled[0]["subscribe"] == repository.current
    assert reconciled[0]["mediainfo"] == fresh_media
    assert reconciled[0]["mediainfo"] is not fresh_media


def test_match_reuses_fresh_fact_for_same_media_subscriptions(monkeypatch):
    """同媒体同季订阅在一个 Match 批次内只读取一次外部新鲜事实。"""
    first = _build_subscribe(_load_replay_cases()[1])
    second = replace(first, id=102)
    repository = _ReplaySubscriptionListRepository([first, second])
    recognition_calls = []
    received_media = []
    candidate = _build_unrelated_candidate()
    candidate.meta_info.media_source = MediaSource.TMDB
    candidate.meta_info.media_id = "100"
    candidate.media_info.media_id = "100"
    candidate.media_info.title = "增长中的剧集"

    class _ReplayMediaChain:
        """返回同一可变对象，验证租约向每个订阅交付独立副本。"""

        def recognize_media(self, **kwargs) -> MediaInfo:
            """记录一次外部识别并返回固定媒体事实。"""
            recognition_calls.append(kwargs)
            return MediaInfo(
                media_source=MediaSource.TMDB,
                media_id="100",
                type=MediaType.TV,
                title="增长中的剧集",
                year="2026",
                seasons={1: list(range(1, 14))},
            )

        @staticmethod
        def recognize_by_meta(*_args, **_kwargs) -> MediaInfo:
            """候选已有明确身份，本场景不应重新识别。"""
            raise AssertionError("明确身份候选不应重新识别")

    chain = SubscribeChain()
    chain.subscription_repository = repository

    def _handle_existing(*, mediainfo, **_kwargs):
        """记录每个订阅收到的事实对象并提前结束其匹配。"""
        received_media.append(mediainfo)
        return True, {}

    chain.check_and_handle_existing_media = _handle_existing
    monkeypatch.setattr("app.chain.subscribe.match.MediaChain", _ReplayMediaChain)
    monkeypatch.setattr(
        "app.chain.subscribe.match.get_configured_system_config",
        lambda: SimpleNamespace(get=lambda _key: []),
    )
    monkeypatch.setattr(
        "app.chain.subscribe.query.get_configured_system_config",
        lambda: SimpleNamespace(get=lambda _key: []),
    )

    chain.match({"replay.example": [candidate]})

    assert len(recognition_calls) == 1
    assert recognition_calls[0]["cache"] is False
    assert len(received_media) == 2
    assert received_media[0] is not received_media[1]
