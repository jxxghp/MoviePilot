"""整理完成事件只对应实际目标文件，静默跳过仍正常结算。"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.schemas.transfer import TransferInfo
from app.schemas.types import EventType
from tests.test_transfer_job_manager import (
    FakeMedia,
    bind_terminal_checkpoint,
    make_fileitem,
    make_task,
    make_transfer_chain,
)


@pytest.mark.parametrize(
    ("extension", "completed_topic", "completed_event", "failed_topic", "failed_event"),
    [
        ("mkv", "transfer.completed", EventType.TransferComplete,
         "transfer.failed", EventType.TransferFailed),
        ("srt", "transfer.subtitle.completed", EventType.SubtitleTransferComplete,
         "transfer.subtitle.failed", EventType.SubtitleTransferFailed),
        ("flac", "transfer.audio.completed", EventType.AudioTransferComplete,
         "transfer.audio.failed", EventType.AudioTransferFailed),
    ],
)
@pytest.mark.parametrize("outcome", ["skipped", "without_target", "completed", "failed"])
def test_transfer_result_events_require_completed_target(
        extension: str,
        completed_topic: str,
        completed_event: EventType,
        failed_topic: str,
        failed_event: EventType,
        outcome: str,
) -> None:
    """视频、字幕和音频跳过不发完成事件，正常完成和失败保留原事件与终态。"""
    chain = make_transfer_chain()
    chain.eventmanager = MagicMock()
    chain.queue_failed_transfer_notification = MagicMock()
    chain._TransferChain__mark_torrent_completed_if_done = MagicMock()
    chain._record_scrape_target = MagicMock()
    chain._send_metadata_scrape_event = MagicMock()
    task = make_task(1)
    task.fileitem = make_fileitem(f"/downloads/Test.Show.S01E01.{extension}")
    task.mediainfo = FakeMedia()
    assert chain._TransferChain__put_to_jobview(task)
    transferinfo = TransferInfo(
        success=outcome != "failed",
        message="综艺非正片已全部跳过（1 个文件）" if outcome == "skipped" else None,
        fileitem=task.fileitem,
        target_item=(
            make_fileitem(f"/library/Test.Show.S01E01.{extension}")
            if outcome == "completed"
            else None
        ),
        transfer_type="copy",
        need_notify=False,
    )
    bind_terminal_checkpoint(task, transferinfo)
    with patch(
        "app.chain.transfer.settlement.add_transfer_success",
        return_value=SimpleNamespace(id=1),
    ) as add_success, patch(
        "app.chain.transfer.settlement.add_transfer_fail",
        return_value=SimpleNamespace(id=1),
    ) as add_fail, patch(
        "app.chain.transfer.settlement.clear_transfer_failures",
    ), patch(
        "app.chain.transfer.settlement.record_transfer_failure",
    ):
        state, _message = chain._TransferChain__default_callback(task, transferinfo)

    assert state is (outcome != "failed")
    chain.durable_event_writer.transfer_result.assert_called_once()
    call = chain.durable_event_writer.transfer_result.call_args.kwargs
    assert call["settlement"].outcome == ("failed" if outcome == "failed" else "succeeded")
    assert task.terminal_settled is True
    if outcome == "failed":
        add_fail.assert_called_once()
        add_success.assert_not_called()
        assert call["topic"] == failed_topic
        assert chain.eventmanager.send_event.call_args.args[0] is failed_event
    else:
        add_success.assert_called_once()
        add_fail.assert_not_called()
        chain.queue_failed_transfer_notification.assert_not_called()
        if outcome in ("skipped", "without_target"):
            assert call["topic"] is None
            chain.eventmanager.send_event.assert_not_called()
        else:
            assert call["topic"] == completed_topic
            event_type, payload = chain.eventmanager.send_event.call_args.args
            assert event_type is completed_event
            assert payload["transferinfo"].target_item == transferinfo.target_item
