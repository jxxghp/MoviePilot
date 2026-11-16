"""
批次聚合关闭后按文件立即发送刮削事件的回归测试。

对应 issue #7415：批次聚合导致 jpg 晚于视频文件生成，实时监听媒体库的
媒体服务器读不到海报。默认（TRANSFER_SCRAPE_BATCH_AGGREGATION 未开启）
应逐任务立即发送，不再登记批次汇总目标；显式开启该运行时配置时恢复
“整批结束后统一发送”的行为。
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.chain.transfer.scrape import ScrapeBatchMixin
from app.schemas.file import FileItem
from app.schemas.transfer import TransferInfo
from app.schemas.types import EventType
from tests.test_transfer_job_manager import (
    FakeMedia,
    bind_terminal_checkpoint,
    make_task,
    make_transfer_chain,
)


class ScrapeHost(ScrapeBatchMixin):
    """
    只带批次状态、运行时配置和事件/通知接收者的测试宿主。
    """

    def __init__(self, batch_aggregation: bool = False) -> None:
        """
        初始化最小宿主状态。

        :param batch_aggregation: 注入的批次聚合运行时配置取值
        """
        self._scrape_batches = {}
        self.runtime_config = SimpleNamespace(
            transfer_scrape_batch_aggregation=batch_aggregation
        )
        self.eventmanager = MagicMock()
        self.success_notification_aggregator = MagicMock()
        self.notify_successful_transfer = MagicMock()

    @staticmethod
    def _is_primary_media_file(fileitem, mediainfo) -> bool:
        """
        测试宿主把所有文件都视为主媒体文件。

        :param fileitem: 待判断的文件项
        :param mediainfo: 当前媒体的识别信息
        :return: 固定返回 True
        """
        return True


@pytest.fixture()
def host() -> ScrapeHost:
    """
    构造关闭批次聚合（默认）的独立宿主。

    :return: 带独立批次状态的测试宿主
    """
    return ScrapeHost()


@pytest.fixture()
def aggregation_host() -> ScrapeHost:
    """
    构造开启批次聚合的独立宿主。

    :return: 批次聚合已开启的测试宿主
    """
    return ScrapeHost(batch_aggregation=True)


def _task(path: str, batch_id: str | None = "batch-1") -> SimpleNamespace:
    """
    构造只带路径与批次标识的最小任务替身。

    :param path: 源文件路径
    :param batch_id: 整理批次标识，None 表示非批次任务
    :return: 任务替身
    """
    return SimpleNamespace(
        transfer_batch_id=batch_id,
        fileitem=SimpleNamespace(path=path),
        meta=SimpleNamespace(title="Show"),
        mediainfo=SimpleNamespace(),
    )


def _info(
        target_path: str, file_path: str, need_scrape: bool = True
) -> SimpleNamespace:
    """
    构造只带刮削目标与新增文件的最小整理结果替身。

    :param target_path: 目标媒体根目录路径
    :param file_path: 本次整理新增的目标文件路径
    :param need_scrape: 是否需要刮削
    :return: 整理结果替身
    """
    return SimpleNamespace(
        need_scrape=need_scrape,
        target_diritem=SimpleNamespace(storage="local", path=target_path),
        file_list_new=[file_path],
    )


def test_flag_defaults_to_immediate(host: ScrapeHost) -> None:
    """
    运行时配置默认关闭批次聚合：批次任务也在完成时立即发送。
    """
    assert host._scrape_batch_aggregation_enabled() is False
    assert host._should_scrape_immediately(_task("/downloads/e1.mkv")) is True


def test_flag_follows_runtime_config(aggregation_host: ScrapeHost) -> None:
    """
    开启运行时配置后批次任务恢复聚合：完成时不立即发送。
    """
    assert aggregation_host._scrape_batch_aggregation_enabled() is True
    assert (
        aggregation_host._should_scrape_immediately(_task("/downloads/e1.mkv"))
        is False
    )


def test_unbatched_task_always_scrapes_immediately(
    host: ScrapeHost, aggregation_host: ScrapeHost
) -> None:
    """
    非批次任务在任何模式下都立即发送。
    """
    assert host._should_scrape_immediately(_task("/downloads/e1.mkv", None)) is True
    assert (
        aggregation_host._should_scrape_immediately(_task("/downloads/e1.mkv", None))
        is True
    )


def test_batch_task_scrapes_immediately_by_default(host: ScrapeHost) -> None:
    """
    默认关闭批次聚合：批次任务完成时按自身文件列表发送刮削事件。
    """
    task = _task("/downloads/e1.mkv")
    host._send_metadata_scrape_event(task, _info("/tv/show", "/tv/show/e1.mkv"))

    host.eventmanager.send_event.assert_called_once()
    event_type, payload = host.eventmanager.send_event.call_args.args
    assert event_type is EventType.MetadataScrape
    assert payload["file_list"] == ["/tv/show/e1.mkv"]


def test_same_directory_files_each_send_immediately(host: ScrapeHost) -> None:
    """
    同一目录的多集在立即模式下各自带自己的文件列表发送。
    """
    first, second = _task("/downloads/e1.mkv"), _task("/downloads/e2.mkv")
    host._register_scrape_batch_task(first)
    host._register_scrape_batch_task(second)

    host._send_metadata_scrape_event(first, _info("/tv/show", "/tv/show/e1.mkv"))
    host._send_metadata_scrape_event(second, _info("/tv/show", "/tv/show/e2.mkv"))

    calls = host.eventmanager.send_event.call_args_list
    assert [call.args[0] for call in calls] == [EventType.MetadataScrape] * 2
    assert [call.args[1]["file_list"] for call in calls] == [
        ["/tv/show/e1.mkv"],
        ["/tv/show/e2.mkv"],
    ]
    # 立即模式下不登记批次汇总目标，批次收尾不会重复发送。
    assert host._scrape_batches["batch-1"]["targets"] == {}


def test_immediate_mode_records_no_batch_target(host: ScrapeHost) -> None:
    """
    立即模式下 _record_scrape_target 不登记目标，批次收尾不补发。
    """
    task = _task("/downloads/e1.mkv")
    host._register_scrape_batch_task(task)
    host._record_scrape_target(task, _info("/tv/show", "/tv/show/e1.mkv"))
    host._close_scrape_batch("batch-1")
    host._finish_scrape_batch_task(task)

    host.eventmanager.send_event.assert_not_called()
    assert not host._scrape_batches


def test_aggregation_mode_merges_into_one_event(aggregation_host: ScrapeHost) -> None:
    """
    开启批次聚合时保持既有行为：批次收尾后按目录合并为一次。
    """
    first, second = _task("/downloads/e1.mkv"), _task("/downloads/e2.mkv")
    aggregation_host._register_scrape_batch_task(first)
    aggregation_host._register_scrape_batch_task(second)
    aggregation_host._record_scrape_target(first, _info("/tv/show", "/tv/show/e1.mkv"))
    aggregation_host._record_scrape_target(
        second, _info("/tv/show", "/tv/show/e2.mkv")
    )

    aggregation_host._finish_scrape_batch_task(first)
    aggregation_host._finish_scrape_batch_task(second)
    aggregation_host.eventmanager.send_event.assert_not_called()

    aggregation_host._close_scrape_batch("batch-1")
    aggregation_host.eventmanager.send_event.assert_called_once()
    _event_type, payload = aggregation_host.eventmanager.send_event.call_args.args
    assert payload["file_list"] == ["/tv/show/e1.mkv", "/tv/show/e2.mkv"]
    aggregation_host.success_notification_aggregator.release_batch.assert_called_once_with(
        "batch-1", loop=None
    )


def test_notification_still_called_per_task(host: ScrapeHost) -> None:
    """
    立即模式下成功通知的聚合调用仍按任务保留。
    """
    task = _task("/downloads/e1.mkv")
    host._register_scrape_batch_task(task)
    host._send_metadata_scrape_event(task, _info("/tv/show", "/tv/show/e1.mkv"))
    host.notify_successful_transfer(task, _info("/tv/show", "/tv/show/e1.mkv"))
    assert host.notify_successful_transfer.call_count == 1


def test_each_episode_settlement_scrapes_before_job_finishes() -> None:
    """
    立即模式下每个主媒体任务成功结算就发送刮削事件，不等作业内最后一个任务。

    同作业先成功的分集即使末集最后才完成（或失败），其刮削事件也必须已发出。
    """
    chain = make_transfer_chain()
    chain.eventmanager = MagicMock()
    chain.transfer_completed = lambda *args, **kwargs: None

    tasks = [make_task(1), make_task(2)]
    target_diritem = FileItem(
        storage="local",
        path="/library/Test Show (2026)",
        type="dir",
        name="Test Show (2026)",
    )
    batch_id = "batch-tv-immediate"
    for task in tasks:
        task.mediainfo = FakeMedia()
        task.transfer_batch_id = batch_id
        task.background = False
        task.manual = True
        assert chain._TransferChain__put_to_jobview(task)
        chain._register_scrape_batch_task(task)
    chain._close_scrape_batch(batch_id)

    transferinfos = [
        TransferInfo(
            success=True,
            fileitem=task.fileitem,
            target_diritem=target_diritem,
            target_item=FileItem(
                storage="local",
                path=f"/library/Test Show (2026)/Season 1/Test.Show.S01E0{number}.mkv",
                type="file",
                name=f"Test.Show.S01E0{number}.mkv",
                extension="mkv",
            ),
            file_list_new=[
                f"/library/Test Show (2026)/Season 1/Test.Show.S01E0{number}.mkv"
            ],
            transfer_type="copy",
            need_scrape=True,
            need_notify=False,
        )
        for number, task in enumerate(tasks, start=1)
    ]
    chain.transfer_history_repository = SimpleNamespace()

    with patch(
        "app.chain.transfer.settlement.add_transfer_success",
        lambda **kwargs: SimpleNamespace(id=1),
    ), patch("app.chain.transfer.settlement.StorageChain") as storage_chain_cls:
        storage_chain_cls.return_value.is_bluray_folder.return_value = False
        bind_terminal_checkpoint(tasks[0], transferinfos[0])
        chain._TransferChain__default_callback(tasks[0], transferinfos[0])
        chain._finish_scrape_batch_task(tasks[0])

        first_calls = [
            call
            for call in chain.eventmanager.send_event.call_args_list
            if call.args[0] == EventType.MetadataScrape
        ]
        assert [
            call.args[1]["file_list"] for call in first_calls
        ] == [["/library/Test Show (2026)/Season 1/Test.Show.S01E01.mkv"]]

        bind_terminal_checkpoint(tasks[1], transferinfos[1])
        chain._TransferChain__default_callback(tasks[1], transferinfos[1])
        chain._finish_scrape_batch_task(tasks[1])

    scrape_calls = [
        call
        for call in chain.eventmanager.send_event.call_args_list
        if call.args[0] == EventType.MetadataScrape
    ]
    assert [call.args[1]["file_list"] for call in scrape_calls] == [
        ["/library/Test Show (2026)/Season 1/Test.Show.S01E01.mkv"],
        ["/library/Test Show (2026)/Season 1/Test.Show.S01E02.mkv"],
    ]
    assert not chain._scrape_batches
