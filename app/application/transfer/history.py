"""整理历史写入所需的稳定字段投影。"""

from typing import Any, Optional, TypedDict, Union

from app.application.classification.reference import effective_classification_snapshot
from app.domain.context import MediaInfo, MusicInfo
from app.domain.meta.metabase import MetaBase
from app.domain.meta.metamusic import MetaMusic
from app.schemas.file import FileItem
from app.schemas.media import resolve_media_identity
from app.schemas.transfer import TransferInfo
from app.schemas.types import MUSIC_ENTITY_RECORDING


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


def success_fields(
    fileitem: FileItem,
    mode: str,
    meta: MetaBase,
    mediainfo: Union[MediaInfo, MusicInfo],
    transferinfo: TransferInfo,
    downloader: Optional[str] = None,
    download_hash: Optional[str] = None,
) -> dict[str, Any]:
    """
    投影转移成功记录，不执行数据库写入。
    :param fileitem: 源文件项
    :param mode: 整理方式
    :param meta: 文件名识别结果
    :param mediainfo: 媒体识别结果
    :param transferinfo: 整理结果
    :param downloader: 下载器
    :param download_hash: 种子 hash
    :return: 可构造类型化写入命令的历史字段
    """
    media_source, media_id = resolve_media_identity(media=mediainfo)
    return dict(
        src=history_source_path(fileitem),
        src_storage=fileitem.storage,
        src_fileitem=fileitem.model_dump(),
        dest=transferinfo.target_item.path if transferinfo.target_item else None,
        dest_storage=transferinfo.target_item.storage if transferinfo.target_item else None,
        dest_fileitem=transferinfo.target_item.model_dump() if transferinfo.target_item else None,
        mode=mode,
        type=mediainfo.type.value,
        **classification_fields(mediainfo),
        title=history_title(meta, mediainfo),
        year=history_year(mediainfo.year),
        media_source=media_source,
        media_id=media_id,
        music_type=getattr(mediainfo, "music_type", None),
        total_tracks=getattr(mediainfo, "total_tracks", None),
        audio_format=getattr(meta, "audio_format", None),
        audio_lossless=getattr(meta, "audio_lossless", None),
        bit_depth=getattr(meta, "bit_depth", None),
        sample_rate=getattr(meta, "sample_rate", None),
        bitrate=getattr(meta, "bitrate", None),
        seasons=meta.season,
        episodes=meta.episode,
        image=mediainfo.get_poster_image(),
        downloader=downloader,
        download_hash=download_hash,
        status=True,
        retry_count=0,
        auto_paused=False,
        files=transferinfo.file_list,
    )


def failure_fields(
    fileitem: FileItem,
    mode: str,
    meta: MetaBase,
    mediainfo: Optional[Union[MediaInfo, MusicInfo]] = None,
    transferinfo: Optional[TransferInfo] = None,
    downloader: Optional[str] = None,
    download_hash: Optional[str] = None,
    retry_count: Optional[int] = None,
    auto_paused: bool = False,
) -> dict[str, Any]:
    """
    投影转移失败记录，不执行数据库写入。

    识别结果与整理结果齐备时按完整字段落库；缺任一项则走「未识别到媒体信息」分支，
    此时只有文件名解析出的元数据可用，不写目标路径。
    :param fileitem: 源文件项
    :param mode: 整理方式
    :param meta: 文件名识别结果
    :param mediainfo: 媒体识别结果，未识别时为 None
    :param transferinfo: 整理结果，未进入整理时为 None
    :param downloader: 下载器
    :param download_hash: 种子 hash
    :param retry_count: 当前文件版本累计失败次数
    :param auto_paused: 是否已达到自动整理暂停阈值
    :return: 可构造类型化写入命令的历史字段
    """
    from app.application.transfer.feedback import classify_transfer_failure

    raw_error = transferinfo.message if transferinfo else "未识别到媒体信息"
    feedback = classify_transfer_failure(
        raw_error,
        overwrite_skipped=bool(transferinfo and transferinfo.overwrite_skipped),
    )
    # 源/目标路径已经是历史的一等字段，errmsg 继续保持旧接口的单条原因文本，
    # 阶段和恢复动作由独立字段承载，避免破坏已有客户端和导出脚本的解析。
    public_error = raw_error or "未知错误"
    if mediainfo and transferinfo:
        media_source, media_id = resolve_media_identity(media=mediainfo)
        history = dict(
            src=history_source_path(fileitem),
            src_storage=fileitem.storage,
            src_fileitem=fileitem.model_dump(),
            dest=transferinfo.target_item.path if transferinfo.target_item else None,
            dest_storage=transferinfo.target_item.storage if transferinfo.target_item else None,
            dest_fileitem=transferinfo.target_item.model_dump() if transferinfo.target_item else None,
            mode=mode,
            type=mediainfo.type.value,
            **classification_fields(mediainfo),
            title=history_title(meta, mediainfo),
            year=history_year(mediainfo.year or meta.year),
            media_source=media_source,
            media_id=media_id,
            music_type=getattr(mediainfo, "music_type", None),
            total_tracks=getattr(mediainfo, "total_tracks", None),
            audio_format=getattr(meta, "audio_format", None),
            audio_lossless=getattr(meta, "audio_lossless", None),
            bit_depth=getattr(meta, "bit_depth", None),
            sample_rate=getattr(meta, "sample_rate", None),
            bitrate=getattr(meta, "bitrate", None),
            seasons=meta.season,
            episodes=meta.episode,
            image=mediainfo.get_poster_image(),
            downloader=downloader,
            download_hash=download_hash,
            episode_group=mediainfo.episode_group,
            status=False,
            errmsg=public_error,
            failure_stage=transferinfo.failure_stage or feedback.stage.value,
            recovery_action=transferinfo.recovery_action or feedback.action,
            retry_count=retry_count,
            auto_paused=auto_paused,
            cleanup_status=transferinfo.cleanup_status,
            cleanup_error=transferinfo.cleanup_error,
            files=transferinfo.file_list,
        )
    else:
        media_source, media_id = resolve_media_identity(media=meta)
        history = dict(
            type=meta.type.value if meta.type else None,
            title=history_title(meta),
            year=history_year(meta.year),
            media_source=media_source,
            media_id=media_id,
            music_type=MUSIC_ENTITY_RECORDING if isinstance(meta, MetaMusic) else None,
            audio_format=getattr(meta, "audio_format", None),
            audio_lossless=getattr(meta, "audio_lossless", None),
            bit_depth=getattr(meta, "bit_depth", None),
            sample_rate=getattr(meta, "sample_rate", None),
            bitrate=getattr(meta, "bitrate", None),
            src=history_source_path(fileitem),
            src_storage=fileitem.storage,
            src_fileitem=fileitem.model_dump(),
            mode=mode,
            seasons=meta.season,
            episodes=meta.episode,
            downloader=downloader,
            download_hash=download_hash,
            status=False,
            errmsg=public_error,
            failure_stage=feedback.stage.value,
            recovery_action=feedback.action,
            retry_count=retry_count,
            auto_paused=auto_paused,
        )
    return history


class HistoryFileFingerprint(TypedDict):
    """查重描述与裁决共享的源文件版本字段。"""

    file_size: Optional[int]
    file_modify_time: Optional[float]
    fileid: Optional[str]


def history_file_fingerprint(fileitem: FileItem) -> HistoryFileFingerprint:
    """从同一个文件快照取出查重所需的版本指纹。"""
    return {
        "file_size": fileitem.size,
        "file_modify_time": fileitem.modify_time,
        "fileid": fileitem.fileid,
    }
