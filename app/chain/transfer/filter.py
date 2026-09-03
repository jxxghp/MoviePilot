"""整理文件筛选、音乐上下文与源目录清理判定。"""
import threading
from copy import deepcopy
from pathlib import Path
from typing import Dict, Optional, Protocol, Union

from app.application.configuration import (
    get_chain_runtime_config_snapshot,
)
from app.application.directory import DirectoryHelper
from app.application.history import (
    DownloadHistorySnapshot,
    TransferHistoryRepository,
    TransferHistorySnapshot,
    resolve_history,
)
from app.application.transfer.workflow import TransferTask
from app.chain._contracts import TransferMixinHost
from app.chain.media import MediaChain
from app.chain.storage import StorageChain
from app.chain.transfer.contract import _TransferOwnerBase
from app.domain.context import MediaInfo, MusicInfo
from app.domain.media import normalize_music_type
from app.domain.meta.metamusic import MetaMusic
from app.runtime.log import logger
from app.schemas.transfer import TransferInfo
from app.schemas.types import (
    MUSIC_ENTITY_ALBUM,
    MediaType,
)
from app.schemas.workflow import FileItem


class NetworkFilesystemPort(Protocol):
    """整理链判断本地路径文件系统类型所需的最小端口。"""

    def is_network_filesystem(
            self,
            path: Path,
            *,
            include_local_fuse: bool = False,
    ) -> bool:
        """判断路径是否位于网络或指定的本地 FUSE 文件系统。"""
        ...


_network_filesystem_lock = threading.RLock()
_network_filesystem_port: Optional[NetworkFilesystemPort] = None


def configure_network_filesystem_port(
        port: NetworkFilesystemPort,
) -> Optional[NetworkFilesystemPort]:
    """装配文件系统判定端口，并返回旧实现供隔离环境恢复。"""
    global _network_filesystem_port
    with _network_filesystem_lock:
        previous = _network_filesystem_port
        _network_filesystem_port = port
    return previous


def reset_network_filesystem_port(
        port: Optional[NetworkFilesystemPort] = None,
) -> None:
    """恢复指定文件系统判定端口；省略参数时回到未装配状态。"""
    global _network_filesystem_port
    with _network_filesystem_lock:
        _network_filesystem_port = port


def _network_filesystem_snapshot() -> NetworkFilesystemPort:
    """获取当前文件系统判定端口，未装配时稳定失败。"""
    with _network_filesystem_lock:
        port = _network_filesystem_port
    if port is None:
        raise RuntimeError("整理文件系统端口尚未由启动组合根装配")
    return port


class FileFilterMixin(_TransferOwnerBase):
    """提供整理文件筛选、音乐匹配和源目录清理判定。"""

    __mixin_host_protocol__ = TransferMixinHost

    @staticmethod
    def _requires_automatic_category(task: TransferTask) -> bool:
        """
        判断当前整理任务是否需要根据媒体识别结果自动创建类别目录。

        :param task: 整理任务
        :return: 是否必须具备自动分类结果
        """
        target_directory = task.target_directory
        if target_directory and DirectoryHelper.has_fixed_category(target_directory):
            return False
        if task.library_category_folder is not None:
            return bool(task.library_category_folder)
        return bool(
            target_directory and target_directory.library_category_folder
        )

    def _is_subtitle_file(self, fileitem: FileItem) -> bool:
        """
        判断是否为字幕文件
        """
        if not fileitem.extension:
            return False
        return (
            True if f".{fileitem.extension.lower()}" in self._subtitle_exts else False
        )

    def _is_audio_file(self, fileitem: FileItem) -> bool:
        """
        判断是否为音频文件
        """
        if not fileitem.extension:
            return False
        return True if f".{fileitem.extension.lower()}" in self._audio_exts else False

    @staticmethod
    def _is_music_lyrics_file(fileitem: FileItem) -> bool:
        """判断文件是否为可随同名音乐音轨迁移的歌词旁挂文件。"""
        path = str(fileitem.path or fileitem.name or "").casefold()
        return path.endswith((".lrc", ".txt", ".lyricsfile.yaml"))

    def _is_media_file(
            self,
            fileitem: FileItem,
            mtype: Optional[MediaType] = None,
    ) -> bool:
        """
        判断是否为主要媒体文件
        """
        if mtype == MediaType.MUSIC:
            if fileitem.type != "file" or not fileitem.extension:
                return False
            return f".{fileitem.extension.lower()}" in self._audio_exts
        if fileitem.type == "dir":
            # 蓝光原盘判断
            return StorageChain().is_bluray_folder(fileitem)
        if not fileitem.extension:
            return False
        extension = f".{fileitem.extension.lower()}"
        return extension in self._media_exts

    def _is_primary_media_file(
            self,
            fileitem: FileItem,
            mediainfo: Optional[MediaInfo | MusicInfo],
    ) -> bool:
        """判断文件在当前媒体上下文中是否属于主要媒体文件。"""
        return self._is_media_file(
            fileitem,
            getattr(mediainfo, "type", None),
        )

    @staticmethod
    def _music_info_from_meta(meta: MetaMusic) -> MusicInfo:
        """将音频文件标签解析结果转换为可整理的最小音乐信息。"""
        return MusicInfo.from_meta(meta)

    @classmethod
    def _match_music_album_context(
            cls,
            file_item: FileItem,
            file_path: Path,
            file_meta: MetaMusic,
    ) -> tuple[MetaMusic, Optional[MusicInfo]]:
        """为缺少远端身份的本地音频尝试目录级专辑匹配，命中后回填文件元数据。

        WAV 等无标签文件只能依靠目录结构和曲目特征识别；匹配结果由 MediaChain
        按目录缓存，同一专辑目录内的后续文件不会重复请求远端。
        """
        # 目录级匹配需要读取本地音频时长，远端存储文件无法参与
        if file_meta.media_id or getattr(file_item, "storage", "local") != "local":
            return file_meta, None
        try:
            matched = MediaChain().recognize_music_album_directory(file_path.parent)
        except Exception as err:
            logger.debug(f"音乐专辑目录匹配失败：{file_path} - {err}")
            return file_meta, None
        info = matched.get(str(file_path.resolve()))
        if not info or not info.media_id:
            return file_meta, None
        logger.info(f"{file_path.name} 通过专辑目录匹配识别为：{info.artist} - {info.title}")
        merged_meta = deepcopy(file_meta)
        # 保留本地音频的实际技术参数，仅回填身份和名称字段
        if info.title:
            merged_meta.title = info.title
        if info.artists:
            merged_meta.artists = list(info.artists)
        if info.album:
            merged_meta.album = info.album
        if info.album_artist:
            merged_meta.album_artist = info.album_artist
        if info.year:
            merged_meta.year = info.year
        if info.disc_number:
            merged_meta.disc_number = info.disc_number
        if info.track_number:
            merged_meta.track_number = info.track_number
        if info.total_tracks:
            merged_meta.total_tracks = info.total_tracks
        merged_meta.media_source = info.media_source
        merged_meta.media_id = info.media_id
        merged_info = cls._music_info_from_meta(merged_meta)
        # 补齐曲目级远端信息，供后续刮削和展示使用
        merged_info.music_type = info.music_type
        merged_info.artist_ids = list(info.artist_ids)
        merged_info.album_id = info.album_id
        merged_info.album_type = info.album_type
        merged_info.secondary_types = list(info.secondary_types)
        merged_info.release_date = info.release_date
        merged_info.release_status = info.release_status
        merged_info.cover_url = info.cover_url
        merged_info.set_library_category(info.library_category)
        merged_info.metadata_category = info.metadata_category
        merged_info.classification = deepcopy(info.classification)
        merged_info.genres = list(info.genres)
        merged_info.tags = list(info.tags)
        merged_info.artist_country = info.artist_country
        merged_info.names = list(info.names)
        merged_info.detail_link = info.detail_link
        merged_info.listen_count = info.listen_count
        merged_info.raw_data = deepcopy(info.raw_data)
        return merged_meta, merged_info

    @staticmethod
    def _download_history_music_type(
            download_history: Optional[DownloadHistorySnapshot],
    ) -> Optional[str]:
        """从下载历史字段或旧版音乐备注中恢复音乐实体类型。"""
        music_type = normalize_music_type(
            getattr(download_history, "music_type", None),
            allow_artist=False,
        )
        if music_type:
            return music_type
        note = getattr(download_history, "note", None)
        music_note = note.get("music") if isinstance(note, dict) else None
        media_payload = music_note.get("media") if isinstance(music_note, dict) else None
        if not isinstance(media_payload, dict):
            return None
        return normalize_music_type(
            media_payload.get("music_type"),
            allow_artist=False,
        )

    @classmethod
    def _restore_music_download_context(
            cls,
            download_history: Optional[DownloadHistorySnapshot],
            file_path: Path,
    ) -> tuple[Optional[MetaMusic], Optional[MusicInfo]]:
        """从下载历史恢复音乐上下文，并用当前音频标签覆盖曲目级字段。"""
        note = getattr(download_history, "note", None)
        music_note = note.get("music") if isinstance(note, dict) else None
        if not isinstance(music_note, dict) or music_note.get("version") != 1:
            return None, None
        try:
            saved_meta = MetaMusic.from_dict(music_note.get("meta") or {})
            saved_info = MusicInfo.from_dict(music_note.get("media") or {})
        except (TypeError, ValueError):
            return None, None

        file_tags = MediaChain.read_path_meta(file_path)
        file_meta = deepcopy(saved_meta)
        file_meta.org_string = file_path.name
        # 曲目标题始终优先使用当前文件自身的标签（缺失时回退为文件名），
        # 防止整包目录继续沿用订阅/下载标题（单曲名、专辑名等）导致所有文件重名。
        if file_tags.title:
            file_meta.title = file_tags.title
        is_album_context = saved_info.music_type == MUSIC_ENTITY_ALBUM
        for field_name in (
                "artists",
                "disc_number",
                "track_number",
                "total_discs",
                "version",
                "isrc",
        ):
            if getattr(file_tags, field_name, None):
                setattr(file_meta, field_name, deepcopy(getattr(file_tags, field_name)))
        for field_name in ("album", "album_artist", "year", "total_tracks"):
            file_value = getattr(file_tags, field_name, None)
            # 整专下载以订阅选中的专辑字段为准，避免单个错误标签把曲目拆到其它专辑目录。
            if file_value and (not is_album_context or not getattr(file_meta, field_name, None)):
                setattr(file_meta, field_name, deepcopy(file_value))
        for field_name in (
                "audio_format",
                "bit_depth",
                "sample_rate",
                "bitrate",
                "duration",
        ):
            if getattr(file_tags, field_name, None):
                setattr(file_meta, field_name, getattr(file_tags, field_name))
        file_meta.media_source = saved_info.media_source or saved_meta.media_source
        file_meta.media_id = saved_info.media_id or saved_meta.media_id

        file_info = cls._music_info_from_meta(file_meta)
        file_info.media_source = saved_info.media_source
        file_info.media_id = saved_info.media_id
        file_info.music_type = saved_info.music_type
        file_info.artist_ids = list(saved_info.artist_ids)
        file_info.album_id = saved_info.album_id
        file_info.album_type = saved_info.album_type
        file_info.release_date = saved_info.release_date
        file_info.cover_url = saved_info.cover_url
        file_info.lyrics = saved_info.lyrics
        file_info.category = saved_info.category
        file_info.genres = list(saved_info.genres)
        file_info.detail_link = saved_info.detail_link
        file_info.listen_count = saved_info.listen_count
        return file_meta, file_info

    @staticmethod
    def _is_music_retry_source(
            history: TransferHistorySnapshot,
            src_path: Path,
    ) -> bool:
        """
        判断重新整理来源是否应走音乐链路：历史类型为音乐，或源路径为音频文件。
        """
        if history.type == MediaType.MUSIC.value:
            return True
        return src_path.suffix.lower() in get_chain_runtime_config_snapshot().audio_extensions

    def _recognize_music_retry_media(
            self,
            history: TransferHistorySnapshot,
            src_path: Path,
    ) -> Optional[Union[MusicInfo, MediaInfo]]:
        """
        重新整理重试时恢复音乐信息。

        优先按历史记录中的 MusicBrainz 身份恢复；单音频文件回退按音频标签与文件名识别；
        音乐专辑目录返回 None，交由整理链按音频后缀逐文件解析识别。
        """
        if history.media_source and history.media_id:
            retry_info = MediaChain().recognize_media(
                mtype=MediaType.MUSIC,
                media_source=history.media_source,
                media_id=history.media_id,
                music_type=getattr(history, "music_type", None),
            )
            if retry_info:
                return retry_info
        if src_path.is_file():
            # 音频走统一路径识别入口，自动路由到音乐识别链
            recognize_context = MediaChain().recognize_by_path(str(src_path))
            return recognize_context.media_info if recognize_context else None
        return None

    def _is_allowed_file(self, fileitem: FileItem) -> bool:
        """
        判断是否允许的扩展名
        """
        if not fileitem.extension:
            return False
        return True if f".{fileitem.extension.lower()}" in self._allowed_exts else False

    @staticmethod
    def _is_allow_filesize(fileitem: FileItem, min_filesize: int) -> bool:
        """
        判断是否满足最小文件大小
        """
        return (
            True
            if not min_filesize or (fileitem.size or 0) > min_filesize * 1024 * 1024
            else False
        )

    @staticmethod
    def _is_hidden_or_recycle_path(file_path: Optional[str]) -> bool:
        """
        判断是否隐藏或回收站路径
        """
        if not file_path:
            return False
        normalized_path = file_path.replace("\\", "/")
        return (
                "/@Recycle/" in normalized_path
                or "/#recycle/" in normalized_path
                or "/." in normalized_path
                or "/@eaDir" in normalized_path
        )

    @staticmethod
    def _should_delete_empty_source_directories(
            task: TransferTask,
            delete_mounted_local_disk_empty_dirs: bool,
            mounted_filesystem_cache: Dict[Path, bool],
    ) -> bool:
        """
        判断移动整理后是否应删除源空目录。

        仅在关闭挂载盘空目录清理且源存储为本地时检测文件系统，
        避免默认流程产生额外系统调用。
        """
        if delete_mounted_local_disk_empty_dirs:
            return True
        if task.fileitem.storage != "local":
            return True

        source_directory = (
            Path(task.target_directory.download_path)
            if task.target_directory and task.target_directory.download_path
            else Path(task.fileitem.path).parent
        )
        if source_directory not in mounted_filesystem_cache:
            mounted_filesystem_cache[source_directory] = (
                _network_filesystem_snapshot().is_network_filesystem(
                    source_directory, include_local_fuse=True
                )
            )
        return not mounted_filesystem_cache[source_directory]

    @staticmethod
    def _is_overwrite_declined(task: TransferTask, transferinfo: TransferInfo,
                               transferhis: TransferHistoryRepository) -> bool:
        """
        判断本次未入库是否为「同路径已有成功记录 + 覆盖模式裁定不覆盖」。

        只有同路径此前已成功整理过才需要保护：这类文件是查重闸放行的同路径新版本，
        媒体库中的原有版本仍然在位，不应因一次不覆盖裁决把成功记录改写成失败记录。
        没有成功记录时（如目标同名文件来自其他源路径）保持原有失败语义，
        用户仍能在历史与通知中看到裁决结果。
        :param task: 整理任务
        :param transferinfo: 整理结果
        :param transferhis: 历史操作对象
        :return: True 表示应保留原成功记录
        """
        if (
                not transferinfo.overwrite_skipped
                or not task.fileitem
                or not task.fileitem.path
        ):
            return False
        try:
            history = resolve_history(
                task.fileitem.path,
                storage=task.fileitem.storage,
                transfer_history_oper=transferhis,
            )
        except Exception as err:
            logger.error(f"查询整理历史失败: {task.fileitem.path} - {err}")
            return False
        return bool(history and history.status)
