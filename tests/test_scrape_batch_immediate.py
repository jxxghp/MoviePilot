"""
批次聚合关闭后按文件立即发送刮削事件的回归测试。

对应 issue #7415：批次聚合导致 jpg 晚于视频文件生成，实时监听媒体库的
媒体服务器读不到海报。默认（TRANSFER_SCRAPE_BATCH_AGGREGATION 未设置）
应逐任务立即发送，不再登记批次汇总目标；显式开启环境变量时恢复
“整批结束后统一发送”的行为。
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.chain.transfer.scrape import (
    ScrapeBatchMixin,
    _scrape_batch_aggregation_enabled,
)
from app.schemas.types import EventType


class ScrapeHost(ScrapeBatchMixin):
    """只带批次状态和事件/通知接收者的测试宿主。"""

    def __init__(self) -> None:
        self._scrape_batches = {}
        self.eventmanager = MagicMock()
        self.success_notification_aggregator = MagicMock()
        self.notify_successful_transfer = MagicMock()

    @staticmethod
    def _is_primary_media_file(fileitem, mediainfo) -> bool:
        return True


@pytest.fixture()
def host() -> ScrapeHost:
    """构造带独立批次状态的宿主。"""
    return ScrapeHost()


def _task(path: str, batch_id: str | None = "batch-1"):
    return SimpleNamespace(
        transfer_batch_id=batch_id,
        fileitem=SimpleNamespace(path=path),
        meta=SimpleNamespace(title="Show"),
        mediainfo=SimpleNamespace(),
    )


def _info(target_path: str, file_path: str, need_scrape: bool = True):
    return SimpleNamespace(
        need_scrape=need_scrape,
        target_diritem=SimpleNamespace(storage="local", path=target_path),
        file_list_new=[file_path],
    )


def test_flag_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """环境变量默认是“立即发送”，1/true/yes/on 时恢复聚合模式。"""
    monkeypatch.delenv("TRANSFER_SCRAPE_BATCH_AGGREGATION", raising=False)
    assert _scrape_batch_aggregation_enabled() is False

    for value in ("1", "true", " YES "):
        monkeypatch.setenv("TRANSFER_SCRAPE_BATCH_AGGREGATION", value)
        assert _scrape_batch_aggregation_enabled() is True

    monkeypatch.setenv("TRANSFER_SCRAPE_BATCH_AGGREGATION", "0")
    assert _scrape_batch_aggregation_enabled() is False


def test_batch_task_scrapes_immediately_by_default(
    host: ScrapeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    """默认关闭批次聚合：批次任务在完成时也应立即发送刮削事件。"""
    monkeypatch.delenv("TRANSFER_SCRAPE_BATCH_AGGREGATION", raising=False)
    task = _task("/downloads/e1.mkv")
    assert host._should_scrape_immediately(task) is True

    host._send_metadata_scrape_event(task, _info("/tv/Show", "/tv/Show/e1.mkv"))
    host.eventmanager.send_event.assert_called_once()
    event_type, payload = host.eventmanager.send_event.call_args.args
    assert event_type is EventType.MetadataScrape
    assert payload["file_list"] == ["/tv/Show/e1.mkv"]


def test_batch_task_waits_for_aggregation_when_enabled(
    host: ScrapeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    """开启环境变量后批次任务恢复聚合：完成时不立即发送。"""
    monkeypatch.setenv("TRANSFER_SCRAPE_BATCH_AGGREGATION", "1")
    task = _task("/downloads/e1.mkv")
    assert host._should_scrape_immediately(task) is False


def test_unbatched_task_always_scrapes_immediately(
    host: ScrapeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    """非批次任务在任何模式下都立即发送。"""
    monkeypatch.setenv("TRANSFER_SCRAPE_BATCH_AGGREGATION", "1")
    assert host._should_scrape_immediately(_task("/downloads/e1.mkv", None)) is True
    monkeypatch.delenv("TRANSFER_SCRAPE_BATCH_AGGREGATION", raising=False)
    assert host._should_scrape_immediately(_task("/downloads/e1.mkv", None)) is True


def test_same_directory_files_each_send_immediately(
    host: ScrapeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一目录的多集在立即模式下各自带自己的文件列表发送。"""
    monkeypatch.delenv("TRANSFER_SCRAPE_BATCH_AGGREGATION", raising=False)
    first, second = _task("/downloads/e1.mkv"), _task("/downloads/e2.mkv")
    host._register_scrape_batch_task(first)
    host._register_scrape_batch_task(second)

    host._send_metadata_scrape_event(first, _info("/tv/Show", "/tv/Show/e1.mkv"))
    host._send_metadata_scrape_event(second, _info("/tv/Show", "/tv/Show/e2.mkv"))

    calls = host.eventmanager.send_event.call_args_list
    assert [call.args[0] for call in calls] == [EventType.MetadataScrape] * 2
    assert [call.args[1]["file_list"] for call in calls] == [
        ["/tv/Show/e1.mkv"],
        ["/tv/Show/e2.mkv"],
    ]
    # 立即模式下不登记批次汇总目标，flush 不会重复发送。
    assert host._scrape_batches["batch-1"]["targets"] == {}


def test_immediate_mode_records_no_batch_target(
    host: ScrapeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    """立即模式下 _record_scrape_target 不登记目标，批次收尾不补发。"""
    monkeypatch.delenv("TRANSFER_SCRAPE_BATCH_AGGREGATION", raising=False)
    task = _task("/downloads/e1.mkv")
    host._register_scrape_batch_task(task)
    host._record_scrape_target(task, _info("/tv/Show", "/tv/Show/e1.mkv"))
    host._close_scrape_batch("batch-1")
    host._finish_scrape_batch_task(task)

    host.eventmanager.send_event.assert_not_called()
    assert not host._scrape_batches


def test_aggregation_mode_merges_into_one_event(
    host: ScrapeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    """环境变量为 1 时保持既有行为：批次收尾后按目录合并为一次。"""
    monkeypatch.setenv("TRANSFER_SCRAPE_BATCH_AGGREGATION", "1")
    first, second = _task("/downloads/e1.mkv"), _task("/downloads/e2.mkv")
    host._register_scrape_batch_task(first)
    host._register_scrape_batch_task(second)
    host._record_scrape_target(first, _info("/tv/Show", "/tv/Show/e1.mkv"))
    host._record_scrape_target(second, _info("/tv/Show", "/tv/Show/e2.mkv"))

    host._finish_scrape_batch_task(first)
    host._finish_scrape_batch_task(second)
    host.eventmanager.send_event.assert_not_called()

    host._close_scrape_batch("batch-1")
    host.eventmanager.send_event.assert_called_once()
    _event_type, payload = host.eventmanager.send_event.call_args.args
    assert payload["file_list"] == ["/tv/Show/e1.mkv", "/tv/Show/e2.mkv"]
    host.success_notification_aggregator.release_batch.assert_called_once_with(
        "batch-1", loop=None
    )


def test_notification_still_called_per_task(
    host: ScrapeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    """立即模式下成功通知的聚合调用仍按任务保留。"""
    monkeypatch.delenv("TRANSFER_SCRAPE_BATCH_AGGREGATION", raising=False)
    task = _task("/downloads/e1.mkv")
    host._register_scrape_batch_task(task)
    host._send_metadata_scrape_event(task, _info("/tv/Show", "/tv/Show/e1.mkv"))
    host.notify_successful_transfer(task, _info("/tv/Show", "/tv/Show/e1.mkv"))
    assert host.notify_successful_transfer.call_count == 1
