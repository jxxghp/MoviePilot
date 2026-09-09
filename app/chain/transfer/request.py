"""整理请求的候选路径解析与批次归属规划。"""

from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from app.application.formatting import FormatParser
from app.application.history import (
    DownloadHistoryQueryPort,
)
from app.application.transfer.workflow import TransferTask
from app.chain.media import MediaChain
from app.chain.storage import StorageChain
from app.domain.context import MediaInfo, MusicInfo
from app.domain.meta.metabase import MetaBase
from app.domain.metainfo import MetaInfoPath
from app.schemas.exception import OperationInterrupted
from app.schemas.transfer import TransferInfo
from app.schemas.types import (
    MUSIC_ENTITY_RECORDING,
    MediaSource,
    MediaType,
)
from app.schemas.workflow import FileItem


def preview_media_title(
    mediainfo: Optional[Union[MediaInfo, MusicInfo]],
) -> Optional[str]:
    """音乐预览以专辑为批次标题，影视继续使用原有标题。"""
    if isinstance(mediainfo, MusicInfo) and mediainfo.album:
        return (
            f"{mediainfo.album} ({mediainfo.year})"
            if mediainfo.year
            else mediainfo.album
        )
    return mediainfo.title_year if mediainfo else None


def _should_discard_batch_recording_identity(
        *,
        multi_track_music_batch: bool,
        manual: bool,
        media_source: Optional[MediaSource],
        media_id: Optional[str],
        mediainfo: Optional[MediaInfo | MusicInfo],
        history_music_type: Optional[str],
) -> bool:
    """判断自动整专是否误带了共享单曲身份。"""
    if not multi_track_music_batch or (manual and media_source and media_id):
        return False
    batch_music_type = getattr(mediainfo, "music_type", None)
    return (
        batch_music_type == MUSIC_ENTITY_RECORDING
        or (not batch_music_type and history_music_type == MUSIC_ENTITY_RECORDING)
    )


def _should_discard_batch_music_identity(
    *,
    manual: bool,
    multi_track_music_batch: bool,
    media_source: Optional[MediaSource],
    media_id: Optional[str],
    mediainfo: Optional[MediaInfo | MusicInfo],
    history_music_type: Optional[str],
) -> bool:
    """批次误带单曲身份或未显式指定媒体时重新识别整张专辑。"""
    if manual and multi_track_music_batch and not (media_source and media_id):
        return True
    return _should_discard_batch_recording_identity(
        multi_track_music_batch=multi_track_music_batch,
        manual=manual,
        media_source=media_source,
        media_id=media_id,
        mediainfo=mediainfo,
        history_music_type=history_music_type,
    )


class _TransferCandidatePlanner:
    """持有一次整理请求的只读候选规划上下文。"""

    def __init__(
            self,
            chain: Any,
            *,
            meta: Optional[MetaBase],
            season: Optional[int],
            formater: Optional[FormatParser],
            batch_mtype: Optional[MediaType],
            mediainfo: Optional[Union[MediaInfo, MusicInfo]],
            continue_callback: Optional[Callable[[], bool]],
            has_episode_format_template: bool,
            transfer_exclude_words: Optional[list],
            download_hash: Optional[str],
            sync_extra_files: bool,
            fileitem: FileItem,
    ) -> None:
        """冻结候选规划依赖，避免请求阶段再读取可变配置。"""
        self._chain = chain
        self._meta = meta
        self._season = season
        self._formaterHandler = formater
        self._batch_mtype = batch_mtype
        self._mediainfo = mediainfo
        self._continue_callback = continue_callback
        self._has_episode_format_template = has_episode_format_template
        self._transfer_exclude_words = transfer_exclude_words
        self._download_hash = download_hash
        self._sync_extra_files = sync_extra_files
        self._fileitem = fileitem

    def _build_file_meta(
            self,
            source_path: Path,
            custom_word_list: Optional[List[str]] = None,
    ) -> Optional[MetaBase]:
        """
        构建整理任务使用的文件元数据，并应用手动季集/自定义格式覆盖。
        """
        built_meta = deepcopy(self._meta) if self._meta else self._build_path_meta(
            source_path, custom_word_list=custom_word_list
        )
        if not built_meta:
            return None
        if not self._meta:
            # _build_path_meta 已经应用过手动季集/自定义格式覆盖；
            # 这里避免再次偏移集数，导致手动整理的集数偏移翻倍。
            return built_meta
        return self._apply_meta_overrides(built_meta, source_path)
    def _has_reliable_video_source(self) -> bool:
        """
        是否存在可靠的影视类型来源；存在时音频按附加音轨解析，
        避免影视场景的音频文件误入音乐识别。
        """
        if self._batch_mtype is not None:
            return self._batch_mtype != MediaType.MUSIC
        # 预载媒体信息为非音乐时，整批整理视为影视上下文
        return self._mediainfo is not None and not isinstance(self._mediainfo, MusicInfo)
    def _build_path_meta(
            self,
            source_path: Path,
            custom_word_list: Optional[List[str]] = None,
            force_video: Optional[bool] = False,
    ) -> Optional[MetaBase]:
        """
        从文件路径识别媒体信息，用于判断附加文件是否属于当前主视频。
        :param force_video: 强制按视频解析，附加文件归属匹配专用，避免音乐判定干扰归属比较
        """
        # 音频后缀且无可靠影视类型来源时按音乐解析，走 MusicBrainz 识别链
        if (
                not force_video
                and source_path.suffix.lower() in self._chain._audio_exts
                and not self._has_reliable_video_source()
        ):
            path_meta = MediaChain.read_path_meta(source_path)
        else:
            # 影视场景附加音轨（如评论音轨）强制按视频解析，保留季集归属
            path_meta = MetaInfoPath(
                source_path, custom_words=custom_word_list, force_video=True
            )
        if not path_meta:
            return None
        return self._apply_meta_overrides(path_meta, source_path)
    def _apply_meta_overrides(
            self,
            current_meta: MetaBase, source_path: Path
    ) -> Optional[MetaBase]:
        """
        应用手动传入的季集覆盖和自定义识别格式。
        """
        # 合并季
        if self._season is not None:
            current_meta.begin_season = self._season

        # 自定义识别
        if self._formaterHandler:
            # 开始集、结束集、PART
            begin_ep, end_ep, part = self._formaterHandler.split_episode(
                file_name=source_path.name, file_meta=current_meta
            )
            if begin_ep is not None:
                current_meta.begin_episode = begin_ep
            if part is not None:
                current_meta.part = part
            if end_ep is not None:
                current_meta.end_episode = end_ep

        return current_meta
    def _is_allowed_transfer_item(self, item: FileItem, _is_bluray_dir: bool) -> bool:
        """筛选单文件模式额外读取的字幕/音频，保持模板和屏蔽词语义。"""
        if self._continue_callback and not self._continue_callback():
            raise OperationInterrupted()
        if self._has_episode_format_template and self._formaterHandler and not self._formaterHandler.match(item.name):
            return False
        if any(
            marker in item.path
            for marker in ("/@Recycle/", "/#recycle/", "/.", "/@eaDir")
        ):
            return False
        return not self._chain._is_blocked_by_exclude_words(item.path, self._transfer_exclude_words)
    def _build_main_meta(
            self,
            main_fileitem: FileItem,
            main_bluray_dir: bool,
            download_history_repository: DownloadHistoryQueryPort,
    ) -> Optional[MetaBase]:
        """
        构建主视频元数据。
        """
        main_path = Path(main_fileitem.path)
        main_download_history = self._chain._resolve_download_history(
            repository=download_history_repository,
            file_path=main_path,
            bluray_dir=main_bluray_dir,
            download_hash=self._download_hash,
        )
        return self._build_file_meta(
            main_path,
            custom_word_list=self._chain._get_subscribe_custom_words(main_download_history),
        )
    def _append_item(
            self,
            planned_items: List[Tuple[FileItem, bool]],
            seen_file_keys: set[Tuple[str, str]],
            item: FileItem,
            is_bluray_dir: bool,
    ) -> bool:
        """
        添加待整理文件项并去重。
        """
        file_key = self._chain._get_file_key(item)
        if file_key in seen_file_keys:
            return False
        planned_items.append((item, is_bluray_dir))
        seen_file_keys.add(file_key)
        return True
    def _build_directory_index(
            self,
            items: List[Tuple[FileItem, bool]]
    ) -> Tuple[
        Dict[Tuple[str, str], List[FileItem]],
        Dict[Tuple[str, str], List[Tuple[FileItem, bool]]],
    ]:
        """
        基于已遍历结果构建同目录主视频和附加文件索引。
        """
        main_items_by_dir: Dict[Tuple[str, str], List[FileItem]] = {}
        extra_items_by_dir: Dict[Tuple[str, str], List[Tuple[FileItem, bool]]] = {}
        for item, is_bluray_dir in items:
            if not item or item.type != "file":
                continue
            dir_key = self._chain._get_file_parent_key(item)
            if not is_bluray_dir and self._chain._is_media_file(item, self._batch_mtype):
                main_items_by_dir.setdefault(dir_key, []).append(item)
            elif (
                    self._chain._is_subtitle_file(item)
                    or self._chain._is_audio_file(item)
                    or self._chain._is_music_lyrics_file(item)
            ):
                extra_items_by_dir.setdefault(dir_key, []).append((item, is_bluray_dir))
        return main_items_by_dir, extra_items_by_dir
    def _get_single_file_sibling_items(
            self,
            current_fileitem: FileItem,
    ) -> Tuple[List[FileItem], List[Tuple[FileItem, bool]]]:
        """
        单文件整理时只额外读取一次父目录，收集同目录主视频和附加文件。
        """
        storagechain = StorageChain()
        if not hasattr(storagechain, "get_parent_item") or not hasattr(
                storagechain, "list_files"
        ):
            return [], []
        parent_item = storagechain.get_parent_item(current_fileitem)
        if not parent_item:
            return [], []
        main_fileitems: List[FileItem] = []
        extra_items: List[Tuple[FileItem, bool]] = []
        for item in storagechain.list_files(parent_item, recursion=False) or []:
            if not item or item.type != "file":
                continue
            if self._chain._is_media_file(item, self._batch_mtype):
                main_fileitems.append(item)
                continue
            if not (
                    self._chain._is_subtitle_file(item)
                    or self._chain._is_audio_file(item)
                    or self._chain._is_music_lyrics_file(item)
            ):
                continue
            if not self._is_allowed_transfer_item(item, False):
                continue
            extra_items.append((item, False))
        return main_fileitems, extra_items
    def _plan_file_items(
            self,
            items: List[Tuple[FileItem, bool]]
    ) -> Tuple[List[Tuple[FileItem, bool]], Dict[Tuple[str, str], MetaBase]]:
        """
        生成最终整理顺序：主视频优先，同名附加文件跟随，剩余附加文件最后处理。
        """
        if not items:
            return [], {}

        download_history_repository = self._chain.download_history_repository
        inherited_map: Dict[Tuple[str, str], MetaBase] = {}
        main_items_by_dir, extra_items_by_dir = self._build_directory_index(items)
        main_items = [
            (item, is_bluray_dir)
            for item, is_bluray_dir in items
            if item
               and (
                       is_bluray_dir
                       or (
                               item.type == "file"
                               and self._chain._is_media_file(item, self._batch_mtype)
                       )
               )
        ]

        single_file_mode = len(items) == 1 and self._fileitem.type == "file"
        if single_file_mode:
            current_item, current_bluray_dir = items[0]
            if current_item.type == "file":
                sibling_main_items, sibling_extra_items = self._get_single_file_sibling_items(
                    current_item
                )
                current_dir_key = self._chain._get_file_parent_key(current_item)
                if not current_bluray_dir and self._chain._is_media_file(
                        current_item, self._batch_mtype
                ):
                    main_items = [(current_item, current_bluray_dir)]
                    main_items_by_dir[current_dir_key] = [current_item]
                    extra_items_by_dir[current_dir_key] = sibling_extra_items
                elif (
                        self._chain._is_subtitle_file(current_item)
                        or self._chain._is_audio_file(current_item)
                        or self._chain._is_music_lyrics_file(current_item)
                ):
                    related_main_file_key = self._chain._get_related_main_file_key(
                        extra_fileitem=current_item,
                        main_fileitems=sibling_main_items,
                    )
                    related_main_fileitem = next(
                        (
                            main_item
                            for main_item in sibling_main_items
                            if self._chain._get_file_key(main_item) == related_main_file_key
                        ),
                        None,
                    )
                    if related_main_fileitem:
                        main_meta = self._build_main_meta(
                            related_main_fileitem,
                            False,
                            download_history_repository,
                        )
                        if main_meta:
                            inherited_map[self._chain._get_file_key(current_item)] = deepcopy(main_meta)
                    return list(items), inherited_map

        if not main_items:
            remaining = [
                item
                for item in items
                if not (
                        self._batch_mtype == MediaType.MUSIC
                        and self._chain._is_music_lyrics_file(item[0])
                )
            ]
            return remaining, inherited_map

        planned_items: List[Tuple[FileItem, bool]] = []
        seen_file_keys: set[Tuple[str, str]] = set()
        extra_meta_cache: Dict[Tuple[str, Tuple[str, ...]], Optional[MetaBase]] = {}

        def _get_cached_extra_meta(
                extra_path: Path,
                custom_word_list: Optional[List[str]],
        ) -> Optional[MetaBase]:
            """
            同一组识别词下的附加文件只解析一次。
            """
            custom_words_key = tuple(custom_word_list or [])
            cache_key = (extra_path.as_posix(), custom_words_key)
            if cache_key not in extra_meta_cache:
                # 归属匹配专用视频解析：此处目的是判断附加文件是否跟随主视频，
                # 若按音乐解析会导致影视目录内的音频无法与主视频比较归属
                extra_meta_cache[cache_key] = self._build_path_meta(
                    extra_path,
                    custom_word_list=list(custom_words_key) or None,
                    force_video=True,
                )
            return extra_meta_cache[cache_key]

        for main_item, main_bluray_dir in main_items:
            self._append_item(planned_items, seen_file_keys, main_item, main_bluray_dir)
            if main_bluray_dir or not self._chain._is_media_file(
                    main_item, self._batch_mtype
            ):
                continue

            main_path = Path(main_item.path)
            main_download_history = self._chain._resolve_download_history(
                repository=download_history_repository,
                file_path=main_path,
                bluray_dir=main_bluray_dir,
                download_hash=self._download_hash,
            )
            subscribe_custom_words = self._chain._get_subscribe_custom_words(
                main_download_history
            )
            main_meta = self._build_file_meta(
                main_path,
                custom_word_list=subscribe_custom_words,
            )
            if not main_meta:
                continue

            dir_key = self._chain._get_file_parent_key(main_item)
            main_fileitems = main_items_by_dir.get(dir_key) or [main_item]
            main_file_key = self._chain._get_file_key(main_item)
            for extra_item, extra_bluray_dir in extra_items_by_dir.get(dir_key, []):
                if self._chain._get_file_key(extra_item) in seen_file_keys:
                    continue
                related_main_file_key = self._chain._get_related_main_file_key(
                    extra_fileitem=extra_item,
                    main_fileitems=main_fileitems,
                )
                if related_main_file_key:
                    if related_main_file_key == main_file_key:
                        if self._append_item(
                                planned_items,
                                seen_file_keys,
                                extra_item,
                                extra_bluray_dir,
                        ):
                            inherited_map[self._chain._get_file_key(extra_item)] = deepcopy(main_meta)
                    continue

                if single_file_mode or not self._sync_extra_files:
                    continue

                extra_meta = _get_cached_extra_meta(
                    Path(extra_item.path),
                    subscribe_custom_words,
                )
                if not self._chain._is_same_media_meta(main_meta, extra_meta):
                    continue
                if self._append_item(
                        planned_items,
                        seen_file_keys,
                        extra_item,
                        extra_bluray_dir,
                ):
                    inherited_map[self._chain._get_file_key(extra_item)] = deepcopy(extra_meta)

        for item, is_bluray_dir in items:
            if (
                    self._batch_mtype == MediaType.MUSIC
                    and self._chain._is_music_lyrics_file(item)
                    and self._chain._get_file_key(item) not in inherited_map
            ):
                continue
            self._append_item(planned_items, seen_file_keys, item, is_bluray_dir)

        return planned_items, inherited_map


def build_transfer_preview_item(task: TransferTask, transferinfo: TransferInfo) -> dict[str, Any]:
    """按实际整理结果投影预览项，失败阶段与恢复动作保留原始裁决。"""
    from app.application.transfer.feedback import classify_transfer_failure

    item_meta = task.meta
    item_media = task.mediainfo
    feedback = classify_transfer_failure(
        transferinfo.message,
        overwrite_skipped=bool(transferinfo.overwrite_skipped),
    )
    return (
        {
            "source": task.fileitem.path,
            "target": transferinfo.target_item.path if transferinfo.target_item else None,
            "target_dir": transferinfo.target_diritem.path if transferinfo.target_diritem else None,
            "success": transferinfo.success,
            "message": transferinfo.message,
            "failure_stage": (
                transferinfo.failure_stage or feedback.stage.value
                if not transferinfo.success
                else None
            ),
            "recovery_action": (
                transferinfo.recovery_action or feedback.action
                if not transferinfo.success
                else None
            ),
            "overwrite_skipped": bool(transferinfo.overwrite_skipped),
            "type": item_media.type.value if item_media and item_media.type else None,
            "title": preview_media_title(item_media),
            "season": item_meta.begin_season if item_meta else None,
            "episode": item_meta.begin_episode if item_meta else None,
            "episode_end": item_meta.end_episode if item_meta else None,
            "part": item_meta.part if item_meta else None,
            "org_string": item_meta.org_string if item_meta else None,
            "apply_words": item_meta.apply_words if item_meta else [],
            "resource_team": item_meta.resource_team if item_meta else None,
            "customization": item_meta.customization if item_meta else None,
        }
    )
