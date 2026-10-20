"""
订阅下载来源、实际选集、通知和持久历史的回归测试。
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.application.chain.events import restore_download_processing, snapshot_download_processing
from app.application.download.admission import subscription_download_label
from app.application.subscription.activity import SubscriptionActivityRun
from app.application.subscription.contract import SubscriptionSnapshot
from app.application.subscription.priority import annotate_subscription_download
from app.application.subscription.status import SubscriptionExecutionStatusService
from app.chain.download.history import DownloadHistoryOwner
from app.chain.download.processing import DownloadProcessingOwner
from app.db.adapters.subscriptionstatus import SessionSubscriptionExecutionStatusRepository
from app.db.base import Base
from app.db.models.subscribeactivity import SubscribeActivity
from app.db.models.subscriptionsearch import SubscriptionSearchBatch, SubscriptionSearchTask
from app.domain.context import Context, MediaInfo, TorrentInfo
from app.domain.metainfo import MetaInfo
from app.schemas.message import Message
from app.schemas.types import MediaType


def _subscription(**values):
    """
    构造开启分集洗版且包含两集目标范围的订阅。
    """
    return SubscriptionSnapshot(id=17, name="Demo", type="电视剧", season=1,
                                total_episode=2, best_version=1, activity_key="activity", **values)


def _context(selected=None):
    """
    构造最终下载选集与原始种子范围不同的候选。
    """
    return Context(meta_info=MetaInfo("Demo.S01E01-E02"), media_info=MediaInfo(type=MediaType.TV, title="Demo"),
                   torrent_info=TorrentInfo(site=1, title="Demo.S01E01-E02", site_name="Site", enclosure="private"),
                   selected_episodes=selected)


@pytest.mark.parametrize(("values", "selected", "kind", "initial", "upgrade"), [
    ({}, [1], "initial", [1], []),
    ({"note": [1]}, [2], "initial", [2], []),
    ({"episode_priority": {"1": 50}}, [1], "upgrade", [], [1]),
    ({"note": [1]}, [1, 2], "upgrade", [2], [1]),
    ({"current_priority": 50}, [2], "upgrade", [], [2]),
    ({"best_version_full": 1}, [], "initial", [1, 2], []),
    ({"best_version_full": 1, "current_priority": 50}, [], "upgrade", [], [1, 2]),
    ({"best_version_full": 1, "note": [1]}, None, "upgrade", [2], [1]),
])
def test_submission_type_uses_selected_episode_facts(values, selected, kind, initial, upgrade):
    """
    洗版开关本身不构成升级，拆包与整包混合下载都保留真实集数来源。
    """
    context = _context(selected)
    annotate_subscription_download(_subscription(**values), context)
    assert context.subscription_download == {
        "subscription_id": 17, "submission_type": kind, "best_version_full": bool(values.get("best_version_full")),
        "initial_episodes": initial, "upgrade_episodes": upgrade,
    }


@pytest.mark.parametrize("media_type", ["电影", "音乐"])
@pytest.mark.parametrize("priority", [None, 0, 50])
def test_non_tv_submission_uses_existing_version(media_type, priority):
    """
    电影和音乐也区分首次订阅与已有版本升级，零基线不冒充升级事实。
    """
    context = _context([])
    subscribe = SubscriptionSnapshot(id=17, name="Demo", type=media_type, best_version=1, current_priority=priority)
    annotate_subscription_download(subscribe, context)
    assert context.subscription_download["submission_type"] == ("upgrade" if priority else "initial")


def test_activity_counts_survive_retry_and_bounded_details():
    """
    成功提交按稳定资源去重，失败不增加分类计数，明细截断与恢复不丢失累计统计。
    """
    run = SubscriptionActivityRun(_subscription(note=[1]), "search", "manual", "task")
    for number in range(105):
        context = _context([1] if number % 2 else [2])
        context.torrent_info.title = f"Demo-{number}"
        annotate_subscription_download(_subscription(note=[1]), context)
        run.resource(context, "submitted")
        run.resource(context, "submitted")
    run.resource(_context([1]), "failed", "下载器拒绝")
    restored = SubscriptionActivityRun(_subscription(), "search", "manual", "task")
    restored.restore(run.snapshot())
    assert restored.counts["submitted"] == 105
    assert restored.counts["initial_submitted"] == 53
    assert restored.counts["upgrade_submitted"] == 52
    assert len(restored.details["resources"]) == 100
    context = _context([2])
    context.torrent_info.title = "Demo-104"
    annotate_subscription_download(_subscription(), context)
    restored.resource(context, "submitted")
    assert restored.counts["submitted"] == 105
    assert "private" not in str(restored.snapshot().details)


@pytest.mark.parametrize(("kind", "full", "label"), [
    ("initial", False, "首次订阅"), ("upgrade", False, "洗版升级"), ("upgrade", True, "全集洗版"),
])
@pytest.mark.parametrize("title", ["开始下载 Demo", "", None])
def test_history_and_processing_keep_source_without_changing_template(monkeypatch, kind, full, label, title):
    """
    下载历史与可恢复上下文保留来源，冻结通知的标题和正文保持模板原样。
    """
    context = _context([1])
    context.subscription_download = {"subscription_id": 17, "submission_type": kind, "best_version_full": full,
                                     "initial_episodes": [], "upgrade_episodes": [1]}
    assert subscription_download_label(context.subscription_download) == label
    chain = DownloadHistoryOwner.__new__(DownloadHistoryOwner)
    chain.durable_event_writer = MagicMock()
    chain._build_download_notification = DownloadProcessingOwner._build_download_notification.__get__(chain)
    chain.runtime_config = SimpleNamespace(media_extensions=(".mkv",), downloading_url="/downloading")
    monkeypatch.setattr("app.application.messaging.message.MessageTemplateHelper.render",
                        lambda **_kwargs: Message(title=title, text="自定义模板内容"))
    chain._settle_download_success(context=context, media=context.media_info, meta=context.meta_info,
                                   torrent=context.torrent_info, folder_name="Demo", file_list=[],
                                   download_dir=Path("/downloads"), layout=None, downloader="qb", download_hash="hash",
                                   download_episodes="E01", episodes={1}, channel=None, source="订阅 17",
                                   userid=None, username="owner", torrent_content=b"torrent", custom_words=None)
    kwargs = chain.durable_event_writer.download_added.call_args.kwargs
    assert kwargs["history"].note["subscription_download"] == context.subscription_download
    assert kwargs["notification_payload"]["message"]["title"] == title
    assert kwargs["notification_payload"]["message"]["text"] == "自定义模板内容"
    restored = restore_download_processing(kwargs["processing_payload"])
    assert restored.context.subscription_download == context.subscription_download
    assert restored.context.selected_episodes == [1]


def test_manual_notification_and_old_context_keep_no_source(monkeypatch):
    """
    手动下载与旧快照不新增标签，也不根据历史订阅开关补造类型。
    """
    chain = DownloadProcessingOwner.__new__(DownloadProcessingOwner)
    chain.runtime_config = SimpleNamespace(downloading_url="/downloading")
    monkeypatch.setattr("app.application.messaging.message.MessageTemplateHelper.render",
                        lambda **_kwargs: Message(title="开始下载 Demo"))
    context = _context()
    message = chain._build_download_notification(media=context.media_info, meta=context.meta_info,
                                                 torrent=context.torrent_info, channel=None, source=None,
                                                 userid=None, username=None, download_episodes=None)
    assert message.title == "开始下载 Demo"
    assert subscription_download_label(None) is None
    assert subscription_download_label({"submission_type": "unknown"}) is None
    payload = snapshot_download_processing(context=context, download_dir=Path("/downloads"),
                                           torrent_content=b"torrent", download_hash="hash", downloader="qb")
    assert restore_download_processing(payload).context.subscription_download is None


def test_batch_reads_persisted_activity_counts_and_safe_items(tmp_path):
    """
    真实异步数据库投影按 task_id 关联活动，列表和详情计数一致并保留完整权限检查。
    """
    async def verify():
        """
        在独立临时库中构造完成任务及其活动，验证批次读取而不接触运行数据库。
        """
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'batch.db'}")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                batch_id, task_id = uuid4().hex, uuid4().hex
                session.add(SubscriptionSearchBatch(batch_id=batch_id, source="manual", state="completed", priority=1,
                                                    total_count=1, finished_count=1, failed_count=0, cancelled_count=0,
                                                    skipped_count=0, cancel_requested=False, created_at="now", updated_at="now"))
                session.add(SubscriptionSearchTask(task_id=task_id, batch_id=batch_id, subscription_id=17, source="manual",
                                                   priority=1, position=0, state="completed", phase="completed",
                                                   attempt_count=1, cancel_requested=False, created_at="now", updated_at="now"))
                run = SubscriptionActivityRun(_subscription(note=[1], best_version_full=1), "search", "manual", task_id)
                context = _context([1, 2])
                annotate_subscription_download(run.subscribe, context)
                run.resource(context, "submitted")
                event = run.snapshot()
                session.add(SubscribeActivity(activity_key="activity", event_id=task_id, kind="search", source="manual",
                                              status="completed", started_at="now", counts=event.counts, details=event.details))
                await session.commit()
                service = SubscriptionExecutionStatusService(SessionSubscriptionExecutionStatusRepository(session))
                assert await service.get_batch(batch_id, accessible_subscription_ids={18}) is None
                details = await service.get_batch(batch_id, accessible_subscription_ids={17})
                listing = await service.list_batches(accessible_subscription_ids={17})
                assert listing[0] == details
                assert details.download_counts["initial_submitted"] == 0
                assert details.download_counts["upgrade_submitted"] == 1
                assert details.download_counts["full_upgrade_submitted"] == 1
                assert details.downloads[0]["subscription_download"] == context.subscription_download
                assert details.downloads_omitted == 0
                assert "private" not in str(details)
        finally:
            await engine.dispose()
    asyncio.run(verify())
