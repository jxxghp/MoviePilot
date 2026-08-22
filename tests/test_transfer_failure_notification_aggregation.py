from unittest.mock import Mock

from app.chain import transfer as transfer_module
from app.chain.transfer import TransferChain
from app.application.transfer import (
    TransferFailureNotification,
    TransferFailureNotificationAggregator,
    TransferTask,
    build_transfer_failure_group_key,
)
from app.domain.context import MediaInfo
from app.domain.metainfo import MetaInfo
from app.runtime.config import ConfigModel
from app.schemas.file import FileItem
from app.schemas.transfer import TransferInfo
from app.schemas.types import MediaSource, MediaType


class _Timer:
    """记录静默窗口是否因新失败到达而取消。"""

    def __init__(self, callback, args):
        """保存定时回调与参数。"""
        self.callback = callback
        self.args = args
        self.cancelled = False

    def cancel(self):
        """标记当前定时器已取消。"""
        self.cancelled = True


class _Loop:
    """同步执行线程安全入队，并保留延迟回调供测试触发。"""

    def __init__(self):
        """初始化定时器清单。"""
        self.timers = []

    def call_soon_threadsafe(self, callback, *args):
        """同步执行本应投递到事件循环的回调。"""
        callback(*args)

    def call_later(self, _delay, callback, *args):
        """保存延迟回调并返回可取消句柄。"""
        timer = _Timer(callback, args)
        self.timers.append(timer)
        return timer


def _task(*, episode: int, download_hash: str = "hash-1") -> TransferTask:
    """构造同一媒体不同剧集的整理任务。"""
    return TransferTask(
        fileitem=FileItem(
            storage="local",
            path=f"/downloads/Show/Show.S01E{episode:02d}.mkv",
            type="file",
            name=f"Show.S01E{episode:02d}.mkv",
        ),
        meta=MetaInfo(f"Show S01E{episode:02d}"),
        mediainfo=MediaInfo(
            media_source=MediaSource.TMDB,
            media_id="100",
            tmdb_id=100,
            title="测试剧",
            type=MediaType.TV,
            year="2026",
        ),
        download_hash=download_hash,
        username="tester",
    )


def test_failure_group_key_prefers_media_identity_and_season():
    """同一媒体同一季应跨文件共享分组键。"""
    first = build_transfer_failure_group_key(_task(episode=1, download_hash="hash-a"))
    second = build_transfer_failure_group_key(_task(episode=2, download_hash="hash-b"))

    assert first == second
    assert first == "media:themoviedb:100:season:1:user:tester"


def test_failure_notification_aggregation_defaults_on():
    """整理失败通知聚合默认开启。"""
    field = ConfigModel.model_fields["TRANSFER_FAILURE_NOTIFICATION_AGGREGATION"]

    assert field.default is True


def test_aggregator_debounces_same_group_and_flushes_once():
    """同组失败应重置定时器并一次性回调全部快照。"""
    loop = _Loop()
    aggregator = TransferFailureNotificationAggregator()
    callback = Mock()
    notices = [
        TransferFailureNotification("测试剧 (2026)", "S01E01", "原因A", 1, None, "tester"),
        TransferFailureNotification("测试剧 (2026)", "S01E02", "原因B", 2, None, "tester"),
    ]

    for notice in notices:
        aggregator.schedule(
            group_key="media:test",
            notification=notice,
            callback=callback,
            loop=loop,
        )

    assert loop.timers[0].cancelled is True
    assert loop.timers[1].cancelled is False
    loop.timers[1].callback(*loop.timers[1].args)
    callback.assert_called_once_with(notices)


def test_aggregated_message_contains_count_reason_stats_and_batch_entry():
    """聚合消息应给出失败数、原因统计、历史 ID 和批量处理入口。"""
    chain = object.__new__(TransferChain)
    sent = []
    chain.post_message = sent.append
    notices = [
        TransferFailureNotification("测试剧 (2026)", "S01E01", "未识别到媒体信息", 11, None, "tester"),
        TransferFailureNotification("测试剧 (2026)", "S01E02", "目标已存在", 12, None, "tester"),
        TransferFailureNotification("测试剧 (2026)", "S01E03", "目标已存在", 13, None, "tester"),
    ]

    chain._send_transfer_failure_notifications(notices)

    assert len(sent) == 1
    message = sent[0]
    assert message.title == "测试剧 (2026) 入库失败（3 个文件）"
    assert "失败文件：3 个" in message.text
    assert "- 目标已存在 × 2" in message.text
    assert "整理记录：#11、#12、#13" in message.text
    assert message.buttons == [[{
        "text": "批量处理",
        "url": transfer_module.settings.MP_DOMAIN("#/history"),
    }]]


def test_enabled_queue_uses_shared_group_key(monkeypatch):
    """开启聚合后公开通知入口应投递到聚合器而不是立即发送。"""
    chain = object.__new__(TransferChain)
    chain.failure_notification_aggregator = Mock()
    chain.post_message = Mock()
    task = _task(episode=1)
    transferinfo = TransferInfo(
        success=False,
        fileitem=task.fileitem,
        message="整理失败",
        transfer_type="copy",
    )
    loop = transfer_module.global_vars.loop
    monkeypatch.setattr(
        transfer_module.settings,
        "TRANSFER_FAILURE_NOTIFICATION_AGGREGATION",
        True,
    )
    chain.queue_failed_transfer_notification(
        task=task,
        transferinfo=transferinfo,
        history_id=22,
    )

    chain.failure_notification_aggregator.schedule.assert_called_once()
    kwargs = chain.failure_notification_aggregator.schedule.call_args.kwargs
    assert kwargs["group_key"] == build_transfer_failure_group_key(task)
    assert kwargs["loop"] is loop
    chain.post_message.assert_not_called()
