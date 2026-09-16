"""整理失败阶段和用户恢复提示测试。"""

from types import SimpleNamespace

from app.application.transfer.feedback import (
    TransferFailureStage,
    classify_transfer_failure,
    format_transfer_failure_message,
)
from app.chain.transfer.settlement import TransferSettlementOwner


def test_feedback_classifies_destination_and_recognition_failures() -> None:
    """常见目标写入与媒体识别错误应映射到不同可操作阶段。"""
    destination = classify_transfer_failure("目标路径不可写")
    recognition = classify_transfer_failure("没有找到可整理的媒体文件")

    assert destination.stage is TransferFailureStage.DESTINATION_ACCESS
    assert "目标存储" in destination.action
    assert recognition.stage is TransferFailureStage.RECOGNITION
    assert "媒体来源" in recognition.action


def test_feedback_message_contains_paths_stage_action_and_pause() -> None:
    """公开错误文案应一次给出定位信息、下一步动作和自动暂停恢复方式。"""
    message = format_transfer_failure_message(
        "目标路径不可写",
        source_path="/downloads/movie.mkv",
        target_path="/media/movies",
        retry_count=3,
        max_retries=3,
        auto_paused=True,
    )

    assert "源文件：/downloads/movie.mkv" in message
    assert "目标路径：/media/movies" in message
    assert "失败阶段：目标存储访问" in message
    assert "下一步：检查目标存储连接" in message
    assert "重试状态：已尝试 3/3 次" in message
    assert "自动整理已暂停" in message


def test_downloader_cleanup_failure_marks_every_successful_history_for_hash() -> None:
    """一个下载任务包含多个文件时，清理失败状态应覆盖全部成功入库历史。"""
    writes = []
    repository = SimpleNamespace(
        list_by_hash=lambda _download_hash: [
            SimpleNamespace(id=11, status=True),
            SimpleNamespace(id=12, status=True),
            SimpleNamespace(id=13, status=False),
        ],
        update_cleanup_status=lambda *args: writes.append(args),
    )

    TransferSettlementOwner._record_downloader_cleanup_failure(
        repository,
        download_hash="torrent-hash",
        fallback_history_id=99,
        cleanup_error="下载器清理失败",
    )

    assert set(writes) == {
        (11, "failed", "下载器清理失败"),
        (12, "failed", "下载器清理失败"),
        (99, "failed", "下载器清理失败"),
    }


def test_downloader_cleanup_failure_keeps_fallback_when_history_query_fails() -> None:
    """关联历史查询异常时仍应标记当前成功记录，且不得破坏成功整理回调。"""
    writes = []
    repository = SimpleNamespace(
        list_by_hash=lambda _download_hash: (_ for _ in ()).throw(RuntimeError("数据库抖动")),
        update_cleanup_status=lambda *args: writes.append(args),
    )

    TransferSettlementOwner._record_downloader_cleanup_failure(
        repository,
        download_hash="torrent-hash",
        fallback_history_id=99,
        cleanup_error="下载器清理失败",
    )

    assert writes == [(99, "failed", "下载器清理失败")]
