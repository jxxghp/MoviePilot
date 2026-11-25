"""
订阅业务活动的事务、隔离、计数与等待语义回归测试。
"""

import asyncio
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.application.subscription.activity import (
    SubscriptionActivityRun,
    SubscriptionActivityService,
    current_activity_run,
    queued_activity,
)
from app.application.subscription.contract import (
    SubscriptionHistoryPatch,
    SubscriptionIdentity,
    SubscriptionPatch,
    SubscriptionSnapshot,
)
from app.application.subscription.sitebudget import SubscriptionSearchDeferred
from app.db.adapters.subscription import SessionSubscriptionRepository
from app.db.adapters.subscriptionactivity import TransactionalSubscriptionActivityRepository
from app.db.models.subscribe import Subscribe
from app.db.models.subscribeactivity import SubscribeActivity
from app.db.models.subscribehistory import SubscribeHistory
from app.db.session import async_session_scope
from app.schemas.types import MediaSource


def _snapshot(**values) -> SubscriptionSnapshot:
    """
    创建拥有独立活动身份的测试订阅。
    """
    return SubscriptionSnapshot(id=1, name="Example", activity_key=uuid4().hex, resolution="2160p", **values)


def _resource(title="Example.S01E09", site=1):
    """
    生成含敏感下载地址的候选，验证活动摘要不会泄漏该地址。
    """
    return SimpleNamespace(torrent_info=SimpleNamespace(
        title=title, site=site, site_name=f"Site {site}",
        enclosure="https://example.test/download?passkey=private", site_cookie="private",
    ))


def test_creation_and_activity_rollback_together(db):
    """
    未提交的订阅与创建活动必须一起回滚，重复创建不会重复记录。
    """
    db.watermark(Subscribe, SubscribeActivity)
    repository = SessionSubscriptionRepository(db.session)
    identity = SubscriptionIdentity(media_source=MediaSource.TMDB, media_id="9916963", type="电视剧", season=1)
    payload = SubscriptionPatch({"name": "Example", "media_source": "tmdb", "media_id": "9916963", "type": "电视剧", "season": 1, "username": "owner"})
    result = repository.stage_add(identity, payload, "owner")
    assert result.created
    event_key = db.session.execute(select(SubscribeActivity.activity_key).where(SubscribeActivity.kind == "created")).scalars().all()[-1]
    key = repository.get(result.subscribe_id).activity_key
    assert event_key == key
    db.session.rollback()
    assert db.session.get(Subscribe, result.subscribe_id) is None
    assert not db.session.execute(select(SubscribeActivity).where(SubscribeActivity.activity_key == key)).scalars().all()
    created = repository.stage_add(identity, payload, "owner")
    db.session.commit()
    assert not repository.stage_add(identity, payload, "owner").created
    assert len(db.session.execute(select(SubscribeActivity).where(SubscribeActivity.activity_key == repository.get(created.subscribe_id).activity_key)).scalars().all()) == 1


def test_updates_freeze_rules_and_history_keeps_original_identity(db):
    """
    人工修改保存前后值，自动时间更新不刷屏，完成历史关联原订阅。
    """
    db.watermark(Subscribe, SubscribeHistory, SubscribeActivity)
    subscribe = Subscribe(name="Example", username="owner", media_source="tmdb", media_id="9916964", resolution="1080p")
    db.add(subscribe)
    repository = SessionSubscriptionRepository(db.session)
    repository.stage_update(subscribe.id, SubscriptionPatch({"last_update": "2026-10-12"}))
    assert not db.session.execute(select(SubscribeActivity).where(SubscribeActivity.activity_key == subscribe.activity_key)).scalars().all()
    snapshot = repository.stage_update(subscribe.id, SubscriptionPatch({"resolution": "2160p"}))
    repository.stage_history(SubscriptionHistoryPatch.from_subscription(snapshot.to_dict()))
    repository.stage_delete_sync(subscribe.id)
    db.session.commit()
    history = db.session.execute(select(SubscribeHistory).where(SubscribeHistory.activity_key == snapshot.activity_key)).scalar_one()
    events = db.session.execute(select(SubscribeActivity).where(SubscribeActivity.activity_key == history.activity_key).order_by(SubscribeActivity.id)).scalars().all()
    assert [event.kind for event in events] == ["updated", "completed"]
    assert events[0].details["changes"] == [{"field": "resolution", "before": "1080p", "after": "2160p"}]
    activity_repository = TransactionalSubscriptionActivityRepository(lambda: db.session, async_session_scope)
    history_id, event_id = history.id, events[0].event_id

    async def verify_history_access():
        """
        完成后仍可按历史 owner 读取原始活动和规则快照。
        """
        page = await activity_repository.list_accessible(history_id, "owner", False, history=True,
                                                         kind=None, since=None, before=None, limit=30)
        assert page and len(page[0]) == 2
        detail = await activity_repository.get_accessible(history_id, event_id, "owner", False, history=True)
        assert detail and detail.details["rules"]["resolution"] == "2160p"
        assert await activity_repository.get_accessible(history_id, event_id, "other", False, history=True) is None

    asyncio.run(verify_history_access())
    replacement = Subscribe(id=subscribe.id, name="Replacement", username="owner", media_source="tmdb", media_id="9916965")
    db.add(replacement)
    assert replacement.activity_key != history.activity_key


def test_round_counts_are_distinct_and_details_do_not_include_credentials():
    """
    候选、符合条件和实际提交分别去重，重复筛选不会夸大计数。
    """
    run = SubscriptionActivityRun(_snapshot(), "search", "manual")
    resource = _resource()
    run.resource(resource, "related")
    run.resource(resource, "rejected", "不符合分辨率")
    run.resource(resource, "rejected", "不符合优先级")
    assert run.details["resources"][0]["reason"] == "不符合优先级"
    run.resource(resource, "qualified")
    run.resource(resource, "qualified")
    run.resource(resource, "submitted")
    run.resource(resource, "qualified")
    run.resource(resource, "submitted")
    assert (run.counts["related"], run.counts["qualified"], run.counts["submitted"]) == (1, 1, 1)
    assert "private" not in str(run.snapshot())
    run.site(1, candidates=32)
    run.site(2, status="waiting")
    assert run.counts["requested_sites"] == 1
    assert run.counts["discovered"] == 32


def test_waiting_is_not_failure_and_scope_is_released():
    """
    暂时站点冲突保存继续时间，不伪装成失败或完成。
    """
    saved = []
    service = SubscriptionActivityService(SimpleNamespace(save=saved.append))
    with pytest.raises(SubscriptionSearchDeferred):
        with service.scope(_snapshot(), "search", "automatic"):
            raise SubscriptionSearchDeferred(retry_at="2026-10-12T10:50:00+00:00", site_ids=(8,), wait_reason="busy")
    assert saved[-1].status == "waiting"
    assert saved[-1].details["pending_site_ids"] == [8]
    assert current_activity_run() is None
    with service.scope(_snapshot(), "refresh", "automatic"):
        pass
    assert saved[-1].status == "empty"


def test_activity_details_are_limited_without_losing_summary_counts():
    """
    大量资源只保留有限摘要，业务总数仍覆盖整轮。
    """
    run = SubscriptionActivityRun(_snapshot(), "refresh", "automatic")
    for index in range(130):
        run.resource(_resource(f"Example.{index}"), "rejected", "不符合条件")
    assert run.counts["related"] == 130
    assert len(run.details["resources"]) == 100
    assert run.details["resources_omitted"] == 30


def test_search_retries_continue_the_same_event_without_duplicate_counts():
    """
    自动恢复关联原搜索，并保留每次处理结果而不重复统计下载。
    """
    stored = {}

    def save(event):
        """
        模拟固定事件身份的持久化更新。
        """
        stored[event.event_id] = event

    def get(activity_key, event_id):
        """
        只返回当前订阅拥有的历史轮次。
        """
        event = stored.get(event_id)
        return event if event is not None and event.activity_key == activity_key else None

    subscribe = _snapshot()
    service = SubscriptionActivityService(SimpleNamespace(save=save, get=get))
    with pytest.raises(SubscriptionSearchDeferred):
        with service.scope(subscribe, "search", "automatic", "task-6963") as run:
            run.resource(_resource(), "submitted")
            raise SubscriptionSearchDeferred(retry_at="2026-10-12T10:50:00+00:00", site_ids=(8,))
    first_started = stored["task-6963"].started_at
    with service.scope(subscribe, "search", "automatic", "task-6963") as run:
        run.resource(_resource(), "qualified")
        run.resource(_resource(), "submitted")
    assert len(stored) == 1
    event = stored["task-6963"]
    assert event.started_at == first_started
    assert event.status == "completed"
    assert (event.counts["related"], event.counts["qualified"], event.counts["submitted"]) == (1, 1, 1)
    assert [attempt["status"] for attempt in event.details["attempts"]] == ["waiting", "completed"]


def test_activity_query_enforces_owner_history_and_stable_pagination(db):
    """
    普通用户不能跨订阅或游标读取，新增事件不会导致翻页重复。
    """
    db.watermark(Subscribe, SubscribeHistory, SubscribeActivity)
    subscribe = Subscribe(name="Example", username="owner", media_source="tmdb", media_id="9916966")
    db.add(subscribe)

    @contextmanager
    def sync_scope():
        """
        测试写入复用已隔离的数据库会话。
        """
        yield db.session

    repository = TransactionalSubscriptionActivityRepository(sync_scope, async_session_scope)
    snapshot = _snapshot(username="owner")
    snapshot = replace(snapshot, id=subscribe.id, activity_key=subscribe.activity_key)
    for _ in range(3):
        repository.save(SubscriptionActivityRun(snapshot, "search", "manual").snapshot())

    async def query():
        """
        执行授权与游标边界断言。
        """
        params = dict(history=False, kind=None, since=None, before=None, limit=2)
        assert await repository.list_accessible(subscribe.id, "other", False, **params) is None
        first, cursor = await repository.list_accessible(subscribe.id, "owner", False, **params)
        assert len(first) == 2 and cursor
        repository.save(SubscriptionActivityRun(snapshot, "search", "manual").snapshot())
        last, next_cursor = await repository.list_accessible(subscribe.id, "owner", False, **{**params, "before": cursor})
        assert len(last) == 1 and next_cursor is None
        assert {event.event_id for event in first}.isdisjoint(event.event_id for event in last)
        assert await repository.list_accessible(subscribe.id, "other", True, **params)

    asyncio.run(query())


def test_failed_submission_is_distinct_from_empty_and_recovery():
    """
    下载器拒绝是真实失败，恢复成功后清除失败计数而不重复关联资源。
    """
    saved = []
    service = SubscriptionActivityService(SimpleNamespace(save=saved.append))
    with service.scope(_snapshot(), "refresh", "automatic") as run:
        run.resource(_resource(), "qualified")
        run.resource(_resource(), "failed", "下载器未接受该资源，未提交成功")
    assert saved[-1].status == "failed"
    assert saved[-1].counts["download_failed"] == 1
    with service.scope(_snapshot(), "refresh", "automatic") as run:
        run.resource(_resource(), "failed")
        run.resource(_resource(), "submitted")
        run.resource(_resource(), "failed")
    assert saved[-1].status == "completed"
    assert saved[-1].counts["related"] == saved[-1].counts["submitted"] == 1
    assert saved[-1].counts["download_failed"] == 0


@pytest.mark.asyncio
async def test_event_details_cannot_cross_subscription_or_owner(db):
    """
    猜中事件 ID 也不能跨 owner、活动整数 ID 或历史整数 ID 读取详情。
    """
    db.watermark(Subscribe, SubscribeActivity)
    owned = Subscribe(name="Owned", username="owner")
    other = Subscribe(name="Other", username="other")
    db.add(owned, other)
    repository = TransactionalSubscriptionActivityRepository(lambda: db.session, async_session_scope)
    snapshot = replace(_snapshot(), id=owned.id, activity_key=owned.activity_key)
    owned_id, other_id = owned.id, other.id
    event = SubscriptionActivityRun(snapshot, "refresh", "automatic").snapshot()
    repository.save(event)
    assert await repository.get_accessible(owned_id, event.event_id, "owner", False, history=False)
    assert await repository.get_accessible(owned_id, event.event_id, "admin", True, history=False)
    assert await repository.get_accessible(owned_id, event.event_id, "other", False, history=False) is None
    assert await repository.get_accessible(other_id, event.event_id, "admin", True, history=False) is None
    assert await repository.get_accessible(owned_id, event.event_id, "owner", False, history=True) is None


def test_all_failed_sites_are_not_reported_as_an_empty_success():
    """
    全部站点失败与正常零结果分开，未发出的请求不进入实际请求计数。
    """
    saved = []
    service = SubscriptionActivityService(SimpleNamespace(save=saved.append))
    with service.scope(_snapshot(), "search", "automatic") as run:
        run.counts["sites"] = 2
        run.site(1, status="failed", attempted=True)
        run.site(2, status="failed", attempted=True)
    assert saved[-1].status == "failed" and saved[-1].counts["failed_sites"] == 2
    with service.scope(_snapshot(), "search", "automatic") as run:
        run.site(1, status="received", candidates=1, attempted=False)
        run.site(2, status="failed", attempted=True)
    assert saved[-1].status == "empty" and saved[-1].counts["requested_sites"] == 1
    assert "1 个站点" in saved[-1].details["reason"]


def test_queued_rules_are_replaced_by_actual_execution_rules():
    """
    入队和执行之间的编辑必须使用真正执行时的条件，原请求时间仍保留。
    """
    subscribe = _snapshot()
    queued = queued_activity(replace(subscribe, resolution="1080p"), "task-queued", "new", None)
    saved = []
    service = SubscriptionActivityService(SimpleNamespace(save=saved.append, get=lambda *_args: queued))
    with service.scope(subscribe, "search", "manual", "task-queued"):
        pass
    assert saved[-1].started_at == queued.started_at
    assert saved[-1].details["rules"]["resolution"] == "2160p"
    assert saved[-1].details["attempts"][0]["rules"]["resolution"] == "2160p"


def test_refresh_failure_is_visible_without_claiming_a_match(monkeypatch):
    """
    候选刷新失败会记录业务失败，暂停和其他媒体类型不会被误记为已检查。
    """
    from app.chain.subscribe.refresh import SubscribeRefreshOwner

    saved = []
    service = SubscriptionActivityService(SimpleNamespace(save=saved.append))
    owner = object.__new__(SubscribeRefreshOwner)
    owner.subscription_activity = service
    owner.subscription_repository = SimpleNamespace(list=lambda: [
        replace(_snapshot(), state="R", type="电视剧"),
        replace(_snapshot(), id=2, state="S", type="电视剧"),
        replace(_snapshot(), id=3, state="R", type="电影"),
    ])
    owner.get_subscribed_sites = lambda _type: [1]
    owner.has_music_subscribe = lambda _type: False

    def fail_refresh(*_args, **_kwargs):
        """
        模拟刷新未完成，未调用外部站点。
        """
        raise RuntimeError("offline source failed")

    monkeypatch.setattr("app.chain.subscribe.refresh.TorrentsChain", lambda: SimpleNamespace(refresh=fail_refresh))
    with pytest.raises(RuntimeError, match="offline source"):
        owner.refresh(mtype="电视剧", source="manual")
    assert len(saved) == 2 and saved[-1].source == "manual"
    assert saved[-1].status == "failed" and saved[-1].details["phase"] == "refresh_failed"
    assert saved[-1].counts["related"] == saved[-1].counts["submitted"] == 0


def test_bounded_details_keep_actual_download_receipts():
    """
    大量拒绝候选不能挤掉后出现的下载回执，同时完整相关资源计数不丢失。
    """
    run = SubscriptionActivityRun(_snapshot(), "search", "manual")
    for index in range(130):
        run.resource(_resource(f"Rejected.{index}"), "rejected", "不符合条件")
    run.resource(_resource("Accepted.S01E09"), "submitted")
    snapshot = run.snapshot()
    assert snapshot.counts["related"] == 131 and snapshot.counts["submitted"] == 1
    assert len(snapshot.details["resources"]) == 100
    assert snapshot.details["resources"][0]["title"] == "Accepted.S01E09"
    assert snapshot.details["resources_omitted"] == 31


def test_resumed_site_request_keeps_the_original_planned_denominator():
    """
    七站点已请求、一个等待的搜索恢复后变为八比八，不能变为八比一。
    """
    stored = {}

    def save(event):
        """
        按固定任务身份持久化当前快照。
        """
        stored[event.event_id] = event

    subscribe = _snapshot()
    service = SubscriptionActivityService(SimpleNamespace(save=save, get=lambda *_args: stored.get("resumed-sites")))
    with pytest.raises(SubscriptionSearchDeferred):
        with service.scope(subscribe, "search", "automatic", "resumed-sites") as run:
            run.plan_sites(range(1, 9))
            for site_id in range(1, 8):
                run.site(site_id, candidates=1)
            run.site(8, status="waiting")
            raise SubscriptionSearchDeferred(retry_at="2026-10-12T10:50:00Z", site_ids=(8,))
    assert stored["resumed-sites"].counts["requested_sites"] == 7
    with service.scope(subscribe, "search", "automatic", "resumed-sites") as run:
        run.plan_sites((8,))
        run.site(8, candidates=1)
    event = stored["resumed-sites"]
    assert event.counts["sites"] == event.counts["requested_sites"] == 8
    assert event.counts["discovered"] == 8 and event.counts["waiting_sites"] == 0
