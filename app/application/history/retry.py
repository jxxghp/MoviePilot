"""整理历史查重闸与失败重试预算。"""

from __future__ import annotations

from typing import Any, Dict, Optional

from app.application.configuration import TransferRetryConfig, get_transfer_retry_config
from app.application.history.contracts import (
    TransferHistoryQueryPort,
    TransferHistorySnapshot,
    get_transfer_history_repository,
)
from app.runtime.cache import TTLCache
from app.runtime.log import logger

# 失败重试次数的合法区间，避免瞬时故障永久漏件或永久失败反复刷通知。
MIN_FAILED_RETRIES = 1
MAX_FAILED_RETRIES = 10
FAILED_RETRY_TTL = 24 * 3600

# 缓存值会同时保存文件指纹，使同一路径的新版本获得独立重试预算。
_failed_retry_counts = TTLCache(
    region="transfer_failed_retry",
    maxsize=5000,
    ttl=FAILED_RETRY_TTL,
)


class HistoryGateAction:
    """整理历史查重闸的判定结果。"""

    PASS_NO_RECORD = "pass_no_record"
    PASS_FAILED = "pass_failed"
    PASS_FAILED_VERSION_CHANGED = "pass_failed_version_changed"
    PASS_SIZE_CHANGED = "pass_size_changed"
    SKIP_RETRY_EXHAUSTED = "skip_retry_exhausted"
    SKIP = "skip"


def is_skip_action(action: str) -> bool:
    """判断查重闸判定是否为跳过整理。"""
    return action in (HistoryGateAction.SKIP, HistoryGateAction.SKIP_RETRY_EXHAUSTED)


def max_failed_retries(config: TransferRetryConfig | None = None) -> int:
    """读取失败重试上限并钳制到合法区间。"""
    raw = (config or get_transfer_retry_config()).max_failed_retries
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warn(
            f"TRANSFER_MAX_FAILED_RETRIES 配置非法（{raw!r}），"
            f"已回退为 {MIN_FAILED_RETRIES}"
        )
        return MIN_FAILED_RETRIES
    if value < MIN_FAILED_RETRIES:
        logger.warn(
            f"TRANSFER_MAX_FAILED_RETRIES 不能小于 {MIN_FAILED_RETRIES}"
            f"（当前 {value}），已按 {MIN_FAILED_RETRIES} 处理"
        )
        return MIN_FAILED_RETRIES
    if value > MAX_FAILED_RETRIES:
        logger.warn(
            f"TRANSFER_MAX_FAILED_RETRIES 不能大于 {MAX_FAILED_RETRIES}"
            f"（当前 {value}），已按 {MAX_FAILED_RETRIES} 处理"
        )
        return MAX_FAILED_RETRIES
    return value


def failed_retry_key(src_path: Optional[str], storage: Optional[str] = None) -> Optional[str]:
    """生成失败重试计数的缓存键。"""
    if not src_path:
        return None
    return f"{storage or 'local'}:{src_path}"


def coerce_modify_time(modify_time: Any) -> Optional[float]:
    """统一转换文件修改时间，无法转换时返回 None。"""
    if modify_time is None:
        return None
    try:
        return float(modify_time)
    except (TypeError, ValueError):
        return None


def coerce_fileid(fileid: Any) -> Optional[str]:
    """统一转换文件唯一标识，空值视为不可比对。"""
    if fileid is None:
        return None
    value = str(fileid).strip()
    return value or None


def file_fingerprint(
        file_size: Any = None,
        file_modify_time: Any = None,
        fileid: Any = None,
) -> Dict[str, Any]:
    """生成用于区分同一路径文件版本的稳定指纹。"""
    fingerprint: Dict[str, Any] = {}
    size = coerce_size(file_size)
    if size is not None:
        fingerprint["size"] = size
    modify_time = coerce_modify_time(file_modify_time)
    if modify_time is not None:
        fingerprint["modify_time"] = modify_time
    normalized_fileid = coerce_fileid(fileid)
    if normalized_fileid is not None:
        fingerprint["fileid"] = normalized_fileid
    return fingerprint


def _retry_state(value: Any) -> tuple[int, Dict[str, Any]]:
    """将新旧缓存值统一转换为失败次数与文件指纹。"""
    if isinstance(value, dict):
        raw_count = value.get("count", 0)
        raw_fingerprint = value.get("fingerprint")
    else:
        raw_count = value
        raw_fingerprint = None
    try:
        count = max(int(raw_count or 0), 0)
    except (TypeError, ValueError):
        count = 0
    fingerprint = (
        file_fingerprint(
            file_size=raw_fingerprint.get("size"),
            file_modify_time=raw_fingerprint.get("modify_time"),
            fileid=raw_fingerprint.get("fileid"),
        )
        if isinstance(raw_fingerprint, dict)
        else {}
    )
    return count, fingerprint


def _is_file_version_changed(
        recorded_fingerprint: Dict[str, Any],
        current_fingerprint: Dict[str, Any],
) -> bool:
    """判断两个可比文件指纹是否指向不同版本。"""
    for field in ("fileid", "modify_time", "size"):
        recorded_value = recorded_fingerprint.get(field)
        current_value = current_fingerprint.get(field)
        if (
                recorded_value is not None
                and current_value is not None
                and recorded_value != current_value
        ):
            return True
    return False


def failed_retry_count(src_path: Optional[str], storage: Optional[str] = None,
                       file_size: Any = None, file_modify_time: Any = None,
                       fileid: Any = None) -> int:
    """读取同一源路径已累计的连续整理失败次数。"""
    key = failed_retry_key(src_path, storage)
    if not key:
        return 0
    count, recorded_fingerprint = _retry_state(_failed_retry_counts.get(key))
    current_fingerprint = file_fingerprint(
        file_size=file_size,
        file_modify_time=file_modify_time,
        fileid=fileid,
    )
    if (
            recorded_fingerprint
            and current_fingerprint
            and _is_file_version_changed(recorded_fingerprint, current_fingerprint)
    ):
        return 0
    return count


def record_transfer_failure(src_path: Optional[str], storage: Optional[str] = None,
                            file_size: Any = None, file_modify_time: Any = None,
                            fileid: Any = None) -> int:
    """累计一次整理失败并返回当前文件版本的连续失败次数。"""
    key = failed_retry_key(src_path, storage)
    if not key:
        return 0
    count, recorded_fingerprint = _retry_state(_failed_retry_counts.get(key))
    current_fingerprint = file_fingerprint(
        file_size=file_size,
        file_modify_time=file_modify_time,
        fileid=fileid,
    )
    if current_fingerprint and (
            not recorded_fingerprint
            or _is_file_version_changed(recorded_fingerprint, current_fingerprint)
    ):
        count = 0
    count += 1
    if current_fingerprint:
        _failed_retry_counts[key] = {
            "count": count,
            "fingerprint": current_fingerprint,
        }
    elif recorded_fingerprint:
        _failed_retry_counts[key] = {
            "count": count,
            "fingerprint": recorded_fingerprint,
        }
    else:
        _failed_retry_counts[key] = count
    return count


def clear_transfer_failures(src_path: Optional[str], storage: Optional[str] = None) -> None:
    """清空同一源路径的失败计数。"""
    key = failed_retry_key(src_path, storage)
    if key:
        # CacheBackend.pop 的默认值必须显式为 0，避免未命中时抛 KeyError。
        _failed_retry_counts.pop(key, 0)


def coerce_size(size: Any) -> Optional[int]:
    """统一转换文件大小，无法转换时返回 None。"""
    if size is None:
        return None
    try:
        return int(size)
    except (TypeError, ValueError):
        return None


def history_src_size(history: TransferHistorySnapshot) -> Optional[int]:
    """读取整理记录中的源文件大小。"""
    return history_src_fingerprint(history).get("size")


def history_src_fingerprint(history: TransferHistorySnapshot) -> Dict[str, Any]:
    """读取整理记录中的源文件版本指纹。"""
    src_fileitem = getattr(history, "src_fileitem", None)
    if not isinstance(src_fileitem, dict):
        return {}
    return file_fingerprint(
        file_size=src_fileitem.get("size"),
        file_modify_time=src_fileitem.get("modify_time"),
        fileid=src_fileitem.get("fileid"),
    )


def resolve_history(
    src_path: str,
    storage: Optional[str] = None,
    transfer_history_oper: Optional[TransferHistoryQueryPort] = None,
) -> Optional[TransferHistorySnapshot]:
    """查询源路径对应的整理记录，并优先返回成功记录。"""
    repository = transfer_history_oper or get_transfer_history_repository()
    history = repository.get_by_src(src_path, storage=storage)
    if history is not None and not history.status:
        history = repository.get_success_by_src(src_path, storage=storage) or history
    return history


def evaluate_history_gate(history: Optional[TransferHistorySnapshot],
                          file_size: Optional[float] = None,
                          file_modify_time: Optional[float] = None,
                          fileid: Optional[str] = None,
                          retry_count: Optional[int] = None) -> str:
    """依据整理历史和文件版本判断本次是否跳过整理。"""
    if history is None:
        return HistoryGateAction.PASS_NO_RECORD
    recorded_fingerprint = history_src_fingerprint(history)
    current_fingerprint = file_fingerprint(
        file_size=file_size,
        file_modify_time=file_modify_time,
        fileid=fileid,
    )
    if not history.status:
        if _is_file_version_changed(recorded_fingerprint, current_fingerprint):
            return HistoryGateAction.PASS_FAILED_VERSION_CHANGED
        if retry_count is None:
            retry_count = failed_retry_count(
                getattr(history, "src", None),
                getattr(history, "src_storage", None),
                file_size=file_size,
                file_modify_time=file_modify_time,
                fileid=fileid,
            )
        if retry_count >= max_failed_retries():
            return HistoryGateAction.SKIP_RETRY_EXHAUSTED
        return HistoryGateAction.PASS_FAILED
    if _is_file_version_changed(recorded_fingerprint, current_fingerprint):
        return HistoryGateAction.PASS_SIZE_CHANGED
    return HistoryGateAction.SKIP


def describe_history_gate(history: Optional[TransferHistorySnapshot],
                          file_size: Optional[float] = None,
                          file_modify_time: Optional[float] = None,
                          fileid: Optional[str] = None) -> str:
    """生成查重闸判定的可读说明，供日志定位拦截原因。"""
    if history is None:
        return "无整理记录"
    recorded_fingerprint = history_src_fingerprint(history)
    current_fingerprint = file_fingerprint(
        file_size=file_size,
        file_modify_time=file_modify_time,
        fileid=fileid,
    )
    if not history.status:
        count = getattr(history, "retry_count", None)
        if count is None:
            count = failed_retry_count(
                getattr(history, "src", None),
                getattr(history, "src_storage", None),
                file_size=file_size,
                file_modify_time=file_modify_time,
                fileid=fileid,
            )
        if _is_file_version_changed(recorded_fingerprint, current_fingerprint):
            return f"失败记录 #{history.id}，文件版本已变化，重试预算将重置"
        return f"失败记录 #{history.id}，已重试 {count}/{max_failed_retries()} 次"
    recorded_size = recorded_fingerprint.get("size")
    current_size = current_fingerprint.get("size")
    if recorded_size is None and current_size is None:
        return f"成功记录 #{history.id}，大小不可比对"
    return f"成功记录 #{history.id}，大小 {recorded_size} -> {current_size}"


def next_failed_retry_count(
    history: Optional[TransferHistorySnapshot],
    *,
    src_path: Optional[str],
    storage: Optional[str] = None,
    file_size: Optional[float] = None,
    file_modify_time: Optional[float] = None,
    fileid: Optional[str] = None,
) -> int:
    """合并缓存与历史记录，计算下一次失败应持久化的连续次数。"""
    cached_count = failed_retry_count(
        src_path,
        storage,
        file_size=file_size,
        file_modify_time=file_modify_time,
        fileid=fileid,
    )
    persisted_count = 0
    if history is not None and not history.status:
        history_retry_count = getattr(history, "retry_count", None) or 0
        gate_action = evaluate_history_gate(
            history,
            file_size=file_size,
            file_modify_time=file_modify_time,
            fileid=fileid,
            retry_count=history_retry_count,
        )
        if gate_action != HistoryGateAction.PASS_FAILED_VERSION_CHANGED:
            persisted_count = max(history_retry_count, 0)
    return max(cached_count, persisted_count) + 1
