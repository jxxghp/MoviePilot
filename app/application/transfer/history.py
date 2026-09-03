"""整理历史写入所需的稳定字段投影。"""

from typing import Any, Optional, Union

from app.application.classification.reference import effective_classification_snapshot
from app.domain.context import MediaInfo, MusicInfo
from app.domain.meta.metabase import MetaBase
from app.domain.meta.metamusic import MetaMusic
from app.schemas.file import FileItem


def classification_fields(
    media: Union[MediaInfo, MusicInfo],
) -> dict[str, Any]:
    """投影整理历史使用的最终分类标量，避免保存推荐分类。"""
    snapshot = effective_classification_snapshot(media)
    return {
        "media_category_id": snapshot.category_id,
        "category": snapshot.path,
        "classification_rule_id": snapshot.rule_id,
        "classification_policy_revision": snapshot.policy_revision,
        "classification_source": snapshot.source,
    }


def history_title(
    meta: MetaBase,
    mediainfo: Optional[Union[MediaInfo, MusicInfo]] = None,
) -> Optional[str]:
    """音乐文件优先记录曲目标题，其它媒体保持识别标题。"""
    if isinstance(meta, MetaMusic) and meta.title:
        return str(meta.title)
    if mediainfo and mediainfo.title:
        return str(mediainfo.title)
    return str(meta.name) if meta.name else None


def history_source_path(fileitem: FileItem) -> str:
    """返回整理历史必需的源路径，拒绝持久化无身份记录。"""
    if not fileitem.path:
        raise ValueError("整理历史缺少源文件路径")
    return fileitem.path


def history_year(value: object) -> Optional[str]:
    """把媒体年份规范为整理历史稳定字符串。"""
    return str(value) if value is not None else None
