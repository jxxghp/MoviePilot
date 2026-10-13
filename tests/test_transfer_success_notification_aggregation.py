"""成功通知按明确批次或静默窗口聚合，且不改变逐文件结算副作用。"""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.application.configuration import ChainRuntimeConfig
from app.application.transfer.execution import TransferExecutionCheckpoint, TransferSettlementResult
from app.application.transfer.jobs import JobManager
from app.application.transfer.models import TransferTask
from app.application.transfer.notifications import (
    TransferNotificationAggregator,
    TransferSuccessNotification,
    build_transfer_success_group_key,
)
from app.chain.transfer.facade import TransferChain
from app.domain.context import MediaInfo, MusicInfo
from app.domain.meta.metamusic import MetaMusic
from app.domain.metainfo import MetaInfo
from app.runtime.config import ConfigModel
from app.runtime.loop import main_loop_registry
from app.schemas.file import FileItem
from app.schemas.transfer import TransferInfo
from app.schemas.types import ContentType, EventType, MediaType, MessageType


class _Loop:
    """记录静默窗口而不创建真实后台任务。"""

    def __init__(self):
        """初始化可手动触发的定时回调。"""
        self.timers = []

    def call_soon_threadsafe(self, callback, *args):
        """同步登记事件循环回调。"""
        callback(*args)

    def call_later(self, _delay, callback, *args):
        """保存定时器并提供可取消的句柄。"""
        timer = SimpleNamespace(callback=callback, args=args, cancel=Mock())
        self.timers.append(timer)
        return timer

    def flush(self):
        """按登记顺序触发定时器，包含已取消回调以验证代际保护。"""
        for timer in self.timers:
            timer.callback(*timer.args)


def _task(number=1, *, batch="batch", username="tester", download_hash="hash"):
    """构造同一专辑内具有不同录音身份的分轨整理任务。"""
    return TransferTask(
        fileitem=FileItem(storage="local", path=f"/downloads/Album/{number}.flac", name=f"{number}.flac", extension="flac", type="file", size=100),
        meta=MetaMusic(title=f"Track {number}", album="Album", track_number=number),
        mediainfo=MusicInfo(title=f"Track {number}", album="Album", artists=["Artist"], year=2013,
                            media_source="musicbrainz", media_id=f"recording-{number}", total_tracks=74),
        target_path=Path("/library/Album"), transfer_batch_id=batch,
        download_hash=download_hash, username=username, background=True,
    )


def _result(number=1):
    """提供包含真实目标路径的成功结果。"""
    path = f"/library/Album/{number}.flac"
    return TransferInfo(
        target_item=FileItem(storage="local", path=path, type="file"),
        target_diritem=FileItem(storage="local", path="/library/Album", type="dir"),
        file_list_new=[path], file_count=1, total_size=100, need_notify=True,
    )


@pytest.fixture
def chain():
    """构造没有线程、数据库和真实消息通道的通知宿主。"""
    owner = object.__new__(TransferChain)
    owner.runtime_config = ChainRuntimeConfig(media_extensions=(".flac", ".mkv"), history_url="https://example.test/history")
    owner.success_notification_aggregator = TransferNotificationAggregator[TransferSuccessNotification]()
    owner.post_message = Mock()
    owner._scrape_batches = {}
    owner._audio_exts = [".flac"]
    owner._media_exts = [".mkv"]
    owner._subtitle_exts = [".srt"]
    owner.eventmanager = Mock()
    yield owner
    owner.success_notification_aggregator.close()


def test_success_notification_aggregation_defaults_on():
    """成功侧默认值与失败侧一致。"""
    assert ConfigModel.model_fields["TRANSFER_SUCCESS_NOTIFICATION_AGGREGATION"].default is True


@pytest.mark.parametrize("change", ["username", "download_hash", "downloader", "destination", "storage"])
def test_success_group_isolates_source_destination_and_user(change):
    """不同用户、种子、下载器、目标或存储不能混入同一通知。"""
    first, second = _task(), _task(2)
    result = _result()
    modified = _result(2)
    if change == "destination":
        modified.target_diritem.path = "/library/Other Album"
    elif change == "storage":
        modified.target_diritem.storage = "smb"
    else:
        setattr(second, change, "other")
    assert build_transfer_success_group_key(first, result) != build_transfer_success_group_key(second, modified)


def test_all_74_slow_tracks_wait_for_batch_and_send_once(chain):
    """曲目间隔超过静默窗也不拆批，统计真实文件并以专辑名发送。"""
    tasks = [_task(number) for number in range(1, 75)]
    for task in tasks:
        chain._register_scrape_batch_task(task)
    chain._close_scrape_batch("batch")
    for number, task in enumerate(tasks, start=1):
        result = _result(number)
        chain.queue_successful_transfer_notification(task, result)
        # 模拟旧通知计数在同一结果对象上被改写。
        result.file_count = 999
        chain._finish_scrape_batch_task(task)
        assert chain.post_message.call_count == (1 if number == 74 else 0)
    message = chain.post_message.call_args.args[0]
    assert message.title == "Album (2013)"
    assert "已入库 74 个文件 / 共 74 首曲目" in message.text
    assert message.buttons == [[{"text": "查看整理记录", "url": "https://example.test/history"}]]
    assert message.ctype == ContentType.OrganizeSuccess
    assert message.mtype == MessageType.Organize
    assert chain.post_message.call_args.kwargs["transferinfo"].total_size == 7400
    assert not chain.eventmanager.send_event.called


def test_single_file_batch_is_immediate_and_keeps_template(chain):
    """一个文件的明确批次结束即发，保留用户模板和无批量按钮行为。"""
    task = _task()
    chain._register_scrape_batch_task(task)
    chain._close_scrape_batch("batch")
    chain.queue_successful_transfer_notification(task, _result())
    chain._finish_scrape_batch_task(task)
    message = chain.post_message.call_args.args[0]
    assert message.title is None and message.text is None
    assert message.buttons is None
    assert chain.post_message.call_count == 1


def test_closed_batch_is_required_even_if_work_finishes_while_enumerating(chain):
    """目录枚举期间先完成的曲目要等登记结束，防止过早发送。"""
    task = _task()
    chain._register_scrape_batch_task(task)
    chain.queue_successful_transfer_notification(task, _result())
    chain._finish_scrape_batch_task(task)
    chain.post_message.assert_not_called()
    chain._close_scrape_batch("batch")
    chain.post_message.assert_called_once()


def test_unbatched_events_use_quiet_window_and_deduplicate_paths(chain, monkeypatch):
    """没有明确批次的外部任务使用静默窗，相同目标不会重复累计。"""
    loop = _Loop()
    monkeypatch.setattr(main_loop_registry, "require", lambda: loop)
    for number in [1, 2, 1]:
        chain.queue_successful_transfer_notification(_task(number, batch=None), _result(number))
    chain.post_message.assert_not_called()
    loop.flush()
    chain.post_message.assert_called_once()
    assert chain.post_message.call_args.kwargs["transferinfo"].file_count == 2
    assert chain.post_message.call_args.kwargs["transferinfo"].total_size == 200


def test_overlapping_batches_wait_for_both_and_shutdown_flushes_once(chain):
    """同组重叠批次不会互相提前刷新；关闭发送剩余快照且重复关闭幂等。"""
    for number, batch in [(1, "a"), (2, "b")]:
        chain.queue_successful_transfer_notification(_task(number, batch=batch), _result(number))
    chain.success_notification_aggregator.release_batch("a")
    chain.post_message.assert_not_called()
    chain.success_notification_aggregator.close()
    chain.success_notification_aggregator.close()
    chain.success_notification_aggregator.release_batch("b")
    chain.post_message.assert_called_once()
    assert "已入库 2 个文件" in chain.post_message.call_args.args[0].text


@pytest.mark.parametrize("notify,manual,background", [(False, False, True), (True, True, False)])
def test_disabled_or_foreground_manual_notifications_remain_silent(chain, notify, manual, background):
    """关闭通知及前台手动整理不进入缓冲。"""
    task = _task()
    task.manual, task.background = manual, background
    result = _result()
    result.need_notify = notify
    chain.notify_successful_transfer(task, result)
    chain.success_notification_aggregator.close()
    chain.post_message.assert_not_called()


@pytest.mark.parametrize("media_type", [MediaType.MOVIE, MediaType.TV, MediaType.MUSIC])
def test_global_disabled_keeps_legacy_notifications(chain, media_type):
    """全局关闭时成功任务不进入新的缓冲，由原有作业通知负责发送。"""
    chain.runtime_config = replace(chain.runtime_config, transfer_success_notification_aggregation=False)
    task = _task()
    if media_type != MediaType.MUSIC:
        task.meta = MetaInfo("Movie 2026" if media_type == MediaType.MOVIE else "Show S01E01")
        task.mediainfo = MediaInfo(title="Movie" if media_type == MediaType.MOVIE else "Show", year="2026", type=media_type)
    chain.notify_successful_transfer(task, _result())
    chain.success_notification_aggregator.close()
    chain.post_message.assert_not_called()


def test_movie_batch_uses_same_success_aggregation(chain):
    """电影同样在批次结束后汇总成功文件，并保持入库消息类型。"""
    for number in [1, 2]:
        task = _task(number)
        task.meta = MetaInfo("Movie 2026")
        task.mediainfo = MediaInfo(title="Movie", year="2026", type=MediaType.MOVIE)
        chain.notify_successful_transfer(task, _result(number))
    chain.post_message.assert_not_called()
    chain.success_notification_aggregator.release_batch("batch")
    message = chain.post_message.call_args.args[0]
    assert message.title == "Movie (2026)"
    assert "已入库 2 个文件" in message.text
    assert message.ctype == ContentType.OrganizeSuccess
    assert chain.post_message.call_count == 1


def test_television_batch_retains_season_episode_range(chain):
    """影视汇总保留季集范围，不把不同季混合。"""
    for number in [1, 2]:
        task = _task(number)
        task.meta = MetaInfo(f"Show S01E{number:02d}")
        task.mediainfo = MediaInfo(title="Show", year="2026", type=MediaType.TV)
        chain.queue_successful_transfer_notification(task, _result(number))
    chain.success_notification_aggregator.release_batch("batch")
    message = chain.post_message.call_args.args[0]
    assert message.title == "Show (2026) S01 E01-E02"


def test_mixed_outcomes_preserve_each_history_event_and_only_count_success(chain, monkeypatch):
    """批次成败混合时逐任务持久化和事件不变，成功汇总只计已入库文件。"""
    chain.jobview = JobManager()
    chain._success_target_files = {}
    chain.transfer_history_repository = Mock()
    chain.queue_failed_transfer_notification = Mock()
    chain._TransferChain__mark_torrent_completed_if_done = Mock()
    chain._record_scrape_target = Mock()
    chain._send_metadata_scrape_event = Mock()
    chain.durable_event_writer = Mock()
    chain.durable_event_writer.transfer_result.return_value = TransferSettlementResult(
        history_id=1, settlement_revision=1, pending_deleted=True,
    )
    monkeypatch.setattr("app.chain.transfer.settlement.record_transfer_failure", Mock())
    monkeypatch.setattr("app.chain.transfer.settlement.clear_transfer_failures", Mock())
    tasks = [_task(number) for number in [1, 2, 3]]
    for task in tasks:
        chain.jobview.add_task(task)
        chain._register_scrape_batch_task(task)
    chain._close_scrape_batch("batch")
    for number, task in enumerate(tasks, start=1):
        result = _result(number)
        if number == 3:
            result.success = False
            result.message = "测试失败"
        task.bind_admission_task_id(f"task-{number}")
        task.bind_execution_lease(owner_id="worker", lease_token=f"lease-{number}")
        task.bind_execution_checkpoint(TransferExecutionCheckpoint.create(
            payload={"outcome": "succeeded" if result.success else "failed", "transferinfo": result.model_dump(mode="json")},
            operation_ids=(f"operation-{number}",),
        ))
        chain._TransferChain__default_callback(task, result)
        chain._finish_scrape_batch_task(task)
    assert chain.durable_event_writer.transfer_result.call_count == 3
    assert chain.queue_failed_transfer_notification.call_count == 1
    assert chain._record_scrape_target.call_count == 2
    assert chain._TransferChain__mark_torrent_completed_if_done.call_count == 3
    assert "已入库 2 个文件" in chain.post_message.call_args.args[0].text
    assert chain.post_message.call_count == 1
    calls = chain.durable_event_writer.transfer_result.call_args_list
    assert [call.kwargs["topic"] for call in calls] == ["transfer.completed", "transfer.completed", "transfer.audio.failed"]
    for call in calls:
        call.kwargs["publish"]({"test": True})
    assert [call.args[0] for call in chain.eventmanager.send_event.call_args_list] == [
        EventType.TransferComplete, EventType.TransferComplete, EventType.AudioTransferFailed,
    ]


def test_old_callback_cannot_flush_recreated_group():
    """已刷新的组重新创建后，旧代回调不得发送新组快照。"""
    loop = _Loop()
    aggregator = TransferNotificationAggregator[str]()
    callback = Mock()
    aggregator.schedule(group_key="group", notification="old", callback=callback, loop=loop)
    old_timer = loop.timers[0]
    old_timer.callback(*old_timer.args)
    aggregator.schedule(group_key="group", notification="new", callback=callback, loop=loop)
    old_timer.callback(*old_timer.args)
    assert callback.call_args_list == [((['old'],), {})]
    loop.timers[1].callback(*loop.timers[1].args)
    assert callback.call_count == 2
    callback.assert_called_with(["new"])
