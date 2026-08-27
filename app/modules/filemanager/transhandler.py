import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple, cast

from jinja2 import Template

from app.adapters.system.host import SystemUtils
from app.application.audio import AudioMetadataHelper
from app.application.directory import DirectoryHelper
from app.application.messaging.message import TemplateHelper
from app.application.transfer import (
    TransferPlanCheckpoint,
    TransferPlanItem,
    TransferPlanningInput,
)
from app.domain.context import MediaInfo, MusicInfo
from app.domain.meta.metabase import MetaBase
from app.domain.meta.metamusic import MetaMusic
from app.domain.metainfo import MetaInfoPath
from app.modules.filemanager.storages import StorageBase
from app.runtime.events import eventmanager
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.schemas.event import (
    TransferInterceptEventData,
    TransferOverwriteCheckEventData,
    TransferRenameBuildEventData,
    TransferRenameEventData,
)
from app.schemas.exception import StorageQueryError
from app.schemas.system import TransferDirectoryConf
from app.schemas.tmdb import TmdbEpisode
from app.schemas.transfer import TransferInfo
from app.schemas.types import ChainEventType, MediaType
from app.schemas.workflow import FileItem


class TransHandler:
    """
    文件转移整理类
    """

    def __init__(self) -> None:
        """初始化无状态整理执行器。"""
        pass

    @staticmethod
    def __normalize_disc_folder_name(value: Optional[str]) -> Optional[str]:
        """
        从 Disc/Disk/DVD/CD 标识中提取盘号并统一为 Disc N。
        """
        if not value:
            return None
        match = re.search(
            r"(?:disc|disk|dvd|cd)[\s._-]*0*(\d{1,3})",
            value,
            re.IGNORECASE,
        )
        if not match:
            return None
        return f"Disc {int(match.group(1))}"

    @classmethod
    def __get_tv_bluray_dir_path(
            cls,
            rendered_path: Path,
            source_item: FileItem,
            meta: MetaBase,
    ) -> Path:
        """
        电视剧原盘目录没有单集文件名，保留季目录并追加盘片目录。
        """
        disc_folder = cls.__normalize_disc_folder_name(getattr(meta, "part", None))
        if not disc_folder and source_item:
            source_name = source_item.name or Path(source_item.path).name
            disc_folder = cls.__normalize_disc_folder_name(source_name)
            if not disc_folder:
                match = re.search(
                    r"(?:^|[^A-Za-z0-9])S\d{1,3}D0*(\d{1,3})(?:[^A-Za-z0-9]|$)",
                    source_name,
                    re.IGNORECASE,
                )
                if match:
                    disc_folder = f"Disc {int(match.group(1))}"
            if not disc_folder:
                disc_folder = source_name

        return rendered_path.parent / (disc_folder or "Disc")

    @staticmethod
    def __update_result(result: TransferInfo, **kwargs):
        """
        更新结果
        """
        # 设置值
        for key, value in kwargs.items():
            if hasattr(result, key):
                current_value = getattr(result, key)
                if current_value is None:
                    current_value = value
                elif isinstance(current_value, list):
                    if isinstance(value, list):
                        current_value.extend(value)
                    else:
                        current_value.append(value)
                elif isinstance(current_value, dict):
                    if isinstance(value, dict):
                        current_value.update(value)
                    else:
                        current_value[key] = value
                elif isinstance(current_value, bool):
                    current_value = value
                elif isinstance(current_value, int):
                    current_value += value or 0
                else:
                    current_value = value
                setattr(result, key, current_value)

    @staticmethod
    def __build_preview_item(
            storage: str,
            path: Path,
            item_type: str,
            size: Optional[int] = None,
    ) -> FileItem:
        """
        构造预览结果中的文件项，不访问真实存储。
        """
        return FileItem(
            storage=storage,
            path=path.as_posix(),
            name=path.name,
            basename=path.stem,
            type=item_type,
            extension=path.suffix.lstrip(".") if item_type == "file" else None,
            size=size if item_type == "file" else None,
        )

    @staticmethod
    def __music_quality_overwrite_decision(
            meta: MetaBase,
            mediainfo: MediaInfo | MusicInfo,
            target_item: FileItem,
    ) -> Optional[bool]:
        """比较新旧音乐的实际音质，无法形成可靠结论时交回原覆盖策略。"""
        if getattr(mediainfo, "type", None) != MediaType.MUSIC or not target_item:
            return None

        source_score = getattr(meta, "audio_quality_score", 0) or getattr(
            mediainfo, "audio_quality_score", 0
        )
        target_path = Path(target_item.path) if target_item.path else None
        if (
                target_path
                and (target_item.storage or "local") == "local"
                and target_path.is_file()
        ):
            target_music = AudioMetadataHelper.read(target_path)
        else:
            target_format = target_item.extension
            if not target_format and target_path:
                target_format = target_path.suffix.lstrip(".")
            target_music = MetaMusic(audio_format=target_format)
        target_score = target_music.audio_quality_score
        if not source_score or not target_score or source_score == target_score:
            return None
        return source_score > target_score

    @staticmethod
    def __serialize_fileitem(fileitem: FileItem) -> dict[str, Any]:
        """生成可持久化且不再引用调用方对象的文件快照。"""
        return cast(dict[str, Any], fileitem.model_dump(mode="json"))

    @staticmethod
    def __serialize_transfer_model(value: object) -> dict[str, Any]:
        """校验旧领域对象的动态序列化结果，阻断 Any 向检查点扩散。"""
        serializer = getattr(value, "to_dict", None)
        if not callable(serializer):
            raise TypeError(f"{type(value).__name__} 不支持整理快照序列化")
        payload = serializer()
        if not isinstance(payload, dict):
            raise TypeError(f"{type(value).__name__} 返回了无效整理快照")
        return payload

    @staticmethod
    def __is_subtitle_file(fileitem: FileItem) -> bool:
        """判断文件是否为配置支持的字幕附件。"""
        return bool(
            fileitem.extension
            and f".{fileitem.extension.lower()}" in get_runtime_setting("RMT_SUBEXT")
        )

    @staticmethod
    def __is_extra_file(fileitem: FileItem, mediainfo: MediaInfo | MusicInfo) -> bool:
        """判断文件是否为需要无条件覆盖的媒体附件。"""
        if not fileitem.extension:
            return False
        extension = f".{fileitem.extension.lower()}"
        path = str(fileitem.path or fileitem.name or "").casefold()
        if extension in get_runtime_setting("RMT_SUBEXT"):
            return True
        if mediainfo.type == MediaType.MUSIC and path.endswith(
            (".lrc", ".txt", ".lyricsfile.yaml")
        ):
            return True
        return (
            mediainfo.type != MediaType.MUSIC
            and extension in get_runtime_setting("RMT_AUDIOEXT")
        )

    @staticmethod
    def __is_special_extra_file(fileitem: FileItem) -> bool:
        """识别没有季集号但允许合法跳过的特典视频。"""
        return bool(
            re.search(
                r"(?:^|[\s_.\-\[【(])(NC(?:OP|ED)|NCOP|NCED|OP|ED|MENU|PV|CM|TRAILER|"
                r"TV\s*SPOT|SP|OVA|OAD|EVENT|IV|INTERVIEW|LOGO|PRODUCER\s*LOGO|"
                r"BEHIND\s*THE\s*SCENES|FEATURETTE"
                r")(?:\d*|[\s_.\-\]】)]|$)",
                fileitem.name or "",
                re.IGNORECASE,
            )
        )

    def __plan_directory_items(
        self,
        *,
        fileitem: FileItem,
        target_storage: str,
        target_path: Path,
        source_oper: StorageBase,
    ) -> tuple[TransferPlanItem, ...]:
        """只读遍历源目录并冻结叶子文件的稳定执行顺序。"""
        items: list[TransferPlanItem] = []

        def collect(source_dir: FileItem, destination_dir: Path) -> None:
            """按存储返回顺序深度优先收集叶子文件。"""
            for source_item in source_oper.list(source_dir) or []:
                destination = destination_dir / source_item.name
                if source_item.type == "dir":
                    collect(source_item, destination)
                    continue
                items.append(
                    TransferPlanItem(
                        sequence=len(items),
                        source_fileitem=self.__serialize_fileitem(source_item),
                        target_storage=target_storage,
                        target_path=destination.as_posix(),
                    )
                )

        collect(fileitem, target_path)
        return tuple(items)

    def plan_transfer(
        self,
        planning_input: TransferPlanningInput,
        *,
        meta: MetaBase,
        mediainfo: MediaInfo | MusicInfo,
        source_oper: StorageBase,
        target_storage: str,
        target_path: Path,
        transfer_type: str,
        need_scrape: bool,
        need_rename: bool,
        need_notify: bool,
        overwrite_mode: Optional[str],
        episodes_info: Optional[List[TmdbEpisode]],
        preview: bool,
    ) -> TransferPlanCheckpoint:
        """只计算最终目标和有序操作，不触发任何文件或目录写副作用。"""
        fileitem = FileItem(**planning_input.source_fileitem)
        rename_format = get_runtime_setting("RENAME_FORMAT")(mediainfo.type)
        planning_meta = deepcopy(meta)
        resolved_mediainfo = self.__serialize_transfer_model(mediainfo)
        resolved_episodes_info = tuple(
            episode.model_dump(mode="json") for episode in episodes_info or []
        )

        if fileitem.type == "dir":
            if need_rename:
                rendered_path = self.get_rename_path(
                    path=target_path,
                    template_string=rename_format,
                    rename_dict=self.get_naming_dict(
                        meta=planning_meta,
                        mediainfo=mediainfo,
                    ),
                    source_path=fileitem.path,
                    source_item=fileitem,
                )
                if mediainfo.type == MediaType.TV:
                    final_target = self.__get_tv_bluray_dir_path(
                        rendered_path=rendered_path,
                        source_item=fileitem,
                        meta=planning_meta,
                    )
                else:
                    final_target = DirectoryHelper.get_media_root_path(
                        rename_format,
                        rename_path=rendered_path,
                        media_type=mediainfo.type,
                    )
                if not final_target:
                    raise ValueError("重命名格式无效")
            else:
                final_target = target_path / fileitem.name
            items = (
                ()
                if preview
                else self.__plan_directory_items(
                    fileitem=fileitem,
                    target_storage=target_storage,
                    target_path=final_target,
                    source_oper=source_oper,
                )
            )
            return TransferPlanCheckpoint(
                planning_input=planning_input,
                target_storage=target_storage,
                root_target_path=target_path.as_posix(),
                final_target_path=final_target.as_posix(),
                resolved_transfer_type=transfer_type,
                items=items,
                resolved_meta=self.__serialize_transfer_model(planning_meta),
                resolved_meta_kind=(
                    type(planning_meta).__name__ if planning_meta else None
                ),
                resolved_mediainfo=resolved_mediainfo,
                resolved_mediainfo_kind=(
                    type(mediainfo).__name__ if mediainfo else None
                ),
                resolved_episodes_info=resolved_episodes_info,
                need_scrape=need_scrape,
                need_rename=need_rename,
                need_notify=need_notify,
                overwrite_mode=overwrite_mode,
                preview=preview,
                skip_reason=(
                    "源目录中没有可整理文件"
                    if not preview and not items
                    else None
                ),
            )

        if mediainfo.type == MediaType.TV:
            if planning_meta.begin_episode is None:
                if self.__is_special_extra_file(fileitem):
                    return TransferPlanCheckpoint(
                        planning_input=planning_input,
                        target_storage=target_storage,
                        root_target_path=target_path.as_posix(),
                        final_target_path=target_path.as_posix(),
                        resolved_transfer_type=transfer_type,
                        items=(),
                        resolved_meta=(
                            self.__serialize_transfer_model(planning_meta)
                        ),
                        resolved_meta_kind=(
                            type(planning_meta).__name__ if planning_meta else None
                        ),
                        resolved_mediainfo=resolved_mediainfo,
                        resolved_mediainfo_kind=(
                            type(mediainfo).__name__ if mediainfo else None
                        ),
                        resolved_episodes_info=resolved_episodes_info,
                        need_scrape=need_scrape,
                        need_rename=need_rename,
                        need_notify=False,
                        overwrite_mode=overwrite_mode,
                        preview=preview,
                        skip_reason="未识别到文件集数，识别为特典/附加视频文件",
                    )
                raise ValueError("未识别到文件集数")
            planning_meta.end_season = None
            if planning_meta.total_season:
                planning_meta.total_season = 1
            if planning_meta.total_episode > 2:
                planning_meta.total_episode = 1
                planning_meta.end_episode = None

        if need_rename:
            file_extension = (
                ".lyricsfile.yaml"
                if str(fileitem.path or "").casefold().endswith(".lyricsfile.yaml")
                else f".{fileitem.extension}"
            )
            final_target = self.get_rename_path(
                path=target_path,
                template_string=rename_format,
                rename_dict=self.get_naming_dict(
                    meta=planning_meta,
                    mediainfo=mediainfo,
                    episodes_info=episodes_info,
                    file_ext=file_extension,
                ),
                source_path=fileitem.path,
                source_item=fileitem,
            )
            if self.__is_subtitle_file(fileitem):
                final_target = self.__rename_subtitles(fileitem, final_target)
            target_directory_path = DirectoryHelper.get_media_root_path(
                rename_format,
                rename_path=final_target,
                media_type=mediainfo.type,
            )
            if not target_directory_path:
                raise ValueError("重命名格式无效")
        else:
            final_target = target_path / fileitem.name

        return TransferPlanCheckpoint(
            planning_input=planning_input,
            target_storage=target_storage,
            root_target_path=target_path.as_posix(),
            final_target_path=final_target.as_posix(),
            resolved_transfer_type=transfer_type,
            items=(
                TransferPlanItem(
                    sequence=0,
                    source_fileitem=self.__serialize_fileitem(fileitem),
                    target_storage=target_storage,
                    target_path=final_target.as_posix(),
                ),
            ),
            resolved_meta=self.__serialize_transfer_model(planning_meta),
            resolved_meta_kind=(
                type(planning_meta).__name__ if planning_meta else None
            ),
            resolved_mediainfo=resolved_mediainfo,
            resolved_mediainfo_kind=(
                type(mediainfo).__name__ if mediainfo else None
            ),
            resolved_episodes_info=resolved_episodes_info,
            need_scrape=need_scrape,
            need_rename=need_rename,
            need_notify=need_notify,
            overwrite_mode=overwrite_mode,
            preview=preview,
        )

    @staticmethod
    def __intercept_transfer(
        *,
        fileitem: FileItem,
        meta: Optional[MetaBase],
        mediainfo: MediaInfo | MusicInfo,
        target_storage: str,
        target_path: Path,
        transfer_type: str,
        over_flag: Optional[bool] = None,
    ) -> tuple[bool, str]:
        """在宿主写副作用前执行插件拦截并返回取消原因。"""
        if over_flag is None:
            event_data = TransferInterceptEventData(
                fileitem=fileitem,
                mediainfo=mediainfo,
                target_storage=target_storage,
                target_path=target_path,
                transfer_type=transfer_type,
            )
        else:
            event_data = TransferInterceptEventData(
                fileitem=fileitem,
                meta=meta,
                mediainfo=mediainfo,
                target_storage=target_storage,
                target_path=target_path,
                transfer_type=transfer_type,
                options={"over_flag": over_flag},
            )
        event = eventmanager.send_event(ChainEventType.TransferIntercept, event_data)
        if (
                event
                and isinstance(event.event_data, TransferInterceptEventData)
                and event.event_data.cancel
        ):
            canceled = event.event_data
            logger.debug(
                f"Transfer canceled by event: {canceled.source},Reason: {canceled.reason}"
            )
            return False, canceled.reason
        return True, ""

    def __resolve_overwrite(
        self,
        *,
        fileitem: FileItem,
        meta: MetaBase,
        mediainfo: MediaInfo | MusicInfo,
        target_oper: StorageBase,
        target_storage: str,
        target_file: Path,
        transfer_type: str,
        overwrite_mode: Optional[str],
        need_notify: bool,
    ) -> tuple[bool, bool, Optional[TransferInfo]]:
        """在执行期完成覆盖事件与策略判断，不改变冻结目标。"""
        if self.__is_extra_file(fileitem, mediainfo):
            return True, False, None
        try:
            target_item = target_oper.get_item_strict(target_file)
        except StorageQueryError as query_error:
            message = (
                f"无法确认目标文件状态，已跳过整理以避免误覆盖："
                f"{target_file} - {query_error}"
            )
            logger.warn(message)
            return False, False, TransferInfo(
                success=False,
                message=message,
                fileitem=fileitem,
                fail_list=[fileitem.path],
                transfer_type=transfer_type,
                need_notify=need_notify,
            )
        if not target_item:
            return False, overwrite_mode == "latest", None

        checked_target = target_file
        over_flag = False
        if target_storage == "local" and target_file.is_symlink():
            checked_target = target_file.readlink()
            if not checked_target.exists():
                over_flag = True
        if over_flag:
            return True, False, None

        logger.info(
            f"目的文件系统中已经存在同名文件 {checked_target}，"
            f"当前整理覆盖模式设置为 {overwrite_mode}"
        )
        event_data = TransferOverwriteCheckEventData(
            fileitem=fileitem,
            target_item=target_item,
            target_storage=target_storage,
            target_path=target_file,
            overwrite_mode=overwrite_mode or "",
            transfer_type=transfer_type,
        )
        event = eventmanager.send_event(
            ChainEventType.TransferOverwriteCheck,
            event_data,
        )
        plugin_overwrite: Optional[bool] = None
        plugin_source_size: Optional[int] = None
        plugin_target_size: Optional[int] = None
        plugin_reason: Optional[str] = None
        if event and isinstance(
                event.event_data,
                TransferOverwriteCheckEventData,
        ):
            plugin_event_data = event.event_data
            plugin_overwrite = plugin_event_data.overwrite
            plugin_source_size = plugin_event_data.source_size
            plugin_target_size = plugin_event_data.target_size
            plugin_reason = plugin_event_data.reason
        if plugin_overwrite is True:
            return True, False, None
        if plugin_overwrite is False:
            return False, False, TransferInfo(
                success=False,
                message=plugin_reason or "插件决定不覆盖已有文件",
                fileitem=fileitem,
                target_item=target_item,
                fail_list=[fileitem.path],
                transfer_type=transfer_type,
                need_notify=need_notify,
                overwrite_skipped=True,
            )
        if overwrite_mode in {"always", "latest"}:
            return True, False, None
        if overwrite_mode == "never":
            return False, False, TransferInfo(
                success=False,
                message="媒体库存在同名文件，当前覆盖模式为不覆盖",
                fileitem=fileitem,
                target_item=target_item,
                fail_list=[fileitem.path],
                transfer_type=transfer_type,
                need_notify=need_notify,
                overwrite_skipped=True,
            )
        if overwrite_mode == "size":
            music_overwrite = self.__music_quality_overwrite_decision(
                meta=meta,
                mediainfo=mediainfo,
                target_item=target_item,
            )
            if music_overwrite is True:
                return True, False, None
            if music_overwrite is False:
                return False, False, TransferInfo(
                    success=False,
                    message="媒体库存在同名音乐文件，且目标音质更好",
                    fileitem=fileitem,
                    target_item=target_item,
                    fail_list=[fileitem.path],
                    transfer_type=transfer_type,
                    need_notify=need_notify,
                    overwrite_skipped=True,
                )
            source_size = (
                plugin_source_size
                if plugin_source_size is not None
                else fileitem.size
            )
            target_size = (
                plugin_target_size
                if plugin_target_size is not None
                else target_item.size
            )
            if target_size < source_size:
                return True, False, None
            return False, False, TransferInfo(
                success=False,
                message="媒体库存在同名文件，且质量更好",
                fileitem=fileitem,
                target_item=target_item,
                fail_list=[fileitem.path],
                transfer_type=transfer_type,
                need_notify=need_notify,
            )
        return False, False, None

    def execute_transfer_plan(
        self,
        checkpoint: TransferPlanCheckpoint,
        *,
        meta: MetaBase,
        mediainfo: MediaInfo | MusicInfo,
        source_oper: StorageBase,
        target_oper: StorageBase,
        cleanup_before_transfer: Optional[Callable[[], None]] = None,
    ) -> TransferInfo:
        """只消费冻结目标和有序操作，并在执行期处理覆盖与插件拦截。"""
        fileitem = FileItem(**checkpoint.planning_input.source_fileitem)
        target_storage = checkpoint.target_storage
        target_path = Path(checkpoint.final_target_path)
        transfer_type = checkpoint.resolved_transfer_type
        result = TransferInfo()

        if checkpoint.skip_reason:
            logger.info(f"文件 {fileitem.path} 跳过整理：{checkpoint.skip_reason}")
            return TransferInfo(
                success=True,
                message=checkpoint.skip_reason,
                fileitem=fileitem,
                transfer_type=transfer_type,
                need_notify=False,
            )

        if checkpoint.preview:
            item_type = "dir" if fileitem.type == "dir" else "file"
            preview_target_item = self.__build_preview_item(
                storage=target_storage,
                path=target_path,
                item_type=item_type,
                size=fileitem.size,
            )
            preview_target_diritem = (
                preview_target_item
                if item_type == "dir"
                else self.__build_preview_item(
                    storage=target_storage,
                    path=target_path.parent,
                    item_type="dir",
                )
            )
            return TransferInfo(
                success=True,
                fileitem=fileitem,
                target_item=preview_target_item,
                target_diritem=preview_target_diritem,
                file_list=[fileitem.path],
                file_list_new=[target_path.as_posix()],
                file_count=0 if item_type == "dir" else 1,
                total_size=0 if item_type == "dir" else fileitem.size or 0,
                need_scrape=checkpoint.need_scrape,
                transfer_type=transfer_type,
                need_notify=False,
            )

        if fileitem.type == "dir":
            stream_path = Path(fileitem.path) / "BDMV" / "STREAM"
            stream_sizes = [
                FileItem(**item.source_fileitem).size or 0
                for item in checkpoint.items
                if Path(str(item.source_fileitem.get("path", ""))).parent
                == stream_path
            ]
            if stream_sizes:
                fileitem.size = sum(stream_sizes)
            allowed, reason = self.__intercept_transfer(
                fileitem=fileitem,
                meta=meta,
                mediainfo=mediainfo,
                target_storage=target_storage,
                target_path=target_path,
                transfer_type=transfer_type,
            )
            if not allowed:
                return TransferInfo(
                    success=False,
                    message=reason,
                    fileitem=fileitem,
                    transfer_type=transfer_type,
                    need_notify=checkpoint.need_notify,
                )
            if cleanup_before_transfer:
                cleanup_before_transfer()
            target_diritem = target_oper.get_folder(target_path)
            if not target_diritem:
                return TransferInfo(
                    success=False,
                    message=f"获取目标目录失败：{target_path}",
                    fileitem=fileitem,
                    transfer_type=transfer_type,
                    need_notify=checkpoint.need_notify,
                )
            for planned_item in checkpoint.items:
                if (
                    planned_item.action != "transfer"
                    or planned_item.target_storage != target_storage
                ):
                    return TransferInfo(
                        success=False,
                        message="整理计划包含不支持的操作或目标存储",
                        fileitem=fileitem,
                        transfer_type=transfer_type,
                        need_notify=checkpoint.need_notify,
                    )
                source_item = FileItem(**planned_item.source_fileitem)
                new_item, error = self.__transfer_command(
                    fileitem=source_item,
                    target_storage=planned_item.target_storage,
                    source_oper=source_oper,
                    target_oper=target_oper,
                    target_file=Path(planned_item.target_path),
                    transfer_type=transfer_type,
                )
                if not new_item:
                    return TransferInfo(
                        success=False,
                        message=error,
                        fileitem=fileitem,
                        transfer_type=transfer_type,
                        need_notify=checkpoint.need_notify,
                    )
                self.__update_result(
                    result=result,
                    file_list=[source_item.path],
                    file_list_new=[new_item.path],
                )
            self.__update_result(
                result=result,
                success=True,
                fileitem=fileitem,
                target_item=target_diritem,
                target_diritem=target_diritem,
                need_scrape=checkpoint.need_scrape,
                need_notify=checkpoint.need_notify,
                transfer_type=transfer_type,
            )
            return result

        if len(checkpoint.items) != 1:
            return TransferInfo(
                success=False,
                message="单文件整理计划必须且只能包含一个操作",
                fileitem=fileitem,
                fail_list=[fileitem.path],
                transfer_type=transfer_type,
                need_notify=checkpoint.need_notify,
            )
        planned_item = checkpoint.items[0]
        if (
            planned_item.action != "transfer"
            or planned_item.target_storage != target_storage
            or planned_item.target_path != checkpoint.final_target_path
        ):
            return TransferInfo(
                success=False,
                message="单文件整理计划与冻结目标不一致",
                fileitem=fileitem,
                fail_list=[fileitem.path],
                transfer_type=transfer_type,
                need_notify=checkpoint.need_notify,
            )
        target_file = Path(planned_item.target_path)
        over_flag, delete_versions, overwrite_failure = self.__resolve_overwrite(
            fileitem=fileitem,
            meta=meta,
            mediainfo=mediainfo,
            target_oper=target_oper,
            target_storage=target_storage,
            target_file=target_file,
            transfer_type=transfer_type,
            overwrite_mode=checkpoint.overwrite_mode,
            need_notify=checkpoint.need_notify,
        )
        if overwrite_failure:
            return overwrite_failure
        allowed, reason = self.__intercept_transfer(
            fileitem=fileitem,
            meta=meta,
            mediainfo=mediainfo,
            target_storage=target_storage,
            target_path=target_file,
            transfer_type=transfer_type,
            over_flag=over_flag,
        )
        if not allowed:
            return TransferInfo(
                success=False,
                message=reason,
                fileitem=fileitem,
                fail_list=[fileitem.path],
                transfer_type=transfer_type,
                need_notify=checkpoint.need_notify,
            )

        if cleanup_before_transfer:
            cleanup_before_transfer()
        target_diritem = target_oper.get_folder(target_file.parent)
        if not target_diritem:
            return TransferInfo(
                success=False,
                message=f"目标目录 {target_file.parent} 获取失败",
                fileitem=fileitem,
                fail_list=[fileitem.path],
                transfer_type=transfer_type,
                need_notify=checkpoint.need_notify,
            )
        if delete_versions:
            self.__delete_version_files(target_oper, target_file)
        new_item, error = self.__transfer_file(
            fileitem=fileitem,
            target_storage=target_storage,
            target_file=target_file,
            transfer_type=transfer_type,
            over_flag=over_flag,
            source_oper=source_oper,
            target_oper=target_oper,
            result=result,
        )
        if not new_item:
            error = error or f"{fileitem.path} 整理后未获取到目标文件信息"
            return TransferInfo(
                success=False,
                message=error,
                fileitem=fileitem,
                fail_list=[fileitem.path],
                transfer_type=transfer_type,
                need_notify=checkpoint.need_notify,
            )
        self.__update_result(
            result=result,
            success=True,
            fileitem=fileitem,
            target_item=new_item,
            target_diritem=target_diritem,
            need_scrape=checkpoint.need_scrape,
            transfer_type=transfer_type,
            need_notify=checkpoint.need_notify,
        )
        return result

    @staticmethod
    def __transfer_command(
        fileitem: FileItem,
        target_storage: str,
        source_oper: StorageBase,
        target_oper: StorageBase,
        target_file: Path,
        transfer_type: str,
    ) -> Tuple[Optional[FileItem], str]:
        """
        处理单个文件
        :param fileitem: 源文件
        :param target_storage: 目标存储
        :param source_oper: 源存储操作对象
        :param target_oper: 目标存储操作对象
        :param target_file: 目标文件路径
        :param transfer_type: 整理方式
        """

        def __get_targetitem(_path: Path) -> FileItem:
            """
            获取文件信息
            """
            return FileItem(
                storage=target_storage,
                path=_path.as_posix(),
                name=_path.name,
                basename=_path.stem,
                type="file",
                size=_path.stat().st_size,
                extension=_path.suffix.lstrip("."),
                modify_time=_path.stat().st_mtime,
            )

        def __build_remote_targetitem(_source_item: FileItem, _path: Path) -> FileItem:
            """
            根据已确认的目标路径构造网盘文件信息，用于兼容元数据延迟可见的存储。
            """
            return FileItem(
                storage=target_storage,
                path=_path.as_posix(),
                name=_path.name,
                basename=_path.stem,
                type=_source_item.type or "file",
                size=_source_item.size,
                extension=_path.suffix.lstrip("."),
                modify_time=_source_item.modify_time,
                thumbnail=_source_item.thumbnail,
            )

        def __get_remote_targetitem(_source_item: FileItem, _path: Path) -> FileItem:
            """
            获取网盘目标文件信息，目标存储索引未刷新时使用目标路径兜底。
            """
            target_item = target_oper.get_item(_path)
            if target_item:
                return target_item
            logger.warn(
                f"目标文件【{target_storage}】{_path} 元数据暂不可见，使用目标路径构造整理结果"
            )
            return __build_remote_targetitem(_source_item, _path)

        if (
            fileitem.storage != target_storage
            and fileitem.storage != "local"
            and target_storage != "local"
        ):
            return None, f"不支持 {fileitem.storage} 到 {target_storage} 的文件整理"

        if fileitem.storage == "local" and target_storage == "local":
            # 创建目录
            if not target_file.parent.exists():
                target_file.parent.mkdir(parents=True, exist_ok=True)
            # 本地到本地
            if transfer_type == "copy":
                state = source_oper.copy(fileitem, target_file.parent, target_file.name)
            elif transfer_type == "move":
                state = source_oper.move(fileitem, target_file.parent, target_file.name)
            elif transfer_type == "link":
                state = source_oper.link(fileitem, target_file)
            elif transfer_type == "softlink":
                state = source_oper.softlink(fileitem, target_file)
            else:
                return None, f"不支持的整理方式：{transfer_type}"
            if state:
                return __get_targetitem(target_file), ""
            else:
                return None, f"{fileitem.path} {transfer_type} 失败"
        elif fileitem.storage == "local" and target_storage != "local":
            # 本地到网盘
            filepath = Path(fileitem.path)
            if not filepath.exists():
                return None, f"文件 {filepath} 不存在"
            if transfer_type == "copy":
                # 复制
                # 根据目的路径创建文件夹
                target_fileitem = target_oper.get_folder(target_file.parent)
                if target_fileitem:
                    # 上传文件
                    new_item = target_oper.upload(
                        target_fileitem, filepath, target_file.name
                    )
                    if new_item:
                        return new_item, ""
                    else:
                        return None, f"{fileitem.path} 上传 {target_storage} 失败"
                else:
                    return (
                        None,
                        f"【{target_storage}】{target_file.parent} 目录获取失败",
                    )
            elif transfer_type == "move":
                # 移动
                # 根据目的路径获取文件夹
                target_fileitem = target_oper.get_folder(target_file.parent)
                if target_fileitem:
                    # 上传文件
                    new_item = target_oper.upload(
                        target_fileitem, filepath, target_file.name
                    )
                    if new_item:
                        # 删除源文件
                        source_oper.delete(fileitem)
                        return new_item, ""
                    else:
                        return None, f"{fileitem.path} 上传 {target_storage} 失败"
                else:
                    return (
                        None,
                        f"【{target_storage}】{target_file.parent} 目录获取失败",
                    )
        elif fileitem.storage != "local" and target_storage == "local":
            # 网盘到本地
            if target_file.exists():
                logger.warn(f"文件已存在：{target_file}")
                return __get_targetitem(target_file), ""
            # 网盘到本地
            if transfer_type in ["copy", "move"]:
                # 下载
                tmp_file = source_oper.download(
                    fileitem=fileitem, path=target_file.parent
                )
                if tmp_file:
                    # 创建目录
                    if not target_file.parent.exists():
                        target_file.parent.mkdir(parents=True, exist_ok=True)
                    # 将tmp_file移动后target_file
                    SystemUtils.move(tmp_file, target_file)
                    if transfer_type == "move":
                        # 删除源文件
                        source_oper.delete(fileitem)
                    return __get_targetitem(target_file), ""
                else:
                    return None, f"{fileitem.path} {fileitem.storage} 下载失败"
        elif fileitem.storage == target_storage:
            # 同一网盘
            if not source_oper.is_support_transtype(transfer_type):
                return None, f"存储 {fileitem.storage} 不支持 {transfer_type} 整理方式"

            if transfer_type == "copy":
                # 复制文件到新目录
                target_fileitem = target_oper.get_folder(target_file.parent)
                if target_fileitem:
                    copy_item = getattr(source_oper, "copy_item", None)
                    if callable(copy_item):
                        new_item = copy_item(
                            fileitem, Path(target_fileitem.path), target_file.name
                        )
                        if new_item:
                            return new_item, ""
                    elif source_oper.copy(
                            fileitem, Path(target_fileitem.path), target_file.name
                    ):
                        return __get_remote_targetitem(fileitem, target_file), ""
                    return None, f"【{target_storage}】{fileitem.path} 复制文件失败"
                else:
                    return (
                        None,
                        f"【{target_storage}】{target_file.parent} 目录获取失败",
                    )
            elif transfer_type == "move":
                # 移动文件到新目录
                target_fileitem = target_oper.get_folder(target_file.parent)
                if target_fileitem:
                    move_item = getattr(source_oper, "move_item", None)
                    if callable(move_item):
                        new_item = move_item(
                            fileitem, Path(target_fileitem.path), target_file.name
                        )
                        if new_item:
                            return new_item, ""
                    elif source_oper.move(
                            fileitem, Path(target_fileitem.path), target_file.name
                    ):
                        return __get_remote_targetitem(fileitem, target_file), ""
                    return None, f"【{target_storage}】{fileitem.path} 移动文件失败"
                else:
                    return (
                        None,
                        f"【{target_storage}】{target_file.parent} 目录获取失败",
                    )
            elif transfer_type == "link":
                if source_oper.link(fileitem, target_file):
                    return __get_remote_targetitem(fileitem, target_file), ""
                else:
                    return None, f"【{target_storage}】{fileitem.path} 创建硬链接失败"
            else:
                return None, f"不支持的整理方式：{transfer_type}"

        return None, "未知错误"

    @staticmethod
    def __rename_subtitles(sub_item: FileItem, new_file: Path) -> Path:
        """
        重命名字幕文件，补充附加信息
        """
        # 字幕正则式
        _zhcn_sub_re = (
            r"([.\[(\s](((zh[-_])?(cn|ch[si]|sg|sc))|zho?"
            r"|chinese|(cn|ch[si]|sg|zho?)[-_&]?(cn|ch[si]|sg|zho?|eng|jap|ja|jpn)"
            r"|eng[-_&]?(cn|ch[si]|sg|zho?)|(jap|ja|jpn)[-_&]?(cn|ch[si]|sg|zho?)"
            r"|简[体中]?)[.\])\s])"
            r"|([\u4e00-\u9fa5]{0,3}[中双][\u4e00-\u9fa5]{0,2}[字文语][\u4e00-\u9fa5]{0,3})"
            r"|简体|简中|JPSC|sc_jp"
            r"|(?<![a-z0-9])gb(?![a-z0-9])"
        )
        _zhtw_sub_re = (
            r"([.\[(\s](((zh[-_])?(hk|tw|cht|tc))"
            r"|cht[-_&]?(cht|eng|jap|ja|jpn)"
            r"|eng[-_&]?cht|(jap|ja|jpn)[-_&]?cht"
            r"|繁[体中]?)[.\])\s])"
            r"|繁体中[文字]|中[文字]繁体|繁体|JPTC|tc_jp"
            r"|(?<![a-z0-9])big5(?![a-z0-9])"
        )
        _ja_sub_re = (
            r"([.\[(\s](ja-jp|jap|ja|jpn"
            r"|(jap|ja|jpn)[-_&]?eng|eng[-_&]?(jap|ja|jpn))[.\])\s])"
            r"|日本語|日語"
        )
        _eng_sub_re = r"[.\[(\s]eng[.\])\s]"

        # 原文件后缀
        file_ext = f".{sub_item.extension}"
        # 新文件后缀
        new_file_type = ""

        # 识别字幕语言
        # 先识别繁中，避免“繁体中文/繁中字”等名称被后面的“中文/中字”简中兜底规则误判。
        if re.search(_zhtw_sub_re, sub_item.name, re.I):
            new_file_type = ".zh-tw"
        elif re.search(_zhcn_sub_re, sub_item.name, re.I):
            new_file_type = ".chi.zh-cn"
        elif re.search(_ja_sub_re, sub_item.name, re.I):
            new_file_type = ".ja"
        elif re.search(_eng_sub_re, sub_item.name, re.I):
            new_file_type = ".eng"

        # 添加默认字幕标识
        if (
            (get_runtime_setting('DEFAULT_SUB') == "zh-cn" and new_file_type == ".chi.zh-cn")
            or (get_runtime_setting('DEFAULT_SUB') == "zh-tw" and new_file_type == ".zh-tw")
            or (get_runtime_setting('DEFAULT_SUB') == "ja" and new_file_type == ".ja")
            or (get_runtime_setting('DEFAULT_SUB') == "eng" and new_file_type == ".eng")
        ):
            new_sub_tag = ".default" + new_file_type
        else:
            new_sub_tag = new_file_type

        return new_file.with_name(new_file.stem + new_sub_tag + file_ext)

    def __transfer_file(
        self,
        fileitem: FileItem,
        source_oper: StorageBase,
        target_oper: StorageBase,
        target_storage: str,
        target_file: Path,
        transfer_type: str,
        result: TransferInfo,
        over_flag: Optional[bool] = False,
    ) -> Tuple[Optional[FileItem], str]:
        """
        整理一个文件，同时处理其他相关文件
        :param fileitem: 原文件
        :param source_oper: 源存储操作对象
        :param target_oper: 目标存储操作对象
        :param target_storage: 目标存储
        :param target_file: 新文件
        :param transfer_type: 整理方式
        :param over_flag: 是否覆盖，为True时会先删除再整理
        :param source_oper: 源存储操作对象
        :param target_oper: 目标存储操作对象
        """
        logger.info(
            f"正在整理文件：【{fileitem.storage}】{fileitem.path} 到 【{target_storage}】{target_file}，"
            f"操作类型：{transfer_type}"
        )
        if target_storage == "local" and (
            target_file.exists() or target_file.is_symlink()
        ):
            if not over_flag:
                logger.warn(f"文件已存在：{target_file}")
                return None, f"{target_file} 已存在"
            else:
                logger.info(f"正在删除已存在的文件：{target_file}")
                target_file.unlink()
        else:
            exists_item = target_oper.get_item(target_file)
            if exists_item:
                if not over_flag:
                    logger.warn(f"文件已存在：【{target_storage}】{target_file}")
                    return None, f"【{target_storage}】{target_file} 已存在"
                else:
                    logger.info(
                        f"正在删除已存在的文件：【{target_storage}】{target_file}"
                    )
                    target_oper.delete(exists_item)
        # 执行文件整理命令
        new_item, errmsg = self.__transfer_command(
            fileitem=fileitem,
            target_storage=target_storage,
            source_oper=source_oper,
            target_oper=target_oper,
            target_file=target_file,
            transfer_type=transfer_type,
        )
        if new_item:
            self.__update_result(
                result=result,
                file_list=[fileitem.path],
                file_list_new=[new_item.path],
                file_count=1,
                total_size=fileitem.size,
            )
            return new_item, errmsg

        return None, errmsg

    @staticmethod
    def get_dest_path(
        mediainfo: MediaInfo,
        target_path: Path,
        need_type_folder: Optional[bool] = False,
        need_category_folder: Optional[bool] = False,
    ):
        """
        获取目标路径
        """
        if need_type_folder and mediainfo.type:
            target_path = target_path / mediainfo.type.value
        if need_category_folder and mediainfo.category:
            target_path = target_path / mediainfo.category
        return target_path

    @staticmethod
    def get_dest_dir(
        mediainfo: MediaInfo,
        target_dir: TransferDirectoryConf,
        need_type_folder: Optional[bool] = None,
        need_category_folder: Optional[bool] = None,
    ) -> Path:
        """
        根据设置并装媒体库目录
        :param mediainfo: 媒体信息
        :param target_dir: 媒体库根目录
        :param need_type_folder: 是否需要按媒体类型创建目录
        :param need_category_folder: 是否需要按媒体类别创建目录
        """
        if need_type_folder is None:
            need_type_folder = target_dir.library_type_folder
        if need_category_folder is None:
            need_category_folder = target_dir.library_category_folder
        if not target_dir.media_type and need_type_folder and mediainfo.type:
            # 一级自动分类
            library_dir = Path(target_dir.library_path) / mediainfo.type.value
        elif target_dir.media_type and need_type_folder:
            # 一级手动分类
            library_dir = Path(target_dir.library_path) / target_dir.media_type
        else:
            library_dir = Path(target_dir.library_path)
        if (
            not target_dir.media_category
            and need_category_folder
            and mediainfo.category
        ):
            # 二级自动分类
            library_dir = library_dir / mediainfo.category
        elif target_dir.media_category and need_category_folder:
            # 二级手动分类
            library_dir = library_dir / target_dir.media_category

        return library_dir

    @staticmethod
    def get_naming_dict(
        meta: MetaBase,
        mediainfo: MediaInfo,
        file_ext: Optional[str] = None,
        episodes_info: List[TmdbEpisode] = None,
    ) -> dict:
        """
        根据媒体信息，返回Format字典
        :param meta: 文件元数据
        :param mediainfo: 识别的媒体信息
        :param file_ext: 文件扩展名
        :param episodes_info: 当前季的全部集信息
        """
        naming_context = TemplateHelper().builder.build(
            meta=meta,
            mediainfo=mediainfo,
            file_extension=file_ext,
            episodes_info=episodes_info,
        )
        # 重命名格式是独立的用户配置契约，继续只暴露各数据源原有 ID 变量。
        naming_context.pop("media_source", None)
        naming_context.pop("media_id", None)
        return naming_context

    @staticmethod
    def __delete_version_files(storage_oper: StorageBase, path: Path) -> bool:
        """
        删除目录下的所有版本文件
        :param storage_oper: 存储操作对象
        :param path: 目录路径
        """
        # 存储
        if not storage_oper:
            return False
        # 识别文件中的季集信息
        meta = MetaInfoPath(path)
        season = meta.season
        episode = meta.episode
        part = meta.part
        logger.warn(f"正在删除目标目录中其它版本的文件：{path.parent}")
        # 获取父目录
        parent_item = storage_oper.get_item(path.parent)
        if not parent_item:
            logger.warn(f"目录 {path.parent} 不存在")
            return False
        # 检索媒体文件
        media_files = storage_oper.list(parent_item)
        if not media_files:
            logger.info(f"目录 {path.parent} 中没有文件")
            return False
        # 删除文件
        for media_file in media_files:
            media_path = Path(media_file.path)
            if media_path == path:
                continue
            if media_file.type != "file":
                continue
            # 当前只有视频文件需要保留最新版本，其余格式无需处理，以避免误删 (issue 5449)
            if f".{media_file.extension.lower()}" not in get_runtime_setting('RMT_MEDIAEXT'):
                continue
            # 识别文件中的季集信息
            filemeta = MetaInfoPath(media_path)
            # 相同季集的文件才删除
            if filemeta.season != season or filemeta.episode != episode:
                continue
            # 相同 Part 的文件才删除，避免误删多 Part 文件 (issue #5862)
            if part and filemeta.part and filemeta.part != part:
                continue
            logger.info(f"正在删除文件：{media_file.name}")
            storage_oper.delete(media_file)
        return True

    @staticmethod
    def get_rename_path(
        template_string: str,
        rename_dict: dict,
        path: Optional[Path] = None,
        source_path: Optional[str] = None,
        source_item: Optional[FileItem] = None,
    ) -> Path:
        """
        生成重命名后的完整路径，支持智能重命名事件
        :param template_string: Jinja2 模板字符串
        :param rename_dict: 渲染上下文，用于替换模板中的变量
        :param path: 可选的基础路径，如果提供，将在其基础上拼接生成的路径
        :param source_path: 源文件路径，即待整理的文件路径
        :param source_item: 源文件信息，即待整理的文件信息
        :return: 生成的完整路径
        """
        # 渲染前先发事件，让插件有机会往 rename_dict 写字段
        build_event_data = TransferRenameBuildEventData(
            template_string=template_string,
            rename_dict=rename_dict,
            source_path=source_path,
            source_item=source_item,
        )
        build_event = eventmanager.send_event(
            ChainEventType.TransferRenameBuild, build_event_data
        )
        if build_event and build_event.event_data:
            rename_dict = build_event.event_data.rename_dict

        # 创建jinja2模板对象
        template = Template(template_string)
        # 渲染生成的字符串
        render_str = template.render(rename_dict)

        logger.debug(f"Initial render string: {render_str}")
        # 发送智能重命名事件
        event_data = TransferRenameEventData(
            template_string=template_string,
            rename_dict=rename_dict,
            render_str=render_str,
            path=path,
            source_path=source_path,
            source_item=source_item,
        )
        event = eventmanager.send_event(ChainEventType.TransferRename, event_data)
        # 检查事件返回的结果
        if event and event.event_data:
            event_data: TransferRenameEventData = event.event_data
            if event_data.updated and event_data.updated_str:
                logger.debug(
                    f"Render string updated by event: "
                    f"{render_str} -> {event_data.updated_str} (source: {event_data.source})"
                )
                render_str = event_data.updated_str

        # 目的路径
        if path:
            return path / render_str
        else:
            return Path(render_str)
