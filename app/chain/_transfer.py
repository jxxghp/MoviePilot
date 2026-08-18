"""整理链功能域 mixin。

TransferChain 从 5000+ 行的单体拆出这些内聚功能域，每个 mixin 只承载一类
整理辅助逻辑；主流程（do_transfer / manual_transfer / remote_transfer）仍留在
TransferChain 中。mixin 方法运行时经 MRO 解析，共享 TransferChain 实例状态。

注意：这里的方法均已去掉私有名前缀双下划线（__ -> _），因为 Python 的名字
改编按定义类生效，方法迁到 mixin 后 __ 前缀会改变改编目标，导致跨类调用失败。
"""
import asyncio
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from app.schemas.history import DownloadHistory as _SchemaDownloadHistory
from app.schemas.transfer import EpisodeFormatRule as _SchemaEpisodeFormatRule
from app.adapters.system.host import SystemUtils
from app.application.agent import build_manual_redo_prompt, get_running_agent_manager
from app.application.formatting import EpisodeFormatRuleHelper
from app.application.history import clear_transfer_failures, resolve_history
from app.application.transfer import TransferTask, job_lock
from app.chain.media import MediaChain
from app.chain.storage import StorageChain
from app.chain.subscribe import SubscribeChain
from app.db.models.downloadhistory import DownloadFiles, DownloadHistory
from app.db.models.transferhistory import TransferHistory
from app.db.oper.downloadhistory import DownloadHistoryOper
from app.db.oper.systemconfig import SystemConfigOper
from app.db.oper.transferhistory import TransferHistoryOper
from app.domain.context import MediaInfo, MusicInfo
from app.domain.media import normalize_music_type
from app.domain.meta.metabase import MetaBase
from app.domain.meta.metamusic import MetaMusic
from app.foundation import text as text_tools
from app.runtime.config import global_vars, settings
from app.runtime.log import logger
from app.schemas.workflow import FileItem
from app.schemas.message import Message
from app.schemas.tmdb import TmdbEpisode
from app.schemas.transfer import TransferInfo
from app.schemas.types import (
    MUSIC_ENTITY_ALBUM,
    EventType,
    MediaSource,
    MediaType,
    NotificationChannel,
    ReplyMode,
    SystemConfigKey,
)

# 字幕文件常见的语言/默认/强制标记，整理同名字幕时只允许剥离这些字幕专属尾缀。
SUBTITLE_STEM_TAGS = {
    "cc",
    "chi",
    "chs",
    "cht",
    "cn",
    "default",
    "en",
    "eng",
    "english",
    "forced",
    "gb",
    "gb2312",
    "hk",
    "ja",
    "jap",
    "japanese",
    "jp",
    "jpn",
    "sc",
    "sdh",
    "tc",
    "zh",
    "zh-cn",
    "zh-hans",
    "zh-hant",
    "zh-tw",
    "zh_cn",
    "zh_hans",
    "zh_hant",
    "zh_tw",
    "zho",
    "中英",
    "中字",
    "双语",
    "简中",
    "简体",
    "繁中",
    "繁体",
}


class FileFilterMixin:
    @staticmethod
    def _requires_automatic_category(task: TransferTask) -> bool:
        """
        判断当前整理任务是否需要根据媒体识别结果自动创建类别目录。

        :param task: 整理任务
        :return: 是否必须具备自动分类结果
        """
        target_directory = task.target_directory
        if target_directory and target_directory.media_category:
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
        merged_info.release_date = info.release_date
        merged_info.cover_url = info.cover_url
        merged_info.category = info.category
        merged_info.genres = list(info.genres)
        merged_info.detail_link = info.detail_link
        return merged_meta, merged_info

    @staticmethod
    def _download_history_music_type(
            download_history: Optional[DownloadHistory],
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
            download_history: Optional[DownloadHistory],
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
    def _is_music_retry_source(history: TransferHistory, src_path: Path) -> bool:
        """
        判断重新整理来源是否应走音乐链路：历史类型为音乐，或源路径为音频文件。
        """
        if history.type == MediaType.MUSIC.value:
            return True
        return src_path.suffix.lower() in settings.RMT_AUDIOEXT

    def _recognize_music_retry_media(
            self,
            history: TransferHistory,
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
                SystemUtils.is_network_filesystem(
                    source_directory, include_local_fuse=True
                )
            )
        return not mounted_filesystem_cache[source_directory]

    @staticmethod
    def _is_overwrite_declined(task: TransferTask, transferinfo: TransferInfo,
                               transferhis: TransferHistoryOper) -> bool:
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
        if not transferinfo.overwrite_skipped or not task.fileitem:
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


class ScrapeBatchMixin:

    def _send_metadata_scrape_event(
            self, task: TransferTask, transferinfo: TransferInfo
    ):
        """
        发送元数据刮削事件，保持对外事件载荷兼容。
        """
        if (
                not task
                or not transferinfo
                or not transferinfo.need_scrape
                or not self._is_primary_media_file(task.fileitem, task.mediainfo)
        ):
            return

        target_diritem = transferinfo.target_diritem
        if not target_diritem:
            return

        self.eventmanager.send_event(
            EventType.MetadataScrape,
            self._build_metadata_scrape_payload(
                task=task,
                fileitem=target_diritem,
                file_list=transferinfo.file_list_new,
                overwrite=False,
            ),
        )

    @staticmethod
    def _build_metadata_scrape_payload(
            task: TransferTask,
            fileitem: FileItem,
            file_list: Optional[list[str]],
            overwrite: bool,
    ) -> dict[str, Any]:
        """构造刮削事件载荷，并为音乐批次保留逐文件身份上下文。"""
        paths = list(dict.fromkeys(file_list or []))
        payload: dict[str, Any] = {
            "meta": task.meta,
            "mediainfo": task.mediainfo,
            "fileitem": fileitem,
            "file_list": paths,
            "overwrite": overwrite,
        }
        if isinstance(task.mediainfo, MusicInfo):
            payload["file_contexts"] = [
                {
                    "path": path,
                    "meta": task.meta,
                    "mediainfo": task.mediainfo,
                }
                for path in paths
            ]
        return payload

    def _register_scrape_batch_task(self, task: TransferTask):
        """
        登记批次任务。刮削事件只在批次关闭且任务全部完成后统一发送。
        """
        if not task or not task.transfer_batch_id:
            return
        with job_lock:
            batch = self._scrape_batches.setdefault(
                task.transfer_batch_id,
                {
                    "pending": set(),
                    "targets": {},
                    "closed": False,
                },
            )
            batch["pending"].add(task.fileitem.path)

    def _close_scrape_batch(self, batch_id: Optional[str]):
        """
        标记批次不再接收新任务，并尝试发送已聚合的刮削事件。
        """
        if not batch_id:
            return
        with job_lock:
            batch = self._scrape_batches.setdefault(
                batch_id,
                {
                    "pending": set(),
                    "targets": {},
                    "closed": False,
                },
            )
            batch["closed"] = True
        self._flush_scrape_batch_if_ready(batch_id)

    def _record_scrape_target(self, task: TransferTask, transferinfo: TransferInfo):
        """
        记录批次内需要刮削的目标文件，按目标媒体根目录聚合。
        """
        if (
                not task
                or not task.transfer_batch_id
                or not transferinfo
                or not transferinfo.need_scrape
                or not self._is_primary_media_file(task.fileitem, task.mediainfo)
        ):
            return

        target_diritem = transferinfo.target_diritem
        if not target_diritem:
            return

        target_files = transferinfo.file_list_new or []
        target_key = (target_diritem.storage, target_diritem.path)
        with job_lock:
            batch = self._scrape_batches.setdefault(
                task.transfer_batch_id,
                {
                    "pending": set(),
                    "targets": {},
                    "closed": False,
                },
            )
            target = batch["targets"].setdefault(
                target_key,
                {
                    "fileitem": target_diritem,
                    "meta": task.meta,
                    "mediainfo": task.mediainfo,
                    "files": [],
                    "file_contexts": {},
                    "overwrite": False,
                },
            )
            if not target.get("meta"):
                target["meta"] = task.meta
            if not target.get("mediainfo"):
                target["mediainfo"] = task.mediainfo
            for target_file in target_files:
                if target_file and target_file not in target["files"]:
                    target["files"].append(target_file)
                if target_file and isinstance(task.mediainfo, MusicInfo):
                    target["file_contexts"][target_file] = {
                        "path": target_file,
                        "meta": task.meta,
                        "mediainfo": task.mediainfo,
                    }

    def _finish_scrape_batch_task(self, task: TransferTask):
        """
        标记批次内单个任务已结束。
        """
        if not task or not task.transfer_batch_id:
            return
        with job_lock:
            batch = self._scrape_batches.get(task.transfer_batch_id)
            if not batch:
                return
            batch["pending"].discard(task.fileitem.path)
        self._flush_scrape_batch_if_ready(task.transfer_batch_id)

    def _flush_scrape_batch_if_ready(self, batch_id: Optional[str]):
        """
        批次任务全部结束后发送聚合后的刮削事件。
        """
        if not batch_id:
            return

        with job_lock:
            batch = self._scrape_batches.get(batch_id)
            if (
                    not batch
                    or not batch.get("closed")
                    or batch.get("pending")
            ):
                return
            targets = list(batch.get("targets", {}).values())
            self._scrape_batches.pop(batch_id, None)

        for target in targets:
            fileitem = target.get("fileitem")
            if not fileitem:
                continue
            file_list = list(dict.fromkeys(target.get("files") or []))
            file_contexts = target.get("file_contexts") or {}
            payload = {
                "meta": target.get("meta"),
                "mediainfo": target.get("mediainfo"),
                "fileitem": fileitem,
                "file_list": file_list,
                "overwrite": target.get("overwrite", False),
            }
            if file_contexts:
                payload["file_contexts"] = [
                    file_contexts[path]
                    for path in file_list
                    if path in file_contexts
                ]
            self.eventmanager.send_event(
                EventType.MetadataScrape,
                payload,
            )


class EpisodeFormatMixin:

    def recommend_name(self, meta: MetaBase, mediainfo: MediaInfo) -> Optional[str]:
        """
        获取重命名后的名称
        :param meta: 元数据
        :param mediainfo: 媒体信息
        :return: 重命名后的名称（含目录）
        """
        # 获取集信息，供重命名模块使用
        episodes_info: Optional[List[TmdbEpisode]] = None
        if mediainfo.type == MediaType.TV:
            # 判断注意season为0的情况
            season_num = mediainfo.season
            if season_num is None and meta.season_seq:
                if meta.season_seq.isdigit():
                    season_num = int(meta.season_seq)
            # 默认值1
            if season_num is None:
                season_num = 1
            episodes_info = self.unicast(
                "tmdb_episodes",
                tmdbid=mediainfo.tmdb_id,
                season=season_num,
                episode_group=mediainfo.episode_group,
            )
        if episodes_info:
            return self.unicast(
                "recommend_name",
                meta=meta,
                mediainfo=mediainfo,
                episodes_info=episodes_info,
            )
        # 电影或无集信息时保持原有参数集，避免影响旧签名的模块实现
        return self.unicast("recommend_name", meta=meta, mediainfo=mediainfo)

    def recommend_episode_format(
            self,
            fileitem: FileItem,
            fileitems: Optional[List[FileItem]] = None,
    ) -> Tuple[bool, str, Optional[dict]]:
        """
        根据目录样本推荐集数定位模板
        """
        if not fileitem and not fileitems:
            logger.warn("推荐集数定位模板失败：缺少目录参数")
            return False, "缺少目录参数", None

        rules = self._get_episode_format_rules()
        if fileitems:
            state, errmsg, sample_files = self._get_selected_episode_format_sample_files(
                fileitems
            )
            if not state:
                logger.warn(f"推荐集数定位模板失败：{errmsg}")
                return False, errmsg, None
            target_path = sample_files[0].path if sample_files else None
        else:
            if not fileitem or not fileitem.path:
                logger.warn("推荐集数定位模板失败：缺少目录参数")
                return False, "缺少目录参数", None
            directory = self._resolve_episode_format_directory(fileitem)
            if not directory or directory.type != "dir":
                logger.warn(f"推荐集数定位模板失败：目录不存在 - {fileitem.path}")
                return False, "目录不存在", None
            sample_files = self._get_episode_format_sample_files(directory)
            target_path = directory.path
        logger.info(
            f"开始匹配集数定位规则：{target_path}，规则数 {len(rules)}，样本数 {len(sample_files)}"
        )
        state, errmsg, data = EpisodeFormatRuleHelper().recommend(
            rules=rules,
            sample_files=sample_files,
        )
        if not state:
            logger.warn(f"集数定位模板推荐失败：{target_path} - {errmsg}")
            return state, errmsg, data
        logger.info(
            f"集数定位模板推荐成功：{target_path} - 规则 {data.get('rule_name') if data else None}"
        )
        return state, errmsg, data

    @staticmethod
    def _get_episode_format_rules() -> List[_SchemaEpisodeFormatRule]:
        """
        获取启用的集数定位规则
        """
        rule_items = SystemConfigOper().get(SystemConfigKey.EpisodeFormatRuleTable) or []
        rules: List[_SchemaEpisodeFormatRule] = []
        for item in rule_items:
            if not isinstance(item, dict):
                continue
            try:
                rule = _SchemaEpisodeFormatRule(**item)
            except Exception as err:
                logger.warn(f"忽略无效的集数定位规则：{err}")
                continue
            if rule.enabled:
                rules.append(rule)
        return sorted(rules, key=lambda item: item.order)

    def _resolve_episode_format_directory(
            self, fileitem: FileItem
    ) -> Optional[FileItem]:
        """
        将文件或目录入参归一化为目录对象
        """
        storage_chain = StorageChain()
        if fileitem.type == "dir":
            return storage_chain.get_item(fileitem)
        source_path = Path(fileitem.path)
        parent_item = FileItem(
            storage=fileitem.storage,
            path=source_path.parent.as_posix(),
            type="dir",
            name=source_path.parent.name,
        )
        return storage_chain.get_item(parent_item)

    def _get_selected_episode_format_sample_files(
            self, fileitems: List[FileItem]
    ) -> Tuple[bool, str, List[FileItem]]:
        """
        获取当前选择文件中可参与模板推荐的样本文件。
        """
        if not fileitems:
            return False, "没有可用于识别的样本文件", []

        expected_dir_key: Optional[Tuple[str, str]] = None
        selected_files: List[FileItem] = []
        seen_files = set()
        for item in fileitems:
            if not item or not item.path or item.type != "file":
                return False, "当前选择不满足智能识别条件", []

            dir_key = (
                item.storage or "local",
                Path(item.path).parent.as_posix(),
            )
            if expected_dir_key is None:
                expected_dir_key = dir_key
            elif dir_key != expected_dir_key:
                return False, "当前选择不满足智能识别条件", []

            file_key = (item.storage or "local", item.path)
            if file_key in seen_files:
                continue
            seen_files.add(file_key)

            if not (
                    self._is_media_file(item)
                    or self._is_subtitle_file(item)
                    or self._is_audio_file(item)
            ):
                continue
            if self._is_hidden_or_recycle_path(item.path):
                continue
            selected_files.append(item)

        if not selected_files:
            return False, "没有可用于识别的样本文件", []
        return True, "", selected_files

    def _get_episode_format_sample_files(
            self, directory: FileItem
    ) -> List[FileItem]:
        """
        获取目录下可参与模板推荐的样本文件。

        推荐结果最终会在手动整理链路中作为 `episode_format`
        交由 `FormatParser` 过滤主视频、字幕和外挂音频，因此这里需要把
        同目录下的主视频、字幕和外挂音频一起纳入推荐流程。
        """
        file_items = StorageChain().list_files(directory, recursion=False) or []
        sample_files: List[FileItem] = []
        for item in file_items:
            if not item or item.type != "file":
                continue
            if not (
                    self._is_media_file(item)
                    or self._is_subtitle_file(item)
                    or self._is_audio_file(item)
            ):
                continue
            if self._is_hidden_or_recycle_path(item.path):
                continue
            sample_files.append(item)
        return sample_files


class HistoryMatchMixin:
    @staticmethod
    def _match_download_file(
            download_file: DownloadFiles,
            file_path: Path,
            save_path: Path,
    ) -> bool:
        """
        判断下载文件记录是否明确对应当前文件。
        """
        if download_file.fullpath == file_path.as_posix():
            return True

        filepath = download_file.filepath
        if not filepath:
            return False

        try:
            return (save_path / Path(filepath)).as_posix() == file_path.as_posix()
        except (TypeError, ValueError):
            return False

    def _resolve_history_from_download_files(
            self,
            downloadhis: DownloadHistoryOper,
            download_files: List[DownloadFiles],
            file_path: Optional[Path] = None,
            save_path: Optional[Path] = None,
    ) -> Optional[DownloadHistory]:
        """
        从下载文件记录中解析唯一的下载历史。
        """
        if file_path and save_path:
            download_files = [
                download_file
                for download_file in download_files
                if self._match_download_file(
                    download_file=download_file,
                    file_path=file_path,
                    save_path=save_path,
                )
            ]

        download_hashes = {
            download_file.download_hash
            for download_file in download_files
            if download_file.download_hash
        }
        if len(download_hashes) == 1:
            return downloadhis.get_by_hash(next(iter(download_hashes)))
        return None

    def _resolve_download_history(
            self,
            downloadhis: DownloadHistoryOper,
            file_path: Path,
            bluray_dir: bool = False,
            download_hash: Optional[str] = None,
    ) -> Optional[DownloadHistory]:
        """
        根据显式 hash、文件路径或种子根目录回查下载历史。
        """
        if download_hash:
            return downloadhis.get_by_hash(download_hash)

        if bluray_dir:
            return downloadhis.get_by_path(file_path.as_posix())

        download_file = downloadhis.get_file_by_fullpath(file_path.as_posix())
        if download_file:
            return downloadhis.get_by_hash(download_file.download_hash)

        # 多文件种子里的字幕/附加文件可能没有稳定的 fullpath 记录，
        # 退回到父目录和 savepath 继续查找，尽量补齐同一种子的关联信息。
        shared_download_roots = self._get_shared_download_roots(file_path)

        for parent_path in file_path.parents:
            parent_posix = parent_path.as_posix()
            download_files = downloadhis.get_files_by_savepath(parent_posix) or []

            if parent_posix in shared_download_roots:
                # 共享下载根目录只能接受有明确文件记录的匹配，
                # 避免单文件/磁力任务把整个根目录污染成同一媒体。
                history = self._resolve_history_from_download_files(
                    downloadhis=downloadhis,
                    download_files=download_files,
                    file_path=file_path,
                    save_path=parent_path,
                )
                if history:
                    return history
                break

            download_history = downloadhis.get_by_path(parent_posix)
            if download_history:
                return download_history

            history = self._resolve_history_from_download_files(
                downloadhis=downloadhis,
                download_files=download_files,
            )
            if history:
                return history

        return None

    @staticmethod
    def _is_movie_year_conflict(
            file_meta: MetaBase,
            # 两种 DownloadHistory 都会进来：库模型（本文件按 ORM 行查历史）与
            # schemas DTO（TransferTask.download_history）。本函数只按 getattr 取
            # year 与 type，对两者一视同仁
            media: Union[DownloadHistory, _SchemaDownloadHistory, MediaInfo, MusicInfo]
    ) -> bool:
        """
        判断文件名年份是否与已识别电影年份冲突。

        多电影合集只保存一条下载历史，不能把合集首部电影的媒体 ID 套用到其它年份的文件；
        电视剧季包仍应继续复用同一条下载历史。
        """
        file_year = getattr(file_meta, "year", None)
        media_year = getattr(media, "year", None)
        if not file_meta or not media or not file_year or not media_year:
            return False
        media_type = getattr(media, "type", None)
        if not isinstance(media_type, MediaType):
            try:
                media_type = MediaType(media_type)
            except (TypeError, ValueError):
                return False
        return (
                media_type == MediaType.MOVIE
                and str(file_year) != str(media_year)
        )

    @staticmethod
    def _optional_attr_equal(
            source: MetaBase,
            target: MetaBase,
            attr: str,
            normalizer: Callable = None,
    ) -> bool:
        """
        比较可选识别字段。

        字段两边都没有识别到时不参与判断；只要任意一边识别到了，就要求两边值一致，
        避免把同名不同年份或不同季集的附加文件误归到当前主视频。
        """
        source_value = getattr(source, attr, None)
        target_value = getattr(target, attr, None)
        if source_value is None and target_value is None:
            return True
        if source_value is None or target_value is None:
            return False
        if normalizer:
            source_value = normalizer(source_value)
            target_value = normalizer(target_value)
        return source_value == target_value

    def _is_same_media_meta(
            self, source_meta: MetaBase, target_meta: MetaBase
    ) -> bool:
        """
        判断两个文件识别出的媒体身份是否一致。
        """
        if not source_meta or not target_meta:
            return False
        if source_meta.type != target_meta.type:
            return False
        if text_tools.normalize_upper(source_meta.name) != text_tools.normalize_upper(
                target_meta.name
        ):
            return False
        if not self._optional_attr_equal(source_meta, target_meta, "year", str):
            return False
        for attr in (
                "begin_season",
                "end_season",
                "begin_episode",
                "end_episode",
        ):
            if not self._optional_attr_equal(source_meta, target_meta, attr, int):
                return False
        return True


class FileKeyMixin:
    @staticmethod
    def _get_file_key(fileitem: FileItem) -> Tuple[str, str]:
        """
        获取文件缓存键。
        """
        normalized_path = Path(str(fileitem.path).replace("\\", "/")).as_posix()
        return fileitem.storage or "local", normalized_path

    @staticmethod
    def _get_file_stem(fileitem: FileItem) -> str:
        """
        获取文件主干名，用于判断同名附加文件。
        """
        file_name = fileitem.name or Path(fileitem.path).name
        return Path(file_name).stem.lower()

    @classmethod
    def _get_subtitle_media_stem(cls, subtitle_fileitem: FileItem) -> str:
        """
        获取字幕对应主视频的候选主干名。
        """
        current_stem = cls._get_file_stem(subtitle_fileitem)
        while current_stem:
            media_stem, separator, suffix = current_stem.rpartition(".")
            if not separator or suffix not in SUBTITLE_STEM_TAGS:
                return current_stem
            current_stem = media_stem
        return current_stem

    def _get_extra_media_stem(self, extra_fileitem: FileItem) -> str:
        """
        获取附加文件对应主视频的候选主干名。
        """
        if self._is_subtitle_file(extra_fileitem):
            return self._get_subtitle_media_stem(extra_fileitem)
        return self._get_file_stem(extra_fileitem)

    def _get_related_main_file_key(
            self,
            extra_fileitem: FileItem,
            main_fileitems: List[FileItem],
    ) -> Optional[Tuple[str, str]]:
        """
        获取与附加文件名完全匹配的主视频键。
        """
        if not (
                self._is_subtitle_file(extra_fileitem)
                or self._is_audio_file(extra_fileitem)
        ):
            return None

        extra_media_stem = self._get_extra_media_stem(extra_fileitem)
        matched_items: List[FileItem] = []
        for main_fileitem in main_fileitems:
            main_stem = self._get_file_stem(main_fileitem)
            if main_stem and main_stem == extra_media_stem:
                matched_items.append(main_fileitem)

        if len(matched_items) != 1:
            return None
        return self._get_file_key(matched_items[0])

    @staticmethod
    def _normalize_dir_path(dir_path: Union[str, Path]) -> str:
        """
        归一化目录路径，用于同一父目录候选缓存。
        """
        normalized = Path(dir_path).as_posix().rstrip("/")
        return normalized or "/"

    def _get_dir_key(self, dir_item: FileItem) -> Tuple[str, str]:
        """
        获取目录缓存键。
        """
        return dir_item.storage, self._normalize_dir_path(dir_item.path)

    def _get_file_parent_key(self, current_item: FileItem) -> Tuple[str, str]:
        """
        获取文件父目录缓存键。
        """
        return (
            current_item.storage,
            self._normalize_dir_path(Path(current_item.path).parent),
        )


class ManualHistoryMixin:
    @staticmethod
    def _get_subscribe_custom_words(
            history_record: Optional[DownloadHistory],
    ) -> Optional[List[str]]:
        """
        获取整理用自定义识别词：优先使用下载时保存的快照，无快照（历史旧记录）时再按来源实时反查订阅。

        快照优先可避免整理阶段因订阅季号漂移、来源解析失败或订阅完成被删导致识别词丢失，从而原样入库到偏移前的季集。
        """
        if not history_record:
            return None
        # 下载时保存的完整订阅识别词快照优先
        if history_record.custom_words:
            return history_record.custom_words.split("\n")
        # 兜底：历史旧记录无快照时，按下载来源实时反查订阅
        if not isinstance(history_record.note, dict):
            return None
        subscribe = SubscribeChain().get_subscribe_by_source(
            history_record.note.get("source")
        )
        return (
            subscribe.custom_words.split("\n")
            if subscribe and subscribe.custom_words
            else None
        )

    @staticmethod
    def _is_successful_move_history(history: Optional[TransferHistory]) -> bool:
        """判断历史记录是否为已成功完成的移动类整理。"""
        return bool(
            history
            and history.status
            and history.mode
            and "move" in history.mode
        )

    def _get_manual_transfer_history(
            self,
            fileitem: FileItem,
            transfer_history_oper: TransferHistoryOper,
            include_move_dest: bool = False,
    ) -> Optional[TransferHistory]:
        """查询文件源路径历史，并兼容从成功移动后的目标现址重新整理。"""
        # resolve_history 在命中失败记录时会再确认一次有无成功记录，
        # 避免 get_by_src 无排序导致同源多行时返回哪条不确定
        history = resolve_history(
            fileitem.path,
            storage=fileitem.storage,
            transfer_history_oper=transfer_history_oper,
        )
        if history or not include_move_dest:
            return history

        history = transfer_history_oper.get_by_dest(
            fileitem.path,
            storage=fileitem.storage,
        )
        return history if self._is_successful_move_history(history) else None

    def get_manual_transfer_histories(
            self,
            fileitems: List[FileItem],
    ) -> List[TransferHistory]:
        """
        查询文件或目录命中的成功整理记录，供手动整理界面显示重整状态。

        :param fileitems: 待查询的文件或目录项
        :return: 去重后的成功整理记录
        """
        transfer_history_oper = TransferHistoryOper()
        histories: Dict[int, TransferHistory] = {}
        for fileitem in fileitems or []:
            if not fileitem or not fileitem.path:
                continue
            storage = fileitem.storage or "local"
            if fileitem.type == "dir":
                matched_histories = transfer_history_oper.list_success_by_src(
                    fileitem.path,
                    storage=storage,
                    recursive=True,
                )
                matched_histories.extend(
                    transfer_history_oper.list_success_move_by_dest(
                        fileitem.path,
                        storage=storage,
                        recursive=True,
                    )
                )
            else:
                history = self._get_manual_transfer_history(
                    fileitem=fileitem,
                    transfer_history_oper=transfer_history_oper,
                    include_move_dest=True,
                )
                matched_histories = [history] if history and history.status else []

            for history in matched_histories:
                histories[history.id] = history
        return list(histories.values())

    @staticmethod
    def _delete_manual_transfer_history(
            history: TransferHistory,
            transfer_history_oper: TransferHistoryOper,
    ) -> Tuple[bool, str]:
        """删除手动重整历史；非成功移动记录同时清理可能存在的旧目标。"""
        if (
                history.dest_fileitem
                and not ManualHistoryMixin._is_successful_move_history(history)
        ):
            dest_fileitem = FileItem(**history.dest_fileitem)
            storage_chain = StorageChain()
            if (
                    storage_chain.exists(dest_fileitem)
                    and not storage_chain.delete_media_file(dest_fileitem)
            ):
                return False, f"{dest_fileitem.path} 删除失败"
        transfer_history_oper.delete(history.id)
        # 删除记录是用户显式要求重来，失败计数一并清零，否则重整仍会受上一轮次数限制
        clear_transfer_failures(history.src, history.src_storage)
        return True, ""


class FailedRetryMixin:
    @staticmethod
    def build_failed_transfer_buttons(
            history_id: Optional[int],
    ) -> Optional[List[List[dict]]]:
        """
        构建整理失败通知的操作按钮。
        """
        if not history_id:
            return None
        return [
            [
                {"text": "重试", "callback_data": f"transfer_retry_{history_id}"},
                {
                    "text": "智能助手接管",
                    "callback_data": f"transfer_ai_retry_{history_id}",
                },
            ]
        ]

    def redo_transfer_history(self, history_id: int) -> Tuple[bool, str]:
        """
        按历史记录直接重新整理，自动重新识别媒体信息。
        """
        return self._re_transfer(logid=history_id)

    @staticmethod
    def parse_failed_transfer_callback(
            callback_data: str,
    ) -> Optional[tuple[str, int]]:
        """
        解析整理失败通知按钮回调。
        """
        for prefix, action in (
                ("transfer_retry_", "retry"),
                ("transfer_ai_retry_", "ai_retry"),
        ):
            if callback_data.startswith(prefix):
                history_id = callback_data.replace(prefix, "", 1)
                if history_id.isdigit():
                    return action, int(history_id)
        return None

    def handle_failed_transfer_callback(
            self,
            *,
            callback_data: str,
            channel: NotificationChannel,
            source: str,
            userid: Union[str, int],
            username: str,
    ) -> bool:
        """
        处理整理失败通知中的重试类按钮。
        """
        callback = self.parse_failed_transfer_callback(callback_data)
        if not callback:
            return False

        action, history_id = callback
        if action == "retry":
            self._retry_transfer_history(
                history_id=history_id,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            )
        else:
            self._take_over_transfer_history_by_ai(
                history_id=history_id,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            )
        return True

    def _retry_transfer_history(
            self,
            history_id: int,
            channel: NotificationChannel,
            source: str,
            userid: Union[str, int],
            username: str,
    ) -> None:
        """
        立即重新整理一条失败的整理记录。
        """
        self.post_message(
            Message(
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                title=f"开始重新整理记录 #{history_id} ...",
                save_history=False,
            )
        )

        state, errmsg = self.redo_transfer_history(history_id)
        if state:
            self.post_message(
                Message(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title=f"整理记录 #{history_id} 已重新整理",
                    link=settings.MP_DOMAIN("#/history"),
                    save_history=False,
                )
            )
            return

        self.post_message(
            Message(
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                title="重新整理失败",
                text=errmsg,
                link=settings.MP_DOMAIN("#/history"),
                save_history=False,
            )
        )

    def _take_over_transfer_history_by_ai(
            self,
            history_id: int,
            channel: NotificationChannel,
            source: str,
            userid: Union[str, int],
            username: str,
    ) -> None:
        """
        由智能助手接管一条失败的整理记录。
        """

        if not settings.AI_AGENT_ENABLE:
            self.post_message(
                Message(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title="MoviePilot智能助手未启用，请在系统设置中启用",
                    save_history=False,
                )
            )
            return

        history = TransferHistoryOper().get(history_id)
        if not history:
            self.post_message(
                Message(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title="重新整理失败",
                    text=f"整理记录 #{history_id} 不存在",
                    link=settings.MP_DOMAIN("#/history"),
                    save_history=False,
                )
            )
            return

        redo_prompt = build_manual_redo_prompt(history)

        self.post_message(
            Message(
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                title=f"已将整理记录 #{history_id} 交给智能助手处理",
                text="处理完成后会在这里回复结果。",
                link=settings.MP_DOMAIN("#/history"),
                save_history=False,
            )
        )

        async def _run_ai_takeover():
            final_output = ""

            def _capture_output(text_output: str):
                nonlocal final_output
                final_output = text_output or ""

            try:
                manager = get_running_agent_manager()
                if manager is None:
                    raise RuntimeError("智能助手服务未运行")
                await manager.run_background_prompt(
                    message=redo_prompt,
                    session_prefix=f"__agent_manual_redo_{history_id}",
                    output_callback=_capture_output,
                    reply_mode=ReplyMode.CAPTURE_ONLY,
                    allow_message_tools=False,
                )
                await self.async_post_message(
                    Message(
                        channel=channel,
                        source=source,
                        userid=userid,
                        username=username,
                        title="智能助手整理完成",
                        text=final_output.strip()
                             or f"整理记录 #{history_id} 已由智能助手处理完成。",
                        link=settings.MP_DOMAIN("#/history"),
                        save_history=False,
                    )
                )
            except Exception as e:
                await self.async_post_message(
                    Message(
                        channel=channel,
                        source=source,
                        userid=userid,
                        username=username,
                        title="智能助手整理失败",
                        text=str(e),
                        link=settings.MP_DOMAIN("#/history"),
                        save_history=False,
                    )
                )

        asyncio.run_coroutine_threadsafe(_run_ai_takeover(), global_vars.loop)

    def _re_transfer(
            self,
            logid: int,
            mtype: MediaType = None,
            media_source: Optional[MediaSource] = None,
            media_id: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """
        根据历史记录，重新识别整理，只支持简单条件
        :param logid: 历史记录ID
        :param mtype: 媒体类型
        :param media_source: 媒体数据源
        :param media_id: 数据源原生 ID，必须与 media_source 成对提供
        """
        # 查询历史记录
        history: TransferHistory = TransferHistoryOper().get(logid)
        if not history:
            logger.error(f"整理记录不存在，ID：{logid}")
            return False, "整理记录不存在"
        # 按源目录路径重新整理
        src_path = Path(history.src)
        if not src_path.exists():
            return False, f"源目录不存在：{src_path}"
        # 查询媒体信息
        explicit_identity = media_source is not None or media_id is not None
        if explicit_identity and (not media_source or not media_id):
            return False, "媒体重新识别需要同时提供 media_source 和 media_id"
        if mtype and media_source and media_id:
            mediainfo = MediaChain().recognize_media(
                mtype=mtype,
                media_source=media_source,
                media_id=media_id,
                music_type=(
                    getattr(history, "music_type", None)
                    if mtype == MediaType.MUSIC
                    else None
                ),
                episode_group=history.episode_group,
            )
            if mediainfo and not isinstance(mediainfo, MusicInfo):
                # 更新媒体图片
                self.obtain_images(mediainfo=mediainfo)
        elif history.media_source and history.media_id:
            try:
                history_type = mtype or MediaType(history.type)
            except ValueError:
                history_type = mtype
            mediainfo = MediaChain().recognize_media(
                mtype=history_type,
                media_source=history.media_source,
                media_id=history.media_id,
                music_type=(
                    getattr(history, "music_type", None)
                    if history_type == MediaType.MUSIC
                    else None
                ),
                episode_group=history.episode_group,
            )
            mtype = history_type
            if mediainfo and not isinstance(mediainfo, MusicInfo):
                self.obtain_images(mediainfo=mediainfo)
        elif mtype == MediaType.MUSIC or self._is_music_retry_source(history, src_path):
            # 音乐重新整理走音乐识别链，避免默认影视识别误入 TMDB
            mtype = MediaType.MUSIC
            mediainfo = self._recognize_music_retry_media(history, src_path)
        else:
            recognize_context = MediaChain().recognize_by_path(
                str(src_path),
                episode_group=history.episode_group,
                obtain_images=True,
            )
            mediainfo = recognize_context.media_info if recognize_context else None
        # 音乐专辑目录允许无预识别信息，由整理链按音频后缀逐文件解析识别
        if not mediainfo and not (mtype == MediaType.MUSIC and src_path.is_dir()):
            return False, (
                f"未识别到媒体信息，类型：{mtype.value if mtype else None}，"
                f"media_source：{media_source}，media_id：{media_id}"
            )
        # 重新执行整理
        if mediainfo:
            logger.info(f"{src_path.name} 识别为：{mediainfo.title_year}")

        # 删除旧的已整理文件
        if history.dest_fileitem:
            # 解析目标文件对象
            dest_fileitem = FileItem(**history.dest_fileitem)
            StorageChain().delete_file(dest_fileitem)

        # 强制整理
        if history.src_fileitem:
            state, errmsg = self.do_transfer(
                fileitem=FileItem(**history.src_fileitem),
                mediainfo=mediainfo,
                mtype=mtype,
                download_hash=history.download_hash,
                force=True,
                background=False,
                manual=True,
            )
            if not state:
                return False, errmsg

        return True, ""
