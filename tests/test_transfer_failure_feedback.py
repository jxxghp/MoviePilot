"""
整理失败阶段和用户恢复提示测试。
"""

from types import SimpleNamespace

from app.application.transfer.feedback import (
    TransferFailureStage,
    classify_transfer_failure,
    format_transfer_failure_message,
)
from app.chain.transfer.settlement import TransferSettlementOwner


def test_feedback_classifies_destination_and_recognition_failures() -> None:
    """
    常见目标写入与媒体识别错误应映射到不同可操作阶段。
    """
    destination = classify_transfer_failure("目标路径不可写")
    recognition = classify_transfer_failure("没有找到可整理的媒体文件")

    assert destination.stage is TransferFailureStage.DESTINATION_ACCESS
    assert "目标存储" in destination.action
    assert recognition.stage is TransferFailureStage.RECOGNITION
    assert "媒体来源" in recognition.action


def test_feedback_classifies_cue_parse_failure_as_recognition() -> None:
    """
    CUE 解析失败应归为媒体识别阶段，恢复动作须指向 CUE 而不是通用重试。
    """
    broken_index = classify_transfer_failure(
        "关联 CUE 无法解析：album.cue - CUE 同一文件的时间索引重复或逆序"
    )
    unreadable = classify_transfer_failure("关联 CUE 无法解析：album.cue")
    conflicting = classify_transfer_failure("存在多个内容冲突的 CUE，请保留需要的索引后重试")
    suggested_wording = classify_transfer_failure("CUE 文件解析失败：album.cue（时间索引无效），可关闭「识别 CUE」后重试")

    for feedback in (broken_index, unreadable, conflicting, suggested_wording):
        assert feedback.stage is TransferFailureStage.RECOGNITION
        assert "关闭「识别 CUE」" in feedback.action
        assert feedback.retryable is True


def test_feedback_does_not_treat_cue_mentions_as_cue_failure() -> None:
    """
    路径、文件名或无关文案里出现 CUE 字样时不能误归为 CUE 解析失败。
    """
    missing_uppercase_cue = classify_transfer_failure("源文件不存在：ALBUM.CUE")
    missing_cue_collection = classify_transfer_failure("无法访问源文件：My CUE Collection/test.flac")
    missing_cue_folder = classify_transfer_failure("源文件不存在：/music/CUE 文件/album.flac")
    missing_lowercase_cue = classify_transfer_failure("源文件不存在：album.cue")
    iso_package = classify_transfer_failure("test.iso：音乐镜像暂不支持直接整理，请先提取音频和 CUE 后重试")

    for feedback in (
        missing_uppercase_cue,
        missing_cue_collection,
        missing_cue_folder,
        missing_lowercase_cue,
    ):
        assert feedback.stage is TransferFailureStage.SOURCE_ACCESS
        assert "关闭「识别 CUE」" not in feedback.action
    assert iso_package.stage is TransferFailureStage.TRANSFER
    assert "关闭「识别 CUE」" not in iso_package.action


def test_feedback_message_contains_paths_stage_action_and_pause() -> None:
    """
    公开错误文案应一次给出定位信息、下一步动作和自动暂停恢复方式。
    """
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
    """
    一个下载任务包含多个文件时，清理失败状态应覆盖全部成功入库历史。
    """
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
    """
    关联历史查询异常时仍应标记当前成功记录，且不得破坏成功整理回调。
    """
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
