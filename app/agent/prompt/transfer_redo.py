"""整理记录 AI 重新整理提示词构造。"""
from typing import Any

from app.agent.prompt import prompt_manager


def build_manual_redo_template_context(history: Any) -> dict[str, int | str]:
    """把整理历史对象映射成 System Tasks 需要的模板变量。"""
    src_fileitem = history.src_fileitem or {}
    dest_fileitem = history.dest_fileitem or {}
    source_path = src_fileitem.get("path") if isinstance(src_fileitem, dict) else ""
    source_storage = history.src_storage or "local"
    if history.status and history.mode == "move":
        dest_path = dest_fileitem.get("path") if isinstance(dest_fileitem, dict) else ""
        if dest_path:
            source_path = dest_path
            source_storage = history.dest_storage or "local"
    source_path = source_path or history.src or ""
    season_episode = f"{history.seasons or ''}{history.episodes or ''}".strip()
    return {
        "history_id": history.id,
        "current_status": "success" if history.status else "failed",
        "recognized_title": history.title or "unknown",
        "media_type": history.type or "unknown",
        "category": history.category or "unknown",
        "year": history.year or "unknown",
        "season_episode": season_episode or "unknown",
        "source_path": source_path or "unknown",
        "source_storage": source_storage,
        "destination_path": history.dest or "unknown",
        "destination_storage": history.dest_storage or "unknown",
        "transfer_mode": history.mode or "unknown",
        "tmdbid": history.tmdbid or "none",
        "doubanid": history.doubanid or "none",
        "error_message": history.errmsg or "none",
    }


def format_manual_redo_record_context(history: Any) -> str:
    """把单条整理记录格式化为批量任务可直接消费的上下文块。"""
    context = build_manual_redo_template_context(history)
    return "\n".join(
        [
            f"Record #{context['history_id']}:",
            f"- Current status: {context['current_status']}",
            f"- Current recognized title: {context['recognized_title']}",
            f"- Media type: {context['media_type']}",
            f"- Category: {context['category']}",
            f"- Year: {context['year']}",
            f"- Season/Episode: {context['season_episode']}",
            f"- Source path: {context['source_path']}",
            f"- Source storage: {context['source_storage']}",
            f"- Destination path: {context['destination_path']}",
            f"- Destination storage: {context['destination_storage']}",
            f"- Transfer mode: {context['transfer_mode']}",
            f"- Current TMDB ID: {context['tmdbid']}",
            f"- Current Douban ID: {context['doubanid']}",
            f"- Error message: {context['error_message']}",
        ]
    )


def build_manual_redo_prompt(history: Any) -> str:
    """构建手动 AI 整理提示词。"""
    return prompt_manager.render_system_task_message(
        "manual_transfer_redo",
        template_context=build_manual_redo_template_context(history),
    )


def build_batch_manual_redo_template_context(histories: list[Any]) -> dict[str, int | str]:
    """把多条整理历史对象映射成批量 System Tasks 需要的模板变量。"""
    return {
        "history_ids_csv": ", ".join(str(history.id) for history in histories),
        "history_count": len(histories),
        "records_context": "\n\n".join(
            format_manual_redo_record_context(history) for history in histories
        ),
    }


def build_batch_manual_redo_prompt(histories: list[Any]) -> str:
    """构建批量手动 AI 整理提示词。"""
    return prompt_manager.render_system_task_message(
        "batch_manual_transfer_redo",
        template_context=build_batch_manual_redo_template_context(histories),
    )
