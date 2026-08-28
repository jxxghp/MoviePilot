from collections.abc import Callable
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app.application.transfer.workflow import (
    TransferFailureNotification,
    TransferFailureNotificationAggregator,
    TransferTask,
    build_transfer_failure_group_key,
)
from app.chain.transfer import TransferChain
from app.domain.context import MediaInfo
from app.domain.metainfo import MetaInfo
from app.runtime.config import ConfigModel
from app.runtime.loop import main_loop_registry
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

    @staticmethod
    def is_running() -> bool:
        """模拟可接收任务的运行中事件循环。"""
        return True

    @staticmethod
    def is_closed() -> bool:
        """模拟尚未关闭的事件循环。"""
        return False

    def call_later(self, _delay, callback, *args):
        """保存延迟回调并返回可取消句柄。"""
        timer = _Timer(callback, args)
        self.timers.append(timer)
        return timer


@pytest.fixture
def replace_main_loop() -> Callable[[object], None]:
    """临时替换主循环登记，并在用例结束后恢复原值。"""
    original = main_loop_registry.current
    try:
        yield main_loop_registry.replace_compat
    finally:
        main_loop_registry.replace_compat(original)

    @staticmethod
    def is_running() -> bool:
        """该替身代表由生命周期持有的运行中循环。"""
        return True

    @staticmethod
    def is_closed() -> bool:
        """该替身在用例期间保持可用。"""
        return False


class _DeferredLoop(_Loop):
    """延迟执行线程安全回调，用于覆盖关闭与入环之间的竞态。"""

    def __init__(self):
        """初始化延迟回调和定时器清单。"""
        super().__init__()
        self.soon_callbacks = []

    def call_soon_threadsafe(self, callback, *args):
        """保存线程安全回调，直到测试显式执行。"""
        self.soon_callbacks.append((callback, args))

    def run_soon_callbacks(self):
        """执行并清空已保存的线程安全回调。"""
        callbacks = list(self.soon_callbacks)
        self.soon_callbacks.clear()
        for callback, args in callbacks:
            callback(*args)


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


def test_aggregator_old_timer_cannot_flush_before_renewal_is_armed():
    """新通知已接收时，旧 timer 不得抢在事件循环重置静默窗前发送。"""
    loop = _DeferredLoop()
    aggregator = TransferFailureNotificationAggregator()
    callback = Mock()
    first = TransferFailureNotification(
        "测试剧 (2026)", "S01E01", "原因A", 1, None, "tester"
    )
    second = TransferFailureNotification(
        "测试剧 (2026)", "S01E02", "原因B", 2, None, "tester"
    )

    aggregator.schedule(
        group_key="media:test",
        notification=first,
        callback=callback,
        loop=loop,
    )
    loop.run_soon_callbacks()
    old_timer = loop.timers[0]

    aggregator.schedule(
        group_key="media:test",
        notification=second,
        callback=callback,
        loop=loop,
    )
    old_timer.callback(*old_timer.args)
    callback.assert_not_called()

    loop.run_soon_callbacks()
    renewed_timer = loop.timers[1]
    assert old_timer.cancelled is True
    renewed_timer.callback(*renewed_timer.args)
    callback.assert_called_once_with([first, second])


def test_aggregator_close_flushes_accepted_notification_before_timer_is_armed():
    """关闭应发送已接收但尚未进入事件循环的通知，且延迟回调不能重新建 timer。"""
    loop = _DeferredLoop()
    aggregator = TransferFailureNotificationAggregator()
    callback = Mock()
    notice = TransferFailureNotification(
        "测试剧 (2026)", "S01E01", "原因A", 1, None, "tester"
    )

    aggregator.schedule(
        group_key="media:test",
        notification=notice,
        callback=callback,
        loop=loop,
    )
    aggregator.close()
    aggregator.close()
    loop.run_soon_callbacks()

    callback.assert_called_once_with([notice])
    assert loop.timers == []


def test_aggregator_close_cancels_timer_and_rejects_new_notification():
    """关闭应取消已建 timer，并让调用方明确感知后续投递被拒绝。"""
    loop = _Loop()
    aggregator = TransferFailureNotificationAggregator()
    callback = Mock()
    notice = TransferFailureNotification(
        "测试剧 (2026)", "S01E01", "原因A", 1, None, "tester"
    )
    aggregator.schedule(
        group_key="media:test",
        notification=notice,
        callback=callback,
        loop=loop,
    )

    aggregator.close()

    assert loop.timers[0].cancelled is True
    callback.assert_called_once_with([notice])
    with pytest.raises(RuntimeError, match="正在关闭"):
        aggregator.schedule(
            group_key="media:test",
            notification=notice,
            callback=callback,
            loop=loop,
        )


def test_aggregator_close_observes_flush_callback_error():
    """关闭阶段同步刷新失败时应记录异常而不是让通知静默丢失。"""
    loop = _DeferredLoop()
    aggregator = TransferFailureNotificationAggregator()
    callback = Mock(side_effect=RuntimeError("send failed"))
    notice = TransferFailureNotification(
        "测试剧 (2026)", "S01E01", "原因A", 1, None, "tester"
    )
    aggregator.schedule(
        group_key="media:test",
        notification=notice,
        callback=callback,
        loop=loop,
    )

    with patch("app.application.transfer.workflow.logger.error") as log_error:
        aggregator.close()

    callback.assert_called_once_with([notice])
    log_error.assert_called_once()


def test_aggregated_message_contains_count_reason_stats_and_batch_entry():
    """聚合消息应给出失败数、原因统计、历史 ID 和批量处理入口。"""
    chain = object.__new__(TransferChain)
    chain.runtime_config = SimpleNamespace(history_url="#/history")
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
        "url": "#/history",
    }]]


def test_enabled_queue_uses_shared_group_key(replace_main_loop):
    """开启聚合后公开通知入口应投递到聚合器而不是立即发送。"""
    chain = object.__new__(TransferChain)
    chain.runtime_config = SimpleNamespace(
        transfer_failure_notification_aggregation=True,
    )
    chain.failure_notification_aggregator = Mock()
    chain.post_message = Mock()
    task = _task(episode=1)
    transferinfo = TransferInfo(
        success=False,
        fileitem=task.fileitem,
        message="整理失败",
        transfer_type="copy",
    )
    loop = _Loop()
    replace_main_loop(loop)
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
