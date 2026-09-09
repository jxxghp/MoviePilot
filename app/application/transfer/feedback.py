"""整理失败的用户反馈、阶段识别和恢复动作投影。"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Optional


class TransferFailureStage(StrEnum):
    """整理失败所在的可操作阶段。"""

    SOURCE_ACCESS = "source_access"
    RECOGNITION = "recognition"
    PLANNING = "planning"
    DESTINATION_ACCESS = "destination_access"
    TRANSFER = "transfer"
    OVERWRITE = "overwrite"
    DOWNLOADER_CLEANUP = "downloader_cleanup"
    EXECUTION = "execution"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class TransferFailureFeedback:
    """整理失败的公开阶段、动作和自动处理状态。"""

    stage: TransferFailureStage
    action: str
    retryable: bool = True
    auto_paused: bool = False


_STAGE_RULES: tuple[tuple[TransferFailureStage, tuple[str, ...]], ...] = (
    (
        TransferFailureStage.SOURCE_ACCESS,
        ("源文件不存在", "无法访问源文件", "读取源文件", "文件不存在", "没有找到源文件"),
    ),
    (
        TransferFailureStage.RECOGNITION,
        (
            "未识别到媒体",
            "识别媒体",
            "媒体信息",
            "无法识别",
            "识别失败",
            "没有找到可整理",
        ),
    ),
    (
        TransferFailureStage.PLANNING,
        ("整理计划", "规划失败", "计划失败", "缺少整理", "规划输入"),
    ),
    (
        TransferFailureStage.OVERWRITE,
        ("覆盖策略", "目标已存在", "跳过覆盖", "不覆盖"),
    ),
    (
        TransferFailureStage.DOWNLOADER_CLEANUP,
        ("删除种子", "清理下载器", "下载器清理", "移除种子"),
    ),
    (
        TransferFailureStage.EXECUTION,
        ("租约", "正在被其他", "执行快照", "任务状态", "人工复核"),
    ),
    (
        TransferFailureStage.DESTINATION_ACCESS,
        (
            "目标目录",
            "目标文件",
            "目标存储",
            "写入目标",
            "写入失败",
            "不可写",
            "创建目录",
            "剩余空间",
            "没有权限",
            "权限不足",
        ),
    ),
)

_STAGE_ACTIONS: dict[TransferFailureStage, str] = {
    TransferFailureStage.SOURCE_ACCESS: "检查源文件是否存在，以及 MoviePilot 的读取权限和路径映射",
    TransferFailureStage.RECOGNITION: "修改文件名，或指定正确的媒体来源和媒体 ID 后重新整理",
    TransferFailureStage.PLANNING: "检查媒体身份、目标目录和整理参数后点击“重新生成计划”",
    TransferFailureStage.DESTINATION_ACCESS: "检查目标存储连接、目录权限和剩余空间，修复后重试",
    TransferFailureStage.TRANSFER: "检查源文件和目标存储，修复后重新整理",
    TransferFailureStage.OVERWRITE: "调整覆盖策略，或移除目标位置的旧文件后重新整理",
    TransferFailureStage.DOWNLOADER_CLEANUP: "媒体已入库；请检查下载器任务并手动清理未完成的下载器操作",
    TransferFailureStage.EXECUTION: "刷新整理历史，等待任务结束；仍无法继续时提交人工复核",
    TransferFailureStage.UNKNOWN: "检查源文件、路径映射和目标存储，修复后重新整理",
}

_STAGE_LABELS: dict[TransferFailureStage, str] = {
    TransferFailureStage.SOURCE_ACCESS: "源文件访问",
    TransferFailureStage.RECOGNITION: "媒体识别",
    TransferFailureStage.PLANNING: "整理计划",
    TransferFailureStage.DESTINATION_ACCESS: "目标存储访问",
    TransferFailureStage.TRANSFER: "文件转移",
    TransferFailureStage.OVERWRITE: "覆盖策略",
    TransferFailureStage.DOWNLOADER_CLEANUP: "下载器清理",
    TransferFailureStage.EXECUTION: "任务执行",
    TransferFailureStage.UNKNOWN: "未知阶段",
}


def transfer_failure_stage_label(stage: Optional[str | TransferFailureStage]) -> str:
    """把稳定失败阶段编码转换为后端通知使用的中文标签。"""
    try:
        normalized = TransferFailureStage(stage or TransferFailureStage.UNKNOWN)
    except ValueError:
        return str(stage or _STAGE_LABELS[TransferFailureStage.UNKNOWN])
    return _STAGE_LABELS[normalized]


def classify_transfer_failure(
        error: Optional[object],
        *,
        overwrite_skipped: bool = False,
        auto_paused: bool = False,
) -> TransferFailureFeedback:
    """根据稳定业务关键词生成用户可执行的失败阶段和恢复动作。"""
    if overwrite_skipped:
        stage = TransferFailureStage.OVERWRITE
    else:
        message = " ".join(str(error or "").split())
        stage = TransferFailureStage.TRANSFER
        for candidate, markers in _STAGE_RULES:
            if any(marker in message for marker in markers):
                stage = candidate
                break
    return TransferFailureFeedback(
        stage=stage,
        action=_STAGE_ACTIONS[stage],
        retryable=stage not in {
            TransferFailureStage.OVERWRITE,
            TransferFailureStage.DOWNLOADER_CLEANUP,
        },
        auto_paused=auto_paused,
    )


def format_transfer_failure_message(
        error: Optional[object],
        *,
        stage: Optional[str] = None,
        action: Optional[str] = None,
        source_path: Optional[str] = None,
        target_path: Optional[str] = None,
        retry_count: Optional[int] = None,
        max_retries: Optional[int] = None,
        auto_paused: bool = False,
) -> str:
    """把整理失败转换为包含原因、阶段和下一步动作的公开文案。"""
    feedback = classify_transfer_failure(error, auto_paused=auto_paused)
    lines = [
        f"失败阶段：{transfer_failure_stage_label(stage or feedback.stage)}",
        f"原因：{' '.join(str(error or '整理失败').split())}",
        f"下一步：{action or feedback.action}",
    ]
    if source_path:
        lines.insert(0, f"源文件：{source_path}")
    if target_path:
        lines.insert(1 if source_path else 0, f"目标路径：{target_path}")
    if retry_count is not None and max_retries is not None:
        lines.append(f"重试状态：已尝试 {retry_count}/{max_retries} 次")
    if auto_paused:
        lines.append("当前状态：自动整理已暂停，请修复原因后点击“重新整理”，或删除失败记录后重新扫描")
    return "\n".join(lines)
