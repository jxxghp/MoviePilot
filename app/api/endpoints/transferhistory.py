"""手动整理历史批次的 HTTP 适配辅助函数。"""

from collections.abc import Sequence
from typing import Any, List, Optional

from app.application.history import ManualTransferHistory, TransferHistoryLookupService
from app.runtime.log import logger
from app.schemas.file import FileItem
from app.schemas.transfer import ManualTransferItem


def _common_history_value(
    histories: Sequence[ManualTransferHistory], attribute: str,
) -> Any:
    """返回一组整理历史中完全一致的非空字段值。"""
    values = [getattr(history, attribute, None) for history in histories]
    nonempty_values = [value for value in values if value is not None and value != ""]
    if not nonempty_values or len(nonempty_values) != len(values):
        return None
    first_value = nonempty_values[0]
    return first_value if all(value == first_value for value in nonempty_values) else None


def restore_manual_transfer_history_batch(
    transer_item: ManualTransferItem,
    history_query: TransferHistoryLookupService,
) -> tuple[List[FileItem], bool, Optional[str], Optional[str], Optional[str]]:
    """还原多选历史的源文件集合与可安全复用的共同下载上下文。"""
    histories = []
    src_fileitems = []
    for logid in transer_item.logids or []:
        history = history_query.get(logid)
        if not history:
            return [], False, None, None, f"整理记录不存在，ID：{logid}"
        histories.append(history)
        source_payload = (
            history.dest_fileitem
            if history.status and history.mode and "move" in history.mode
            else history.src_fileitem
        )
        if not source_payload:
            return [], False, None, None, f"整理记录缺少文件信息，ID：{logid}"
        src_fileitems.append(FileItem.model_validate(source_payload))

    force = bool(histories) and all(bool(history.status) for history in histories)
    downloader = None
    download_hash = None
    if transer_item.from_history and histories:
        transer_item.type_name = (
            _common_history_value(histories, "type") or transer_item.type_name
        )
        transer_item.media_source = (
            _common_history_value(histories, "media_source")
            or transer_item.media_source
        )
        transer_item.media_id = (
            _common_history_value(histories, "media_id") or transer_item.media_id
        )
        transer_item.music_type = (
            _common_history_value(histories, "music_type")
            or transer_item.music_type
        )
        downloader = _common_history_value(histories, "downloader")
        download_hash = _common_history_value(histories, "download_hash")
    logger.info("手动整理历史批次还原 %s 个源文件", len(src_fileitems))
    return src_fileitems, force, downloader, download_hash, None
