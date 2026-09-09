"""整理请求候选构建与任务工作流编排。"""

import re
import traceback
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from app.application.classification.reference import (
    category_path_below_media_type,
)
from app.application.configuration import get_configured_system_config
from app.application.directory import DirectoryHelper
from app.application.formatting import FormatParser
from app.application.history import (
    describe_history_gate,
    evaluate_history_gate,
    is_skip_action,
)
from app.application.transfer.history import history_file_fingerprint
from app.application.transfer.workflow import (
    TransferAdmission,
    TransferAdmissionConflictError,
    TransferTask,
)
from app.chain.media import MediaChain
from app.chain.storage import StorageChain
from app.chain.transfer.contract import _TransferOwnerBase
from app.domain.context import MediaInfo, MusicAlbumInfo, MusicInfo, TorrentInfo
from app.domain.meta.metabase import MetaBase
from app.domain.meta.metamusic import MetaMusic
from app.runtime.log import logger
from app.runtime.progress import ProgressHelper
from app.runtime.stop import runtime_stop_state
from app.schemas.exception import OperationInterrupted
from app.schemas.media import resolve_media_identity
from app.schemas.system import TransferDirectoryConf
from app.schemas.transfer import EpisodeFormat, TransferInfo
from app.schemas.types import (
    MediaSource,
    MediaType,
    ProgressKey,
    SystemConfigKey,
)
from app.schemas.workflow import FileItem

from .request import (
    _should_discard_batch_music_identity,
    _TransferCandidatePlanner,
    _TransferSubmissionCollector,
    build_transfer_preview_item,
)


def _normalize_transfer_identity(
    mediainfo: Optional[Union[MediaInfo, MusicInfo]],
    mtype: Optional[MediaType],
    media_source: Optional[MediaSource],
    media_id: Optional[str],
    meta: Optional[MetaBase],
) -> tuple[
    Optional[Union[MediaInfo, MusicInfo]],
    Optional[MediaSource],
    Optional[str],
    Optional[str],
]:
    """规范整理请求的媒体身份，并在显式身份缺失时短路。"""
    explicit_identity = media_source is not None or media_id is not None
    normalized_source, normalized_media_id = resolve_media_identity(
        media_source=media_source,
        media_id=media_id,
    )
    if explicit_identity and (not normalized_source or not normalized_media_id):
        return (
            mediainfo,
            normalized_source,
            normalized_media_id,
            "整理任务需要同时提供有效的 media_source 和 media_id",
        )
    if not explicit_identity and mediainfo:
        normalized_source, normalized_media_id = resolve_media_identity(media=mediainfo)
    if explicit_identity and not mediainfo:
        mediainfo = MediaChain().recognize_media(
            mtype=mtype,
            media_source=normalized_source,
            media_id=normalized_media_id,
            music_type=getattr(meta, "music_type", None),
        )
        if not mediainfo:
            return (
                mediainfo,
                normalized_source,
                normalized_media_id,
                "未识别到媒体信息，请检查媒体来源和媒体 ID 后重试",
            )
    return mediainfo, normalized_source, normalized_media_id, None


class TransferWorkflowOwner(_TransferOwnerBase):
    """协调请求级候选构建并委托规划、执行与结算 owner。"""

    @staticmethod
    def _build_transfer_fileitem(torrent: TorrentInfo) -> FileItem:
        """把下载器任务路径转换为整理链使用的本地文件项。"""
        file_path = torrent.path
        return FileItem(
            storage="local",
            path=file_path.as_posix() + ("/" if file_path.is_dir() else ""),
            type="dir" if not file_path.is_file() else "file",
            name=file_path.name,
            size=file_path.stat().st_size,
            extension=file_path.suffix.lstrip("."),
        )

    def _TransferChain__get_trans_fileitems(
        self,
        fileitem: FileItem,
        predicate: Optional[Callable[[FileItem, bool], bool]],
        verify_file_exists: bool = True,
    ) -> List[Tuple[FileItem, bool]]:
        """
        获取待整理文件项列表

        :param fileitem: 源文件项
        :param predicate: 用于筛选目录或文件项
            该函数接收两个参数：

            - `file_item`: 需要判断的文件项（类型为 `FileItem`）
            - `is_bluray_dir`: 表示该项是否为蓝光原盘目录（布尔值）

            函数应返回 `True` 表示保留该项，`False` 表示过滤掉

            若 `predicate` 为 `None`，则默认保留所有项
        :param verify_file_exists: 验证目录或文件是否存在，默认值为 `True`
        """
        if runtime_stop_state.is_system_stopped:
            raise OperationInterrupted()

        storagechain = StorageChain()

        def __is_bluray_sub(_path: str) -> bool:
            """
            判断是否蓝光原盘目录内的子目录或文件
            """
            return True if re.search(r"BDMV[/\\]STREAM", _path, re.IGNORECASE) else False

        def __get_bluray_dir(_storage: str, _path: Path) -> Optional[FileItem]:
            """
            获取蓝光原盘BDMV目录的上级目录
            """
            for p in _path.parents:
                if p.name == "BDMV":
                    return storagechain.get_file_item(storage=_storage, path=p.parent)
            return None

        def _apply_predicate(file_item: FileItem, is_bluray_dir: bool) -> List[Tuple[FileItem, bool]]:
            """仅保留满足调用方筛选条件的普通文件或蓝光目录。"""
            if predicate is None or predicate(file_item, is_bluray_dir):
                return [(file_item, is_bluray_dir)]
            return []

        if verify_file_exists:
            latest_fileitem = storagechain.get_item(fileitem)
            if not latest_fileitem:
                logger.warn(f"目录或文件不存在：{fileitem.path}")
                return []
            # 确保从历史记录重新整理时 能获得最新的源文件大小、修改日期等
            fileitem = latest_fileitem

        # 是否蓝光原盘子目录或文件
        if __is_bluray_sub(fileitem.path):
            if bluray_dir := __get_bluray_dir(fileitem.storage, Path(fileitem.path)):
                # 返回该文件所在的原盘根目录
                return _apply_predicate(bluray_dir, True)

        # 单文件
        if fileitem.type == "file":
            return _apply_predicate(fileitem, False)

        # 是否蓝光原盘根目录
        sub_items = storagechain.list_files(fileitem, recursion=False) or []
        if storagechain.contains_bluray_subdirectories(sub_items):
            # 当前目录是原盘根目录，不需要递归
            return _apply_predicate(fileitem, True)

        # 不是原盘根目录 递归获取目录内需要整理的文件项列表
        return [
            item
            for sub_item in sub_items
            for item in (
                self._TransferChain__get_trans_fileitems(sub_item, predicate, verify_file_exists=False)
                if sub_item.type == "dir"
                else _apply_predicate(sub_item, False)
            )
        ]

    @staticmethod
    def _get_shared_download_roots(file_path: Path) -> set[str]:
        """
        获取当前文件所在的共享下载根目录边界。

        父目录兜底回查只应在种子自身目录内进行，不能越过共享下载根目录，
        否则历史中的单文件/无子目录任务会污染同级其它文件的识别结果。
        """
        shared_roots: set[str] = set()
        media_type_dirs = {mtype.value for mtype in MediaType}
        directory_helper = DirectoryHelper()

        for dir_info in DirectoryHelper().get_download_dirs():
            if not dir_info.download_path:
                continue

            download_root = Path(dir_info.download_path)
            if not file_path.is_relative_to(download_root):
                continue

            shared_roots.add(download_root.as_posix())
            relative_parts = file_path.relative_to(download_root).parts
            current_root = download_root
            part_index = 0
            media_type = dir_info.media_type
            type_folder_applied = False

            if (
                not dir_info.media_type
                and dir_info.download_type_folder
                and len(relative_parts) > part_index
                and relative_parts[part_index] in media_type_dirs
            ):
                current_root = current_root / relative_parts[part_index]
                shared_roots.add(current_root.as_posix())
                media_type = relative_parts[part_index]
                part_index += 1
                type_folder_applied = True

            if (
                not directory_helper.has_fixed_category(dir_info)
                and dir_info.download_category_folder
                and len(relative_parts) > part_index
            ):
                category_paths = directory_helper.classification_category_paths(media_type)
                category_paths = tuple(
                    relative_path
                    for category_path in category_paths
                    if (
                        relative_path := category_path_below_media_type(
                            category_path,
                            media_type,
                            type_folder_enabled=type_folder_applied,
                        )
                    )
                )
                category_matched = False
                sorted_category_paths = sorted(
                    category_paths,
                    key=len,
                    reverse=True,
                )
                for category_parts in sorted_category_paths:
                    relative_category_parts = tuple(relative_parts[part_index : part_index + len(category_parts)])
                    if relative_category_parts != category_parts:
                        continue
                    category_matched = True
                    category_root = current_root
                    for category_part in category_parts:
                        category_root = category_root / category_part
                        shared_roots.add(category_root.as_posix())
                    break
                if not category_matched:
                    category_root = current_root / relative_parts[part_index]
                    shared_roots.add(category_root.as_posix())

        return shared_roots

    def _collect_transfer_candidates(
        self,
        fileitem: FileItem,
        batch_mtype: Optional[MediaType],
        min_filesize: int,
        epformat: Optional[EpisodeFormat],
        season: Optional[int],
        continue_callback: Optional[Callable],
        selected_fileitems: Optional[list[FileItem]] = None,
    ) -> Tuple[List[Tuple[FileItem, bool]], bool]:
        """
        收集并过滤本次整理的候选文件。

        候选遍历只负责发现文件，业务过滤集中在此阶段；返回模板命中状态供公开整理流程
        保持“未命中自定义集数模板时跳过”的旧行为。
        """
        format_handler = (
            FormatParser(
                eformat=epformat.format,
                details=epformat.detail,
                part=epformat.part,
                offset=epformat.offset,
            )
            if epformat
            else None
        )
        has_template = bool(epformat and epformat.format)
        exclude_words = get_configured_system_config().get(SystemConfigKey.TransferExcludeWords)
        matched_template = False

        def keep_candidate(item: FileItem, _is_bluray_dir: bool) -> bool:
            """候选遍历阶段只响应取消请求，不提前应用业务过滤。"""
            if continue_callback and not continue_callback():
                raise OperationInterrupted()
            return True

        def is_allowed(item: FileItem, is_bluray_dir: bool) -> bool:
            """判断候选文件是否符合格式、后缀、大小和屏蔽词约束。"""
            nonlocal matched_template
            if continue_callback and not continue_callback():
                raise OperationInterrupted()
            if has_template and format_handler:
                if not format_handler.match(item.name):
                    return False
                matched_template = True
            if batch_mtype == MediaType.MUSIC:
                if self._is_music_lyrics_file(item):
                    return not self._is_blocked_by_exclude_words(item.path, exclude_words)
                if not self._is_media_file(item, batch_mtype):
                    return False
                if not self._is_allow_filesize(item, min_filesize):
                    return False
            elif not is_bluray_dir and not self._is_subtitle_file(item) and not self._is_audio_file(item):
                if not self._is_media_file(item, batch_mtype):
                    return False
                if not self._is_allow_filesize(item, min_filesize):
                    return False
            if any(marker in item.path for marker in ("/@Recycle/", "/#recycle/", "/.", "/@eaDir")):
                logger.debug(f"{item.path} 是回收站或隐藏的文件")
                return False
            return not self._is_blocked_by_exclude_words(item.path, exclude_words)

        if selected_fileitems is None:
            candidates = self._TransferChain__get_trans_fileitems(fileitem, predicate=keep_candidate)
        else:
            storage_chain = StorageChain()
            candidates = [
                (latest_fileitem, False)
                for selected_fileitem in selected_fileitems
                if (latest_fileitem := storage_chain.get_item(selected_fileitem))
            ]
        return [
            (item, is_bluray_dir) for item, is_bluray_dir in candidates if is_allowed(item, is_bluray_dir)
        ], matched_template

    def do_transfer(
        self,
        fileitem: FileItem,
        meta: MetaBase = None,
        mediainfo: Optional[Union[MediaInfo, MusicInfo, MusicAlbumInfo]] = None,
        mtype: Optional[MediaType] = None,
        media_source: Optional[MediaSource] = None,
        media_id: Optional[str] = None,
        target_directory: TransferDirectoryConf = None,
        target_storage: Optional[str] = None,
        target_path: Path = None,
        transfer_type: Optional[str] = None,
        scrape: Optional[bool] = None,
        library_type_folder: Optional[bool] = None,
        library_category_folder: Optional[bool] = None,
        season: Optional[int] = None,
        epformat: EpisodeFormat = None,
        min_filesize: Optional[int] = 0,
        downloader: Optional[str] = None,
        download_hash: Optional[str] = None,
        force: Optional[bool] = False,
        background: Optional[bool] = True,
        manual: Optional[bool] = False,
        preview: Optional[bool] = False,
        sync_extra_files: Optional[bool] = False,
        cleanup_dest_fileitem: Optional[FileItem] = None,
        continue_callback: Callable = None,
        reorganize: Optional[bool] = False,
        music_release_regions: Optional[list[str]] = None,
        music_release_scripts: Optional[list[str]] = None,
    ) -> Tuple[bool, Union[str, dict]]:
        """
        兼容公开整理入口，委托给内部批次执行阶段。

        公开签名是 API、工作流、监控器和插件共同使用的稳定契约；具体整理阶段保留在
        内部方法中，后续可以独立拆分规划、执行和结算，而不迫使调用方迁移参数。
        """
        return self._run_transfer_workflow(
            fileitem=fileitem,
            meta=meta,
            mediainfo=mediainfo,
            mtype=mtype,
            media_source=media_source,
            media_id=media_id,
            target_directory=target_directory,
            target_storage=target_storage,
            target_path=target_path,
            transfer_type=transfer_type,
            scrape=scrape,
            library_type_folder=library_type_folder,
            library_category_folder=library_category_folder,
            season=season,
            epformat=epformat,
            min_filesize=min_filesize,
            downloader=downloader,
            download_hash=download_hash,
            force=force,
            background=background,
            manual=manual,
            preview=preview,
            sync_extra_files=sync_extra_files,
            cleanup_dest_fileitem=cleanup_dest_fileitem,
            continue_callback=continue_callback,
            reorganize=reorganize,
            music_release_regions=music_release_regions,
            music_release_scripts=music_release_scripts,
        )

    def _execute_transfer(self, *args: Any, **kwargs: Any) -> Tuple[bool, Union[str, dict]]:
        """兼容旧内部钩子，统一委托请求工作流 owner。"""
        return self._run_transfer_workflow(*args, **kwargs)

    def _run_transfer_workflow(
        self,
        fileitem: FileItem,
        meta: MetaBase = None,
        mediainfo: Optional[Union[MediaInfo, MusicInfo, MusicAlbumInfo]] = None,
        mtype: Optional[MediaType] = None,
        media_source: Optional[MediaSource] = None,
        media_id: Optional[str] = None,
        target_directory: TransferDirectoryConf = None,
        target_storage: Optional[str] = None,
        target_path: Path = None,
        transfer_type: Optional[str] = None,
        scrape: Optional[bool] = None,
        library_type_folder: Optional[bool] = None,
        library_category_folder: Optional[bool] = None,
        season: Optional[int] = None,
        epformat: EpisodeFormat = None,
        min_filesize: Optional[int] = 0,
        downloader: Optional[str] = None,
        download_hash: Optional[str] = None,
        force: Optional[bool] = False,
        background: Optional[bool] = True,
        manual: Optional[bool] = False,
        preview: Optional[bool] = False,
        sync_extra_files: Optional[bool] = False,
        cleanup_dest_fileitem: Optional[FileItem] = None,
        continue_callback: Callable = None,
        reorganize: Optional[bool] = False,
        recovery_admission: Optional[TransferAdmission] = None,
        music_release_regions: Optional[list[str]] = None,
        music_release_scripts: Optional[list[str]] = None,
        selected_fileitems: Optional[list[FileItem]] = None,
        report_results: bool = False,
        skip_success: bool = False,
    ) -> Tuple[bool, Union[str, dict]]:
        """
        执行一个复杂目录的整理操作
        :param fileitem: 文件项
        :param meta: 元数据
        :param mediainfo: 媒体信息
        :param mtype: 未提供媒体信息时使用的媒体类型提示
        :param media_source: 请求级识别与刮削数据源
        :param media_id: 数据源原生 ID；显式指定身份时与 media_source 成对传入
        :param target_directory:  目标目录配置
        :param target_storage: 目标存储器
        :param target_path: 目标路径
        :param transfer_type: 整理类型
        :param scrape: 是否刮削元数据
        :param library_type_folder: 媒体库类型子目录
        :param library_category_folder: 媒体库类别子目录
        :param season: 季
        :param epformat: 剧集格式
        :param min_filesize: 最小文件大小(MB)
        :param downloader: 下载器
        :param download_hash: 下载记录hash
        :param force: 是否强制整理
        :param background: 是否后台运行
        :param manual: 是否手动整理
        :param preview: 是否仅预览
        :param reorganize: 是否清理已有成功记录后重新整理
        :param sync_extra_files: 是否在整理主视频文件时同步整理同媒体附加文件
        :param cleanup_dest_fileitem: 确认存在待整理任务后需要清理的旧目标文件
        :param continue_callback: 继续处理回调
        :param recovery_admission: 内部恢复调用绑定的既有 durable 记录
        :param music_release_regions: 本次音乐整理的发行地区优先级
        :param music_release_scripts: 本次音乐整理的文字字形优先级
        :param selected_fileitems: 前端显式选中的文件，按同一批次规划
        :param report_results: 返回实际阶段回执，后台接收不表示入库完成
        :param skip_success: 在预览、历史清理及任务准入前跳过成功记录
        返回：成功标识，错误信息
        """
        selected_music_album: Optional[MusicAlbumInfo]
        if isinstance(mediainfo, MusicAlbumInfo):
            selected_music_album = mediainfo
            mediainfo = mediainfo.to_music_info()
        else:
            selected_music_album = None
        mediainfo, media_source, media_id, identity_error = _normalize_transfer_identity(
            mediainfo=mediainfo,
            mtype=mtype,
            media_source=media_source,
            media_id=media_id,
            meta=meta,
        )
        if identity_error:
            return False, identity_error

        all_success = True
        submission = _TransferSubmissionCollector(report_results, fileitem)
        transfer_batch_id = str(uuid.uuid4())
        batch_mtype = getattr(mediainfo, "type", None)
        if batch_mtype in (None, MediaType.UNKNOWN):
            batch_mtype = mtype
        if preview:
            # 预览模式始终同步执行，避免进入异步队列
            background = False
        # 自定义格式
        has_episode_format_template = bool(epformat and epformat.format)
        formaterHandler = (
            FormatParser(
                eformat=epformat.format,
                details=epformat.detail,
                part=epformat.part,
                offset=epformat.offset,
            )
            if epformat
            else None
        )

        # 汇总错误信息
        err_msgs: List[str] = []
        transfer_exclude_words = get_configured_system_config().get(SystemConfigKey.TransferExcludeWords)

        candidate_planner = _TransferCandidatePlanner(
            self,
            meta=meta,
            season=season,
            formater=formaterHandler,
            batch_mtype=batch_mtype,
            mediainfo=mediainfo,
            continue_callback=continue_callback,
            has_episode_format_template=has_episode_format_template,
            transfer_exclude_words=transfer_exclude_words,
            download_hash=download_hash,
            sync_extra_files=bool(sync_extra_files),
            fileitem=fileitem,
        )

        try:
            file_items, matched_episode_format_template = self._collect_transfer_candidates(
                fileitem=fileitem,
                batch_mtype=batch_mtype,
                min_filesize=min_filesize or 0,
                epformat=epformat,
                season=season,
                continue_callback=continue_callback,
                selected_fileitems=selected_fileitems,
            )
        except OperationInterrupted:
            return submission.result(False, [f"{fileitem.name} 已取消"], preview=False, preview_items=[])

        if not file_items:
            if has_episode_format_template and not matched_episode_format_template:
                logger.info(f"{fileitem.path} 未匹配到集数定位模板，跳过整理")
                submission.record(fileitem, "skipped", "未匹配到集数定位模板，跳过整理")
                return submission.result(True, [], preview=bool(preview), preview_items=[])
            logger.warn(f"{fileitem.path} 没有找到可整理的媒体文件")
            return False, f"{fileitem.name} 没有找到可整理的媒体文件"

        file_items = self._filter_manual_transfer_history(file_items, bool(manual and skip_success), submission.record_history_skip)
        if not file_items:
            return submission.result(True, [], preview=bool(preview), preview_items=[])
        file_items, inherited_meta_map = candidate_planner._plan_file_items(file_items)

        selected_music_track_map, selected_music_error = self._selected_music_track_map(
            file_items, selected_music_album
        )
        if selected_music_error:
            return False, selected_music_error

        planned_file_count = len(file_items)

        if preview:
            logger.info(f"正在预览 {planned_file_count} 个文件的整理路径...")
        else:
            logger.info(f"正在计划整理 {planned_file_count} 个文件...")

        submission.expect(file_items)
        try:
            (
                transfer_tasks,
                all_success,
                err_msgs,
                skipped_history_count,
                skipped_torrents,
            ) = self._build_transfer_tasks(
                file_items=file_items,
                inherited_meta_map=inherited_meta_map,
                build_file_meta=candidate_planner._build_file_meta,
                meta=meta,
                mediainfo=mediainfo,
                media_source=media_source,
                media_id=media_id,
                batch_mtype=batch_mtype,
                target_directory=target_directory,
                target_storage=target_storage,
                target_path=target_path,
                transfer_type=transfer_type,
                scrape=scrape,
                library_type_folder=library_type_folder,
                library_category_folder=library_category_folder,
                downloader=downloader,
                download_hash=download_hash,
                transfer_batch_id=transfer_batch_id,
                manual=bool(manual),
                background=bool(background),
                preview=bool(preview),
                reorganize=bool(reorganize),
                force=bool(force),
                continue_callback=continue_callback,
                cleanup_dest_fileitem=cleanup_dest_fileitem,
                recovery_admission=recovery_admission,
                music_release_regions=music_release_regions,
                music_release_scripts=music_release_scripts,
                selected_music_track_map=selected_music_track_map,
                submission=submission,
            )
        except OperationInterrupted:
            return submission.result(False, [f"{fileitem.name} 已取消"], preview=False, preview_items=[])

        all_success, err_msgs, preview_items = self._execute_transfer_tasks(
            transfer_tasks=transfer_tasks,
            preview=bool(preview),
            continue_callback=continue_callback,
            all_success=all_success,
            err_msgs=err_msgs,
            submission=submission,
        )

        # 下载器任务在这一轮可能因为历史记录全部命中而没有进入整理队列，
        # 这里补打一遍已整理标签，避免同一种子被重复扫描。
        if skipped_history_count == planned_file_count and skipped_torrents:
            for skipped_hash, skipped_downloader in skipped_torrents:
                logger.info(f"补充设置下载任务已整理标签：{skipped_hash}")
                self._TransferChain__mark_torrent_completed_if_done(skipped_hash, skipped_downloader)

        return submission.result(all_success, err_msgs, preview=bool(preview), preview_items=preview_items)

    def _enqueue_transfer_candidate(
        self, task: TransferTask, errors: list[str], submission: _TransferSubmissionCollector,
    ) -> Optional[bool]:
        """在唯一队列入口捕获准入失败，给同批其余文件保留执行机会和错误回执。"""
        try:
            return bool(self.put_to_queue(task=task))
        except Exception as error:
            logger.error(f"{task.fileitem.name} 加入整理队列失败：{error}", exc_info=True)
            message = str(error) if isinstance(error, TransferAdmissionConflictError) else "未能加入整理队列，请稍后重试"
            errors.append(f"{task.fileitem.name} {message}")
            submission.record(task.fileitem, "failed", message, target_dir=task.target_path)
            return None

    def _build_transfer_tasks(
        self,
        *,
        file_items: List[Tuple[FileItem, bool]],
        inherited_meta_map: Dict[Tuple[str, str], MetaBase],
        build_file_meta: Callable[[Path, Optional[List[str]]], Optional[MetaBase]],
        meta: Optional[MetaBase],
        mediainfo: Optional[Union[MediaInfo, MusicInfo]],
        media_source: Optional[MediaSource],
        media_id: Optional[str],
        batch_mtype: Optional[MediaType],
        target_directory: Optional[TransferDirectoryConf],
        target_storage: Optional[str],
        target_path: Optional[Path],
        transfer_type: Optional[str],
        scrape: Optional[bool],
        library_type_folder: Optional[bool],
        library_category_folder: Optional[bool],
        downloader: Optional[str],
        download_hash: Optional[str],
        transfer_batch_id: str,
        manual: bool,
        background: bool,
        preview: bool,
        reorganize: bool,
        force: bool,
        continue_callback: Optional[Callable[[], bool]],
        cleanup_dest_fileitem: Optional[FileItem],
        recovery_admission: Optional[TransferAdmission],
        music_release_regions: Optional[list[str]],
        music_release_scripts: Optional[list[str]],
        selected_music_track_map: dict[str, MusicInfo],
        submission: _TransferSubmissionCollector,
    ) -> Tuple[List[TransferTask], bool, List[str], int, set[Tuple[str, str]]]:
        """从冻结候选构建任务，并集中执行历史去重与 durable 绑定。"""
        all_success = True
        transfer_tasks: List[TransferTask] = []
        err_msgs: List[str] = []
        skipped_history_count = 0
        skipped_torrents = set()
        cleanup_intent_assigned = False
        multi_track_music_batch = (
            batch_mtype == MediaType.MUSIC
            and sum(self._is_audio_file(item) for item, _ in file_items) > 1
        )
        try:
            for file_item, bluray_dir in file_items:
                if runtime_stop_state.is_system_stopped:
                    raise OperationInterrupted()
                if continue_callback and not continue_callback():
                    raise OperationInterrupted()
                file_path = Path(file_item.path)

                # 自动整理按 app/application/history.py 的统一判定去重（失败记录放行重试、
                # 成功但源文件已变化放行交 overwrite_mode 决断）；手动整理可清理失败记录，
                # 或按用户确认清理成功记录；手动显式指定媒体身份时，先解除旧失败任务再重新规划。
                if (
                    not force or reorganize or (manual and (media_source is not None or media_id is not None))
                ) and not preview:
                    transfer_history_oper = self.transfer_history_repository
                    transferd = self._get_manual_transfer_history(
                        fileitem=file_item,
                        transfer_history_oper=transfer_history_oper,
                        include_move_dest=bool(manual and reorganize),
                    )
                    if transferd:
                        should_reorganize = manual and (reorganize or not transferd.status)
                        if should_reorganize:
                            if not reorganize and not (media_source is not None or media_id is not None):
                                durable_retry = self._request_durable_transfer_retry(
                                    transferd,
                                    requested_by="manual_reorganize",
                                )
                                if durable_retry is not None:
                                    accepted, message = durable_retry
                                    submission.record(file_item, "retry_wait" if accepted else "failed", message)
                                    if accepted:
                                        logger.info(message)
                                    else:
                                        all_success = False
                                        logger.error(message)
                                        err_msgs.append(message)
                                    # 普通失败重试仍由唯一 durable 调度器继续原计划。
                                    continue
                            state, message = self._delete_manual_transfer_history(
                                history=transferd,
                                transfer_history_oper=transfer_history_oper,
                            )
                            if not state:
                                submission.record(file_item, "failed", message)
                                all_success = False
                                logger.error(message)
                                err_msgs.append(message)
                                continue
                            logger.info(f"{file_item.path} 已清理旧整理记录，继续重新整理。")
                            transferd = None

                    if transferd:
                        fingerprint = history_file_fingerprint(file_item)
                        history_description = describe_history_gate(transferd, **fingerprint)
                        if not manual:
                            # 自动路径（目录监控、下载器轮询）与监控分发共用同一套判定，
                            # 否则监控层刚放行的失败重试与升级请求会在这里被全额收回
                            gate_action = evaluate_history_gate(
                                transferd,
                                **fingerprint,
                                retry_count=getattr(transferd, "retry_count", None),
                            )
                            if not is_skip_action(gate_action):
                                logger.info(f"{file_item.path} 命中{history_description}，重新送入整理")
                                transferd = None

                        if transferd:
                            submission.record(file_item, "skipped" if transferd.status else "failed", f"{file_item.name} 已整理过")
                            skipped_history_count += 1
                            if not transferd.status:
                                all_success = False
                            # 失败记录能走到这里说明重试次数已用尽，此时同样要打已整理标签让种子
                            # 退出轮询，否则下载器每一轮都会重新扫描并在这里被拦一次，空转且刷屏
                            candidate_hash = download_hash or transferd.download_hash
                            candidate_downloader = downloader or transferd.downloader
                            if candidate_hash and candidate_downloader:
                                skipped_torrents.add((candidate_hash, candidate_downloader))
                            logger.info(
                                f"{file_item.path} 已整理过（{history_description}），如需重新处理，请删除整理记录。"
                            )
                            err_msgs.append(f"{file_item.name} 已整理过")
                            continue

                # 提前获取下载历史，以便获取自定义识别词
                download_history_repository = self.download_history_repository
                download_history = self._resolve_download_history(
                    repository=download_history_repository,
                    file_path=file_path,
                    bluray_dir=bluray_dir,
                    download_hash=download_hash,
                )

                discard_music_identity = _should_discard_batch_music_identity(
                    multi_track_music_batch=multi_track_music_batch, manual=manual,
                    media_source=media_source,
                    media_id=media_id,
                    mediainfo=mediainfo,
                    history_music_type=self._download_history_music_type(download_history),
                )
                history_music_meta, history_music_info = self._restore_music_download_context(
                    download_history=download_history,
                    file_path=file_path,
                    discard_saved_identity=discard_music_identity,
                )

                if not meta:
                    # 文件元数据(优先使用订阅识别词)
                    inherited_meta = inherited_meta_map.get(self._get_file_key(file_item))
                    if history_music_meta:
                        file_meta = history_music_meta
                    elif inherited_meta:
                        file_meta = deepcopy(inherited_meta)
                    else:
                        file_meta = build_file_meta(
                            file_path,
                            self._get_subscribe_custom_words(download_history),
                        )
                else:
                    file_meta = build_file_meta(file_path, None)

                if not file_meta:
                    submission.record(file_item, "failed", f"{file_path.name} 无法识别有效信息")
                    all_success = False
                    logger.error(f"{file_path.name} 无法识别有效信息")
                    err_msgs.append(f"{file_path.name} 无法识别有效信息")
                    continue

                # 获取下载Hash
                if download_history and (not downloader or not download_hash):
                    _downloader = download_history.downloader
                    _download_hash = download_history.download_hash
                else:
                    _downloader = downloader
                    _download_hash = download_hash

                # 自动整理预载的媒体信息来自整条下载历史；电影合集内文件年份冲突时逐文件识别。
                file_meta, task_mediainfo = self._selected_music_task_context(
                    file_item, file_path, file_meta, selected_music_track_map,
                    None if discard_music_identity
                    else mediainfo or history_music_info,
                )
                if not task_mediainfo and isinstance(file_meta, MetaMusic):
                    # 无标签音频或误带单曲身份的整包按目录级专辑匹配；命中结果带缓存不会逐文件重复请求
                    file_meta, task_mediainfo = self._match_music_album_context(
                        file_item, file_path, file_meta, music_release_regions, music_release_scripts,
                    )
                    if not task_mediainfo and discard_music_identity:
                        task_mediainfo = self._music_info_from_meta(file_meta)
                if not manual and task_mediainfo and self._is_movie_year_conflict(file_meta, task_mediainfo):
                    task_mediainfo = None

                transfer_task = TransferTask(
                    fileitem=file_item,
                    meta=file_meta,
                    mediainfo=task_mediainfo,
                    media_source=media_source,
                    media_id=media_id,
                    mtype=batch_mtype,
                    target_directory=target_directory,
                    target_storage=target_storage,
                    target_path=target_path,
                    transfer_type=transfer_type,
                    scrape=scrape,
                    library_type_folder=library_type_folder,
                    library_category_folder=library_category_folder,
                    downloader=_downloader,
                    download_hash=_download_hash,
                    download_history=download_history,
                    transfer_batch_id=transfer_batch_id,
                    manual=manual,
                    background=background,
                    preview=preview,
                )
                cleanup_intent = cleanup_dest_fileitem if not preview and not cleanup_intent_assigned else None
                transfer_task.bind_planning_input(
                    self._TransferChain__build_planning_input(
                        transfer_task,
                        cleanup_dest_fileitem=cleanup_intent,
                    )
                )
                if (
                    recovery_admission
                    and file_item.storage == recovery_admission.storage
                    and file_item.path == recovery_admission.src_path
                ):
                    transfer_task.bind_admission_task_id(recovery_admission.task_id)
                    self._TransferChain__bind_claimed_admission(
                        transfer_task,
                        recovery_admission,
                    )
                    if recovery_admission.planning_input:
                        transfer_task.bind_planning_input(recovery_admission.planning_input)
                    if recovery_admission.checkpoint:
                        transfer_task.bind_plan_checkpoint(recovery_admission.checkpoint)
                if background:
                    queued = self._enqueue_transfer_candidate(transfer_task, err_msgs, submission)
                    if queued is None:
                        all_success = False
                        continue
                    if queued:
                        submission.record(file_item, "accepted", "已添加到整理队列", target_dir=target_path)
                        if cleanup_intent:
                            cleanup_intent_assigned = True
                        logger.info(f"{file_path.name} 已添加到整理队列")
                    else:
                        logger.debug(f"{file_path.name} 已在整理队列中，跳过")
                else:
                    # 加入列表
                    queued = self._TransferChain__put_to_jobview(transfer_task)
                    if queued:
                        self._register_scrape_batch_task(transfer_task)
                        transfer_tasks.append(transfer_task)
                        if cleanup_intent:
                            cleanup_intent_assigned = True
                    else:
                        logger.debug(f"{file_path.name} 已在整理列表中，跳过")
                if not queued and manual:
                    submission.record(file_item, "failed", "已在整理队列中，请等待当前任务结束后重新整理")
                    all_success = False
                    err_msgs.append(f"{file_path.name} 已在整理队列中，请等待当前任务结束后重新整理")
        except OperationInterrupted:
            self._cancel_unexecuted_tasks(transfer_tasks)
            raise
        finally:
            file_items.clear()
            del file_items
            self._close_scrape_batch(transfer_batch_id)

        return (
            transfer_tasks,
            all_success,
            err_msgs,
            skipped_history_count,
            skipped_torrents,
        )

    def _cancel_unexecuted_tasks(self, tasks: list[TransferTask]) -> None:
        """撤销本次同步请求尚未执行的内存占位，让用户之后可以重新提交这些文件。"""
        for task in tasks:
            self.jobview.fail_unfinished_task(task)
            self.jobview.try_remove_job(task)
            self._finish_scrape_batch_task(task)

    def _submission_execution_state(self, task: TransferTask, enabled: bool) -> Optional[str]:
        """失败后读取既有持久投影，避免把等待恢复或人工复核误报为可重新提交。"""
        if not enabled or task.terminal_settled or not task.admission_task_id:
            return None
        try:
            snapshot = self.transfer_execution_repository.get_snapshot(task_id=task.admission_task_id)
            return snapshot.state.value if snapshot else None
        except Exception as error:
            logger.error(f"读取整理提交回执状态失败：{task.admission_task_id} - {error}")
            return "unknown"

    def _execute_transfer_tasks(
        self,
        *,
        transfer_tasks: List[TransferTask],
        preview: bool,
        continue_callback: Optional[Callable[[], bool]],
        all_success: bool,
        err_msgs: List[str],
        submission: _TransferSubmissionCollector,
    ) -> Tuple[bool, List[str], List[dict[str, Any]]]:
        """同步消费已规划任务；后台任务只由 queue owner 消费。"""
        preview_items: List[dict[str, Any]] = []

        def _preview_callback(task: TransferTask, transferinfo: TransferInfo) -> Tuple[bool, str]:
            """收集预览结果，保持任务执行顺序。"""
            preview_items.append(build_transfer_preview_item(task, transferinfo))
            return transferinfo.success, transferinfo.message

        if transfer_tasks:
            # 总数量
            total_num = len(transfer_tasks)
            # 已处理数量
            processed_num = 0
            # 失败数量
            fail_num = 0
            # 已完成文件
            finished_files = []

            progress = None
            if not preview:
                # 启动进度
                progress = ProgressHelper(ProgressKey.FileTransfer)
                progress.start()
                __process_msg = f"开始整理，共 {total_num} 个文件 ..."
                logger.info(__process_msg)
                progress.update(value=0, text=__process_msg)
            try:
                for task_index, transfer_task in enumerate(transfer_tasks):
                    if runtime_stop_state.is_system_stopped or (continue_callback and not continue_callback()):
                        self._cancel_unexecuted_tasks(transfer_tasks[task_index:])
                        all_success = False
                        err_msgs.append("整理已取消，剩余文件未执行")
                        break
                    if not preview:
                        # 更新进度
                        __process_msg = (
                            f"正在整理 （{processed_num + fail_num + 1}/{total_num}）{transfer_task.fileitem.name} ..."
                        )
                        logger.info(__process_msg)
                        progress.update(
                            value=(processed_num + fail_num) / total_num * 100,
                            text=__process_msg,
                            data={
                                "current": Path(transfer_task.fileitem.path).as_posix(),
                                "finished": finished_files,
                            },
                        )
                    terminal = False
                    terminal_settlement: Optional[bool] = None

                    def callback_after_terminal_settlement(
                        callback_task: TransferTask,
                        transferinfo: TransferInfo,
                    ) -> Tuple[bool, str]:
                        """同步路径也由默认回调原子提交历史、事件与 durable 终态。"""
                        nonlocal terminal_settlement
                        submission.capture(callback_task, transferinfo)
                        callback = _preview_callback if preview else self._TransferChain__default_callback
                        try:
                            return callback(callback_task, transferinfo)
                        finally:
                            if not callback_task.preview:
                                terminal_settlement = callback_task.terminal_settled

                    try:
                        self._TransferChain__claim_task_for_execution(transfer_task)
                        self._TransferChain__start_job_execution(transfer_task)
                        state, err_msg = self._TransferChain__handle_transfer(
                            task=transfer_task,
                            callback=callback_after_terminal_settlement,
                        )
                        terminal = bool(preview or transfer_task.plan_checkpoint is not None)
                    except Exception as e:
                        if terminal_settlement is not None:
                            terminal = True
                        logger.error(
                            f"{transfer_task.fileitem.name} 整理任务处理出现错误：{e} - {traceback.format_exc()}"
                        )
                        if not preview:
                            self._TransferChain__fail_transfer_task(transfer_task, e)
                        state, err_msg = False, (
                            str(e) if isinstance(e, TransferAdmissionConflictError)
                            else "整理任务处理失败，请稍后重试"
                        )
                    finally:
                        durable_settled = self._TransferChain__finish_job_execution(
                            transfer_task,
                            terminal=terminal,
                            terminal_settlement=terminal_settlement,
                        )
                    if terminal and not durable_settled:
                        state = False
                        err_msg = "整理任务结果暂未确认，后台将自动重试"
                    submission.execution(
                        transfer_task, state, err_msg,
                        durable_state=self._submission_execution_state(transfer_task, submission.enabled),
                    )
                    if not state:
                        all_success = False
                        logger.warn(f"{transfer_task.fileitem.name} {err_msg}")
                        err_msgs.append(f"{transfer_task.fileitem.name} {err_msg}")
                        if preview:
                            # 预览模式不走默认回调，这里需要手动收敛任务状态，避免残留 running
                            self.jobview.fail_task(transfer_task)
                            self.jobview.try_remove_job(transfer_task)
                        if preview and (
                            not preview_items or preview_items[-1].get("source") != transfer_task.fileitem.path
                        ):
                            preview_items.append(
                                build_transfer_preview_item(transfer_task, TransferInfo(
                                    success=False, message=err_msg, failure_stage="execution",
                                    recovery_action="刷新整理历史，等待任务结束；仍无法继续时提交人工复核",
                                ))
                            )
                        fail_num += 1
                    else:
                        if preview:
                            # 预览模式手动标记完成，确保可重复预览
                            self.jobview.finish_task(transfer_task)
                            self.jobview.try_remove_job(transfer_task)
                        processed_num += 1
                    finished_files.append(Path(transfer_task.fileitem.path).as_posix())
            finally:
                transfer_tasks.clear()
                del transfer_tasks

            if not preview:
                __end_msg = f"整理队列处理完成，共整理 {total_num} 个文件，失败 {fail_num} 个"
                logger.info(__end_msg)
                progress.update(value=100, text=__end_msg, data={})
                progress.end()

        return all_success, err_msgs, preview_items
