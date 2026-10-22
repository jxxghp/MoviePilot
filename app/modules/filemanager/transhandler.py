"""
媒体文件整理的只读规划、可恢复执行、安全覆盖与命名策略。
"""

import filecmp
import re
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable, List, Optional, Tuple, Union, cast

from jinja2 import Template

from app.adapters.system.host import SystemUtils
from app.application.audio import AudioMetadataHelper
from app.application.classification.reference import (
    append_classification_category_path,
    category_path_below_media_type,
    classification_media_type,
    ensure_path_within_root,
)
from app.application.directory import DirectoryHelper
from app.application.messaging.message import TemplateHelper
from app.application.transfer.execution import (
    TransferExecutionConflictError,
    TransferOperationObservation,
    TransferOperationObservationState,
    TransferPlanningRejectedError,
    TransferStepResult,
    TransferStepRunner,
    build_transfer_overwrite_paths,
)
from app.application.transfer.workflow import (
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
from app.schemas.file import FileItem
from app.schemas.system import TransferDirectoryConf
from app.schemas.tmdb import TmdbEpisode
from app.schemas.transfer import TransferInfo
from app.schemas.types import ChainEventType, MediaType


class TransHandler:
    """
    规划媒体整理路径，并执行可恢复的文件转移和覆盖操作。

    规划冻结路径、元数据和叶子操作顺序；执行阶段处理覆盖规则、插件裁决和存储副作用。
    注入步骤执行器时记录操作证据并保守恢复，未注入时沿用同步调用方式。
    """

    def __init__(self) -> None:
        """
        初始化无状态整理执行器。
        """
        pass

    @staticmethod
    def __normalize_disc_folder_name(value: Optional[str]) -> Optional[str]:
        """
        从 Disc、Disk、DVD 或 CD 标识中提取盘号并规范名称。

        :param value: 待识别的分片或目录名；可为空
        :return: Disc N 格式的盘片名；无匹配时返回 None
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
        为电视剧原盘保留季目录并追加盘片子目录。

        :param rendered_path: 模板渲染的单集路径，父目录作为季目录
        :param source_item: 原盘源目录，名称用于补充盘号
        :param meta: 源元数据，优先使用 part 中的盘号
        :return: 季目录下的盘片目录；无法提取盘号时沿用源目录名或使用 Disc
        """
        disc_folder = cls.__normalize_disc_folder_name(getattr(meta, "part", None))
        if not disc_folder and source_item:
            source_name = source_item.name or (Path(source_item.path).name if source_item.path else "")
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
    def __update_result(result: TransferInfo, **kwargs: Any) -> None:
        """
        按字段类型合并整理结果，原地修改结果对象。

        忽略不存在的属性；布尔值直接替换，不按整数累加。

        :param result: 累计整理结果
        :param kwargs: 待合并字段；列表追加、字典更新、整数累加，其他值覆盖
        """
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
        构造预览结果文件项，不访问真实存储。

        :param storage: 目标存储标识
        :param path: 计划中的目标路径
        :param item_type: file 表示文件，dir 表示目录
        :param size: 文件字节数；目录忽略该值
        :return: 根据路径推导名称和扩展名的预览文件项
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
        """
        比较新旧音乐的音质分数，证据不足时交回原覆盖策略。

        :param meta: 源文件元数据，优先从中读取音质分数
        :param mediainfo: 媒体类型和备用音质信息
        :param target_item: 已有目标文件；本地文件读取真实音频标签，远端按格式估分
        :return: 源音质更好时为 True，更差时为 False；不适用、缺分或同分时为 None
        """
        if getattr(mediainfo, "type", None) != MediaType.MUSIC or not target_item:
            return None
        if Path(str(target_item.path or "")).suffix.casefold() in {".jpg", ".jpeg", ".png", ".webp"}:
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
        """
        将文件项序列化为可持久化的独立快照。

        :param fileitem: 待冻结的源或目标文件项
        :return: 采用 JSON 值类型的字典
        """
        return fileitem.model_dump(mode="json")

    @staticmethod
    def __serialize_transfer_model(value: object) -> dict[str, Any]:
        """
        校验领域对象可序列化为整理检查点使用的字典。

        :raises TypeError: 对象不支持序列化或返回值不是字典
        :param value: 提供 to_dict 方法的领域对象
        :return: 对象序列化的字典
        """
        serializer = getattr(value, "to_dict", None)
        if not callable(serializer):
            raise TypeError(f"{type(value).__name__} 不支持整理快照序列化")
        payload = serializer()
        if not isinstance(payload, dict):
            raise TypeError(f"{type(value).__name__} 返回了无效整理快照")
        return payload

    @staticmethod
    def __is_subtitle_file(fileitem: FileItem) -> bool:
        """
        按运行配置检查文件是否为字幕附件。

        :param fileitem: 带扩展名的文件项
        :return: 扩展名属于字幕配置时为 True
        """
        return bool(
            fileitem.extension
            and f".{fileitem.extension.lower()}" in get_runtime_setting("RMT_SUBEXT")
        )

    @staticmethod
    def __is_extra_file(fileitem: FileItem, mediainfo: MediaInfo | MusicInfo) -> bool:
        """
        识别不参与主媒体质量比较的附属文件。

        :param fileitem: 待判断的文件项
        :param mediainfo: 决定音频或歌词附件规则的主媒体信息
        :return: 字幕、音乐歌词或影视外置音轨时为 True
        """
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
        """
        识别缺少季集号时允许跳过的特典视频名称。

        :param fileitem: 提供文件名的待整理项
        :return: 名称匹配片头、片尾、预告、花絮等标记时为 True
        """
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
        """
        只读遍历源目录并冻结叶子文件的执行顺序。

        不冻结空目录，也不创建目标目录。

        :param fileitem: 待整理的源目录
        :param target_storage: 叶子操作的目标存储
        :param target_path: 对应源目录的目标根路径
        :param source_oper: 负责列举源目录的存储适配器
        :return: 按存储返回顺序深度优先排列的叶子操作元组
        """
        items: list[TransferPlanItem] = []

        def collect(source_dir: FileItem, destination_dir: Path) -> None:
            """
            递归收集当前目录叶子文件，追加到外层计划列表。

            :param source_dir: 当前源目录
            :param destination_dir: 对应的目标目录路径
            """
            for source_item in source_oper.list(source_dir) or []:
                if not source_item.path or not source_item.name:
                    raise ValueError("源文件项缺少路径或名称")
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
        """
        冻结整理目标和有序操作，不执行宿主文件或目录写入。

        命名过程会发送插件事件，插件行为不在此处隔离；特典缺少集号时生成合法跳过计划。

        :raises TransferPlanningRejectedError: 普通剧集缺少集号
        :raises ValueError: 命名模板或目标路径无效
        :param planning_input: 请求接纳阶段冻结的源文件和请求参数
        :param meta: 源元数据；内部复制后归一化季集，不修改调用方对象
        :param mediainfo: 已识别媒体信息，用于命名和分类
        :param source_oper: 源存储适配器；非预览目录规划会只读列举子项
        :param target_storage: 已经解析的目标存储标识
        :param target_path: 已经解析的媒体库根路径
        :param transfer_type: 已经解析的整理方式
        :param need_scrape: 执行结果是否请求后续刮削
        :param need_rename: 是否根据模板生成目标名称
        :param need_notify: 执行结果是否允许后续通知
        :param overwrite_mode: 执行期采用的覆盖策略；可为空
        :param episodes_info: 当前季的集详情，用于命名；可为空
        :param preview: 是否仅预览；目录预览不遍历并冻结叶子操作
        :return: 包含最终路径、元数据快照和操作清单的检查点
        """
        fileitem = FileItem.model_validate(planning_input.source_fileitem)
        final_target: Optional[Path]
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
                final_target = target_path / cast(str, fileitem.name)
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
                raise TransferPlanningRejectedError("未识别到文件集数")
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
            if (mediainfo.type == MediaType.MUSIC
                    and Path(str(fileitem.path)).suffix.casefold() in {".jpg", ".jpeg", ".png", ".webp"}):
                # 封面保持原名并放到专辑根，不套用曲序/曲名模板或进入 Disc 子目录。
                final_target = target_directory_path / Path(str(fileitem.path)).name
            if getattr(planning_meta, "music_layout", None) == "image_cue":
                # 整轨与索引只调整专辑目录，保留原名才能让 CUE FILE 引用继续有效。
                final_target = final_target.parent / Path(str(fileitem.path)).name
        else:
            final_target = target_path / cast(str, fileitem.name)

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
        """
        在宿主写入前发送整理拦截事件并读取插件裁决。

        :param fileitem: 源文件或目录项
        :param meta: 源元数据；目录事件不传入该字段
        :param mediainfo: 已识别媒体信息
        :param target_storage: 目标存储标识
        :param target_path: 冻结的目标路径
        :param transfer_type: 整理方式
        :param over_flag: 是否覆盖；None 表示目录事件，不携带覆盖选项
        :return: 是否允许执行及取消原因；未取消时原因为空字符串
        """
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
        """
        在执行期读取目标状态、插件裁决和覆盖策略。

        未知目标状态返回失败结果；明确策略拒绝标记为覆盖跳过，以保留已有成功历史。
        字幕及其他附件优先覆盖；普通文件允许插件先作裁决，再按配置和音乐音质或大小判断。

        :param fileitem: 源文件项，提供大小及附件判断信息
        :param meta: 源元数据，提供音乐音质信息
        :param mediainfo: 已识别媒体信息
        :param target_oper: 用于严格查询目标的存储适配器
        :param target_storage: 目标存储标识
        :param target_file: 已冻结的目标文件路径
        :param transfer_type: 整理方式，传入插件并写入失败结果
        :param overwrite_mode: always、latest、never 或 size；未知或空值不主动覆盖
        :param need_notify: 失败结果是否请求通知
        :return: 是否覆盖、是否清理其他版本、可选失败结果
        """
        is_extra_file = self.__is_extra_file(fileitem, mediainfo)
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
                fail_list=[fileitem.path] if fileitem.path else [],
                transfer_type=transfer_type,
                need_notify=need_notify,
            )
        if is_extra_file:
            return target_item is not None, False, None
        if not target_item:
            return False, overwrite_mode == "latest" and target_file.suffix.casefold() not in {
                ".jpg", ".jpeg", ".png", ".webp"
            }, None

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
                fail_list=[fileitem.path] if fileitem.path else [],
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
                fail_list=[fileitem.path] if fileitem.path else [],
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
                    fail_list=[fileitem.path] if fileitem.path else [],
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
            sizes_known = source_size is not None and target_size is not None
            if sizes_known and cast(int, target_size) < cast(int, source_size):
                return True, False, None
            return False, False, TransferInfo(
                success=False,
                message=(
                    "媒体库存在同名文件，且质量更好"
                    if sizes_known else "无法确认源文件或目标文件大小，已跳过覆盖"
                ),
                fileitem=fileitem,
                target_item=target_item,
                fail_list=[fileitem.path] if fileitem.path else [],
                transfer_type=transfer_type,
                need_notify=need_notify,
                overwrite_skipped=sizes_known,
            )
        return False, False, None

    @staticmethod
    def __serialize_step_item(fileitem: Optional[FileItem]) -> Optional[dict[str, Any]]:
        """
        将步骤中的可选文件项投影为 JSON 字典。

        :param fileitem: 待记录的文件项；可为空
        :return: 文件项字典；输入为空时返回 None
        """
        return fileitem.model_dump(mode="json") if fileitem else None

    @staticmethod
    def __restore_step_item(result: TransferStepResult) -> Optional[FileItem]:
        """
        从已持久化的成功证据恢复目标文件，不重新访问存储。

        :param result: 含 item 结果字段的步骤证据
        :return: 恢复的文件项；没有有效字典字段时返回 None
        """
        payload = result.payload.get("item")
        return FileItem.model_validate(payload) if isinstance(payload, dict) else None

    @staticmethod
    def __assert_file_identity(expected: FileItem, current: Optional[FileItem]) -> None:
        """
        清理前比对冻结文件的大小、类型及可用身份；证据不足时保留文件。

        本地链接的查询沿用 stat 语义，硬链接和软链接均比较实际内容的身份。
        本地必须核对修改时间，优先纳秒证据；同路径还核对状态时间，防止等长改写后还原 mtime。
        rename 合法改变状态时间，因此跨路径只比较修改时间。旧本地快照回退到秒级修改时间，
        缺少时间凭据时转人工复核。云盘修改时间可能在上传后变化或缺失，不参与校验。

        :param expected: 冻结的文件身份，必须包含大小
        :param current: 当前严格查询结果；不可见时视为冲突
        :raises TransferExecutionConflictError: 文件消失、身份字段变化或类型不一致
        :raises StorageQueryError: 缺少必要身份凭据
        """
        if current is None:
            raise TransferExecutionConflictError(f"文件 {expected.path} 已不可见，保留源文件和旧备份")
        if expected.size is None or current.size is None:
            raise StorageQueryError(f"文件 {expected.path} 缺少 size 身份凭据，需人工复核")
        if expected.size != current.size:
            raise TransferExecutionConflictError(f"文件 {expected.path} 的 size 已变化，禁止清理")
        if expected.type != current.type:
            raise TransferExecutionConflictError(f"文件 {expected.path} 的类型已变化，禁止清理")
        if expected.fileid:
            if not current.fileid:
                raise StorageQueryError(f"文件 {expected.path} 缺少 fileid 身份凭据，需人工复核")
            if expected.fileid != current.fileid:
                raise TransferExecutionConflictError(f"文件 {expected.path} 已被替换，禁止清理")
        if expected.storage == "local":
            expected_time: Optional[int | float]
            current_time: Optional[int | float]
            if expected.modify_time_ns is not None:
                expected_time, current_time = expected.modify_time_ns, current.modify_time_ns
            else:
                expected_time, current_time = expected.modify_time, current.modify_time
            if expected_time is None or current_time is None:
                raise StorageQueryError(f"文件 {expected.path} 缺少修改时间身份凭据，需人工复核")
            if expected_time != current_time:
                raise TransferExecutionConflictError(f"文件 {expected.path} 的修改时间已变化，禁止清理")
            if expected.path == current.path and expected.change_time_ns is not None:
                if current.change_time_ns is None:
                    raise StorageQueryError(f"文件 {expected.path} 缺少状态时间身份凭据，需人工复核")
                if expected.change_time_ns != current.change_time_ns:
                    raise TransferExecutionConflictError(f"文件 {expected.path} 的状态时间已变化，禁止清理")

    @staticmethod
    def __restore_move_target_identity(source: FileItem, target: FileItem) -> FileItem:
        """
        恢复旧版 115 秒传回执的精确大小，保留原文件 ID 和持久账本不变。

        仅本地文件上传到 115 且回执包含大小和文件 ID 时，使用冻结源的字节数。
        删除前仍须严格核对云端大小、原文件 ID 及本地源身份，不能凭路径接纳替换文件。

        :param source: 上传前冻结的本地源文件
        :param target: 已持久成功的上传回执，旧版本可能记录舍入后的显示大小
        :return: 用于严格核验和结算的目标身份；证据不完整或其它存储沿用原值
        """
        if (source.storage == "local" and target.storage == "u115"
                and source.type == target.type == "file" and target.fileid
                and source.size is not None and target.size is not None):
            return target.model_copy(update={"size": source.size})
        return target

    @classmethod
    def __observe_safe_delete(
            cls, storage_oper: StorageBase, path: Path, *,
            frozen_item: Optional[FileItem], before_delete: Optional[Callable[[], None]],
    ) -> TransferOperationObservation:
        """
        只在删除对象和保留副本的身份都可确认时，允许未执行删除步骤重试。

        :param storage_oper: 负责严格查询待删除路径的适配器
        :param path: 待删除文件路径
        :param frozen_item: 可选的待删除文件冻结身份
        :param before_delete: 删除前核验保留副本的回调；可为空
        :return: 可恢复删除状态和证据；身份冲突或查询未知时拒绝猜测
        """
        try:
            current = storage_oper.get_item_strict(path)
            if current is None:
                state = TransferOperationObservationState.APPLIED
            else:
                if before_delete:
                    before_delete()
                if frozen_item:
                    cls.__assert_file_identity(frozen_item, current)
                state = TransferOperationObservationState.NOT_APPLIED
        except TransferExecutionConflictError as error:
            return TransferOperationObservation(
                state=TransferOperationObservationState.CONFLICT,
                evidence=TransferStepResult(payload={"identity_error": str(error)}),
            )
        except Exception as error:
            return TransferOperationObservation(
                state=TransferOperationObservationState.UNKNOWN,
                evidence=TransferStepResult(payload={"query_error": str(error)}),
            )
        return TransferOperationObservation(
            state=state, evidence=TransferStepResult(payload={"path": path.as_posix(), "deleted": current is None}),
        )

    @staticmethod
    def __observe_item_presence(
            storage_oper: StorageBase,
            path: Path,
            *,
            applied_when_present: bool,
    ) -> TransferOperationObservation:
        """
        用严格查询判断目标是否符合操作完成条件。

        任何查询异常都返回未知状态，不据此推断路径不存在。

        :param storage_oper: 提供严格路径查询的适配器
        :param path: 待观察路径
        :param applied_when_present: 为 True 时存在即完成，为 False 时不存在即完成
        :return: 已完成、未完成或未知的观察结果，并附查询证据
        """
        try:
            item = storage_oper.get_item_strict(path)
        except Exception as error:
            return TransferOperationObservation(
                state=TransferOperationObservationState.UNKNOWN,
                evidence=TransferStepResult(payload={
                    "path": path.as_posix(),
                    "query_error": str(error),
                }),
            )
        exists = item is not None
        applied = exists if applied_when_present else not exists
        return TransferOperationObservation(
            state=(
                TransferOperationObservationState.APPLIED
                if applied
                else TransferOperationObservationState.NOT_APPLIED
            ),
            evidence=TransferStepResult(payload={
                "path": path.as_posix(),
                "exists": exists,
                "item": TransHandler.__serialize_step_item(item),
            }),
        )

    @staticmethod
    def __run_persisted_step(
            step_runner: Optional[TransferStepRunner],
            *,
            phase: str,
            kind: str,
            payload: dict[str, Any],
            execute: Callable[[], TransferStepResult],
            observe: Callable[[], TransferOperationObservation],
    ) -> TransferStepResult:
        """
        委托步骤执行器记录或恢复操作，无执行器时直接调用。

        :param step_runner: 可恢复步骤执行器；None 表示同步调用
        :param phase: 步骤所属阶段，参与稳定操作身份
        :param kind: 阶段中的操作类别
        :param payload: 冻结的步骤意图参数
        :param execute: 实际执行并返回成功证据的回调
        :param observe: 只读观察外部状态的回调，供执行器恢复未回执操作
        :return: 已执行或从账本恢复的步骤结果
        """
        if step_runner is None:
            return execute()
        return step_runner.run(
            phase=phase,
            kind=kind,
            payload=payload,
            execute=execute,
            observe=observe,
        )

    @staticmethod
    def __observe_transfer_operation(
            *,
            fileitem: FileItem,
            target_storage: str,
            source_oper: StorageBase,
            target_oper: StorageBase,
            target_file: Path,
            transfer_type: str,
    ) -> TransferOperationObservation:
        """
        保守观察传输结果，证据不足时禁止自动重放。

        移动根据源与目标的存在性判断；本地复制比较完整内容，链接核对文件身份或链接指向。
        非本地目标已存在时，仅凭存在性不能认定复制已完成。

        :param fileitem: 冻结的源文件项
        :param target_storage: 目标存储标识
        :param source_oper: 源严格查询适配器
        :param target_oper: 目标严格查询适配器
        :param target_file: 已冻结目标文件路径
        :param transfer_type: move、copy、link、softlink 或 reflink
        :return: 完成状态及文件证据；无法证明内容或查询失败时返回未知状态
        """
        if not fileitem.path:
            return TransferOperationObservation(
                state=TransferOperationObservationState.UNKNOWN,
                evidence=TransferStepResult(payload={"reason": "源文件项缺少路径"}),
            )
        try:
            source_item = source_oper.get_item_strict(Path(fileitem.path))
            target_item = target_oper.get_item_strict(target_file)
        except Exception as error:
            return TransferOperationObservation(
                state=TransferOperationObservationState.UNKNOWN,
                evidence=TransferStepResult(payload={"query_error": str(error)}),
            )

        source_exists = source_item is not None
        target_exists = target_item is not None
        evidence = TransferStepResult(payload={
            "source_exists": source_exists,
            "target_exists": target_exists,
            "item": TransHandler.__serialize_step_item(target_item),
        })
        # 115 盘内操作包含复制/移动及改名。最终路径缺失不能排除中间副本已产生，
        # 包括旧版适配器留下的原名或编号副本，禁止据此自动重复复制。
        if fileitem.storage == target_storage == "u115":
            return TransferOperationObservation(
                state=TransferOperationObservationState.UNKNOWN,
                evidence=evidence,
            )
        if transfer_type == "move":
            if not source_exists and target_exists:
                return TransferOperationObservation(
                    state=TransferOperationObservationState.APPLIED,
                    evidence=evidence,
                )
            if source_exists and not target_exists:
                return TransferOperationObservation(
                    state=TransferOperationObservationState.NOT_APPLIED,
                    evidence=evidence,
                )
            return TransferOperationObservation(
                state=TransferOperationObservationState.CONFLICT,
                evidence=evidence,
            )

        if not target_exists:
            return TransferOperationObservation(
                state=TransferOperationObservationState.NOT_APPLIED,
                evidence=evidence,
            )
        if fileitem.storage != "local" or target_storage != "local":
            return TransferOperationObservation(
                state=TransferOperationObservationState.UNKNOWN,
                evidence=evidence,
            )
        source_path = Path(fileitem.path)
        try:
            if transfer_type == "copy":
                applied = source_path.is_file() and filecmp.cmp(
                    source_path, target_file, shallow=False
                )
            elif transfer_type == "link":
                applied = source_path.is_file() and target_file.samefile(source_path)
            elif transfer_type == "softlink":
                applied = target_file.is_symlink() and target_file.resolve() == source_path.resolve()
            elif transfer_type == "reflink":
                applied = (
                    source_path.is_file()
                    and target_file.is_file()
                    and not target_file.is_symlink()
                    and not target_file.samefile(source_path)
                    and filecmp.cmp(source_path, target_file, shallow=False)
                )
            else:
                return TransferOperationObservation(
                    state=TransferOperationObservationState.UNKNOWN,
                    evidence=evidence,
                )
        except OSError as error:
            return TransferOperationObservation(
                state=TransferOperationObservationState.UNKNOWN,
                evidence=TransferStepResult(payload={
                    **evidence.payload,
                    "verification_error": str(error),
                }),
            )
        return TransferOperationObservation(
            state=(
                TransferOperationObservationState.APPLIED
                if applied
                else TransferOperationObservationState.CONFLICT
            ),
            evidence=evidence,
        )

    @classmethod
    def __execute_transfer_with_steps(
            cls,
            *,
            step_runner: Optional[TransferStepRunner],
            source_fileitem: dict[str, Any],
            target_storage: str,
            source_oper: StorageBase,
            target_oper: StorageBase,
            target_file: Path,
            transfer_type: str,
            require_target_absent: bool = False,
            materialize_kind: str = "materialize_target",
    ) -> tuple[Optional[FileItem], str]:
        """
        按冻结叶节点执行传输，并把跨存储 move 拆为落地与源删除。

        意图直接消费原始快照，避免旧快照补默认字段或适配器更新运行期文件信息
        导致步骤身份漂移；每个外部操作单独恢复文件对象。

        :param step_runner: 步骤执行器；None 时同步执行
        :param source_fileitem: 原始冻结源快照，不补写运行期字段
        :param target_storage: 目标存储标识
        :param source_oper: 源存储适配器
        :param target_oper: 目标存储适配器
        :param target_file: 最终或暂存目标路径
        :param transfer_type: 整理方式；跨存储 move 拆为 copy 和独立删除
        :param require_target_absent: 是否要求目标事先不存在并核验落地大小
        :param materialize_kind: 目标落地步骤类别，默认 materialize_target
        :return: 目标文件项及提示；缺少成功目标证据时文件项为 None
        """
        fileitem = FileItem.model_validate(source_fileitem)
        if not fileitem.path:
            return None, "源文件项缺少路径"
        source_path = Path(fileitem.path)
        cross_storage_move = (
            transfer_type == "move" and fileitem.storage != target_storage
        )
        materialize_type = "copy" if cross_storage_move else transfer_type
        intent_payload = {
            "source": source_fileitem,
            "target_storage": target_storage,
            "target_path": target_file.as_posix(),
            "transfer_type": materialize_type,
        }

        def execute_materialize() -> TransferStepResult:
            """
            执行一次目标落地并冻结其返回对象。
            """
            if require_target_absent and target_oper.get_item_strict(target_file) is not None:
                raise RuntimeError(f"暂存目标已存在，需确认上次整理结果：{target_file}")
            new_item, error = cls.__transfer_command(
                fileitem=fileitem,
                target_storage=target_storage,
                source_oper=source_oper,
                target_oper=target_oper,
                target_file=target_file,
                transfer_type=materialize_type,
            )
            if not new_item:
                raise RuntimeError(error or f"{fileitem.path} 整理失败")
            if require_target_absent:
                # SDK 的成功返回不能替代暂存文件的完整性证据，旧目标此时仍未移动。
                staged_item = target_oper.get_item_strict(target_file)
                if (fileitem.size is None or staged_item is None
                        or staged_item.size != fileitem.size):
                    raise TransferExecutionConflictError(f"暂存目标 {target_file} 大小无法确认，保留旧目标")
                new_item = staged_item
            return TransferStepResult(payload={
                "item": cls.__serialize_step_item(new_item),
                "message": error,
            })

        materialized = cls.__run_persisted_step(
            step_runner,
            phase="transfer",
            kind=materialize_kind,
            payload=intent_payload,
            execute=execute_materialize,
            observe=lambda: cls.__observe_transfer_operation(
                fileitem=fileitem,
                target_storage=target_storage,
                source_oper=source_oper,
                target_oper=target_oper,
                target_file=target_file,
                transfer_type=materialize_type,
            ),
        )
        new_item = cls.__restore_step_item(materialized)
        if not new_item:
            return None, "整理步骤成功证据缺少目标文件"
        if not cross_storage_move:
            return new_item, str(materialized.payload.get("message") or "")
        new_item = cls.__restore_move_target_identity(fileitem, new_item)

        def execute_source_delete() -> TransferStepResult:
            """
            在目标已落地后单独删除跨存储 move 的源文件。
            """
            confirm_materialized_target()
            current_source = source_oper.get_item_strict(source_path)
            if current_source is not None:
                cls.__assert_file_identity(fileitem, current_source)
            if current_source is not None and not source_oper.delete(current_source):
                raise RuntimeError(f"{fileitem.path} 源文件删除失败")
            return TransferStepResult(payload={
                "source_path": fileitem.path,
                "deleted": True,
            })

        def confirm_materialized_target() -> None:
            """
            删除跨存储移动源前核对当前目标仍与持久落地结果一致。
            """
            cls.__assert_file_identity(new_item, target_oper.get_item_strict(target_file))

        cls.__run_persisted_step(
            step_runner,
            phase="transfer",
            kind="delete_move_source",
            payload={
                "source": source_fileitem,
                "target_storage": target_storage,
                "target_path": target_file.as_posix(),
            },
            execute=execute_source_delete,
            observe=lambda: cls.__observe_safe_delete(
                source_oper, source_path,
                frozen_item=fileitem, before_delete=confirm_materialized_target,
            ),
        )
        return new_item, str(materialized.payload.get("message") or "")

    @classmethod
    def __rename_with_step(
            cls,
            *,
            step_runner: Optional[TransferStepRunner],
            storage_oper: StorageBase,
            storage: str,
            source: Path,
            target: Path,
            kind: str,
            expected_source: Optional[FileItem] = None,
            allow_missing_source: bool = False,
    ) -> Optional[FileItem]:
        """
        执行同目录无覆盖重命名，并用两端严格查询恢复中断操作。

        两端同时存在或遗留步骤中同时消失视为冲突；只有覆盖备份在执行前明确
        发现旧目标缺失时，才记录为无副作用的跳过。查询或身份无法确认时不自动重放。

        :param step_runner: 步骤执行器；None 时同步执行
        :param storage_oper: 负责查询和重命名的存储适配器
        :param storage: 用于步骤身份和诊断的存储标识
        :param source: 原路径，必须存在
        :param target: 新路径，必须不存在且由调用方保持在同一目录
        :param kind: 步骤操作类别
        :param expected_source: 可选的冻结源身份；提供时在操作前后核对
        :param allow_missing_source: 旧目标已被外部删除时是否将重命名记录为跳过
        :return: 重命名后的文件项；允许旧目标缺失并跳过时返回 None
        """
        def execute() -> TransferStepResult:
            """
            目标必须不存在，避免适配器的 rename 意外替换其它文件。
            """
            source_item = storage_oper.get_item_strict(source)
            target_item = storage_oper.get_item_strict(target)
            if allow_missing_source and source_item is None and target_item is None:
                return TransferStepResult(payload={"item": None, "skipped": True})
            if source_item is None or target_item is not None:
                raise RuntimeError(f"重命名路径状态冲突：{source} -> {target}")
            if expected_source is not None:
                cls.__assert_file_identity(expected_source, source_item)
            if not storage_oper.rename(source_item, target.name):
                raise RuntimeError(f"【{storage}】{source} 重命名失败")
            target_item = storage_oper.get_item_strict(target)
            if target_item is None:
                raise RuntimeError(f"【{storage}】{target} 重命名后不可见")
            if expected_source is not None:
                cls.__assert_file_identity(expected_source, target_item)
            return TransferStepResult(payload={"item": cls.__serialize_step_item(target_item)})

        def observe() -> TransferOperationObservation:
            """
            只接受一端存在的明确结果，两端同时存在或消失均禁止重放。
            """
            try:
                source_item = storage_oper.get_item_strict(source)
                target_item = storage_oper.get_item_strict(target)
                if source_item is None and target_item is not None and expected_source is not None:
                    cls.__assert_file_identity(expected_source, target_item)
            except TransferExecutionConflictError as error:
                return TransferOperationObservation(
                    state=TransferOperationObservationState.CONFLICT,
                    evidence=TransferStepResult(payload={"identity_error": str(error)}),
                )
            except Exception as error:
                return TransferOperationObservation(
                    state=TransferOperationObservationState.UNKNOWN,
                    evidence=TransferStepResult(payload={"query_error": str(error)}),
                )
            if source_item is None and target_item is not None:
                state = TransferOperationObservationState.APPLIED
            elif source_item is not None and target_item is None:
                state = TransferOperationObservationState.NOT_APPLIED
            else:
                state = TransferOperationObservationState.CONFLICT
            return TransferOperationObservation(
                state=state,
                evidence=TransferStepResult(payload={
                    "source_exists": source_item is not None,
                    "target_exists": target_item is not None,
                    "item": cls.__serialize_step_item(target_item),
                }),
            )

        renamed = cls.__run_persisted_step(
            step_runner, phase="transfer", kind=kind,
            payload={"storage": storage, "source_path": source.as_posix(),
                     "target_path": target.as_posix()},
            execute=execute, observe=observe,
        )
        item = cls.__restore_step_item(renamed)
        if item is None and not (
                allow_missing_source and renamed.payload.get("skipped") is True
        ):
            raise RuntimeError("重命名成功证据缺少目标文件")
        return item

    @classmethod
    def __replace_target_with_steps(
            cls,
            *,
            step_runner: Optional[TransferStepRunner],
            source_fileitem: dict[str, Any],
            target_storage: str,
            source_oper: StorageBase,
            target_oper: StorageBase,
            target_file: Path,
            transfer_type: str,
    ) -> tuple[Optional[FileItem], str]:
        """
        先完整暂存新版本，再备份旧版本、提升新文件，最后清理备份和移动源。

        临时名由冻结输入确定，崩溃恢复会使用相同操作身份。备份和提升各自记账，
        查询失败或远端结果不明时交给 步骤执行器 隔离，绝不猜测完成或删除备份。

        :param step_runner: 步骤执行器；None 时同步执行
        :param source_fileitem: 用于临时名称及步骤身份的冻结源快照
        :param target_storage: 目标存储标识
        :param source_oper: 源存储适配器
        :param target_oper: 目标存储适配器
        :param target_file: 需要替换的旧目标路径
        :param transfer_type: 整理方式；move 在新目标提升成功前保留源文件
        :return: 新目标文件项及提示；源目标相同或落地失败时文件项为 None
        """
        if (source_fileitem.get("storage") == target_storage
                and Path(source_fileitem["path"]) == target_file):
            return None, "源文件和目标文件相同，已跳过覆盖以保留原文件"
        stage_path, backup_path = build_transfer_overwrite_paths(
            source=source_fileitem, target_storage=target_storage,
            target_path=target_file.as_posix(), transfer_type=transfer_type,
        )
        stage, backup = Path(stage_path), Path(backup_path)
        # move 也先 copy，提升成功前始终保留源文件。
        new_item, message = cls.__execute_transfer_with_steps(
            step_runner=step_runner, source_fileitem=source_fileitem,
            target_storage=target_storage, source_oper=source_oper,
            target_oper=target_oper, target_file=stage,
            transfer_type="copy" if transfer_type == "move" else transfer_type,
            require_target_absent=True,
            materialize_kind="stage_overwrite_target",
        )
        if new_item is None:
            return None, message
        backup_item = cls.__rename_with_step(
            step_runner=step_runner, storage_oper=target_oper,
            storage=target_storage, source=target_file, target=backup,
            kind="backup_overwrite_target",
            allow_missing_source=True,
        )
        new_item = cast(FileItem, cls.__rename_with_step(
            step_runner=step_runner, storage_oper=target_oper,
            storage=target_storage, source=stage, target=target_file,
            kind="promote_overwrite_target", expected_source=new_item,
        ))

        def confirm_replacement() -> None:
            """
            清理前核对新目标的持久提升凭据，不能仅凭路径存在删除其它副本。
            """
            cls.__assert_file_identity(new_item, target_oper.get_item_strict(target_file))

        if transfer_type == "move":
            cls.__delete_target_with_step(
                step_runner=step_runner, target_oper=source_oper,
                target_storage=str(source_fileitem["storage"]),
                target_file=Path(source_fileitem["path"]), kind="delete_overwrite_source",
                before_delete=confirm_replacement, frozen_item=FileItem(**source_fileitem),
            )
        cls.__delete_target_with_step(
            step_runner=step_runner, target_oper=target_oper,
            target_storage=target_storage, target_file=backup,
            kind="delete_overwrite_backup",
            before_delete=confirm_replacement, frozen_item=backup_item,
        )
        return new_item, message

    @classmethod
    def __ensure_directory_with_step(
            cls,
            *,
            step_runner: Optional[TransferStepRunner],
            target_oper: StorageBase,
            target_storage: str,
            path: Path,
    ) -> Optional[FileItem]:
        """
        记录可能创建目录的操作并恢复其结果。

        实际取得目录失败时抛出 RuntimeError。

        :param step_runner: 步骤执行器；None 时同步执行
        :param target_oper: 提供 get_folder 和严格查询的目标适配器
        :param target_storage: 目标存储标识
        :param path: 需要存在的目录路径
        :return: 恢复的目录项；成功证据缺少文件项时返回 None
        """
        def execute() -> TransferStepResult:
            """
            获取或创建目标目录并冻结目录对象。
            """
            directory = target_oper.get_folder(path)
            if not directory:
                raise RuntimeError(f"目标目录 {path} 获取失败")
            return TransferStepResult(payload={
                "item": cls.__serialize_step_item(directory),
            })

        result = cls.__run_persisted_step(
            step_runner,
            phase="prepare",
            kind="ensure_target_directory",
            payload={"storage": target_storage, "path": path.as_posix()},
            execute=execute,
            observe=lambda: cls.__observe_item_presence(
                target_oper,
                path,
                applied_when_present=True,
            ),
        )
        return cls.__restore_step_item(result)

    @classmethod
    def __cleanup_with_step(
            cls,
            *,
            step_runner: Optional[TransferStepRunner],
            cleanup: Optional[Callable[[], None]],
            observe_cleanup: Optional[Callable[[], bool]],
            source_path: str,
    ) -> None:
        """
        把历史目标清理纳入步骤账本。

        只有清理回调正常返回才记录成功；缺少观察能力时不能自动猜测遗留调用是否完成。

        :param step_runner: 步骤执行器；None 时直接调用清理回调
        :param cleanup: 实际清理回调；为空时跳过
        :param observe_cleanup: 只读确认清理结果的回调；缺失时遗留结果视为未知
        :param source_path: 用于稳定步骤身份的源路径
        """
        if cleanup is None:
            return

        def execute() -> TransferStepResult:
            """
            执行统一旧目标清理能力。
            """
            cleanup()
            return TransferStepResult(payload={"cleaned": True})

        def observe() -> TransferOperationObservation:
            """
            通过只读兼容能力确认旧目标是否已经消失。
            """
            if observe_cleanup is None:
                return TransferOperationObservation(
                    state=TransferOperationObservationState.UNKNOWN,
                    evidence=TransferStepResult(payload={
                        "reason": "cleanup observer unavailable",
                    }),
                )
            try:
                cleaned = observe_cleanup()
            except Exception as error:
                return TransferOperationObservation(
                    state=TransferOperationObservationState.UNKNOWN,
                    evidence=TransferStepResult(payload={"query_error": str(error)}),
                )
            return TransferOperationObservation(
                state=(
                    TransferOperationObservationState.APPLIED
                    if cleaned
                    else TransferOperationObservationState.NOT_APPLIED
                ),
                evidence=TransferStepResult(payload={"cleaned": cleaned}),
            )

        cls.__run_persisted_step(
            step_runner,
            phase="prepare",
            kind="cleanup_previous_destination",
            payload={"source_path": source_path},
            execute=execute,
            observe=observe,
        )

    @classmethod
    def __delete_target_with_step(
            cls,
            *,
            step_runner: Optional[TransferStepRunner],
            target_oper: StorageBase,
            target_storage: str,
            target_file: Path,
            kind: str,
            before_delete: Optional[Callable[[], None]] = None,
            frozen_item: Optional[FileItem] = None,
    ) -> None:
        """
        在身份核验通过后删除目标，并记录可恢复的清理证据。

        目标已经不存在视为成功；查询未知、身份冲突或删除失败时向上传播异常。

        :param step_runner: 步骤执行器；None 时同步执行
        :param target_oper: 负责严格查询和删除的适配器
        :param target_storage: 待删除项所在存储标识
        :param target_file: 待删除项的冻结路径
        :param kind: 清理操作类别
        :param before_delete: 删除前核对保留副本的回调；为空时不作此项核对
        :param frozen_item: 可选的待删除文件身份；提供时必须与当前文件一致
        """
        def execute() -> TransferStepResult:
            """
            只删除当前冻结目标，目标已不存在视为成功。
            """
            if before_delete:
                before_delete()
            current = target_oper.get_item_strict(target_file)
            if current is not None and frozen_item is not None:
                cls.__assert_file_identity(frozen_item, current)
            if current is not None and not target_oper.delete(current):
                raise RuntimeError(f"【{target_storage}】{target_file} 删除失败")
            return TransferStepResult(payload={"deleted": True})

        cls.__run_persisted_step(
            step_runner,
            phase="cleanup",
            kind=kind,
            payload={"storage": target_storage, "path": target_file.as_posix()},
            execute=execute,
            observe=lambda: cls.__observe_safe_delete(
                target_oper, target_file, frozen_item=frozen_item, before_delete=before_delete,
            ),
        )

    def __resolve_leaf_overwrite_with_step(
            self,
            *,
            step_runner: Optional[TransferStepRunner],
            planned_item: TransferPlanItem,
            target_oper: StorageBase,
            transfer_type: str,
            meta: MetaBase,
            mediainfo: MediaInfo | MusicInfo,
            overwrite_mode: Optional[str],
            need_notify: bool,
    ) -> tuple[bool, Optional[TransferInfo]]:
        """
        按冻结覆盖策略裁决目录叶目标，保持既有叶子步骤的稳定身份。

        :param step_runner: 步骤执行器；None 时直接查询
        :param planned_item: 含冻结源快照和目标路径的叶子操作
        :param target_oper: 负责严格查询的目标适配器
        :param transfer_type: 整理方式，参与步骤身份
        :param meta: 目录媒体元数据
        :param mediainfo: 已识别媒体信息
        :param overwrite_mode: 检查点冻结的覆盖策略
        :param need_notify: 拒绝覆盖时是否请求通知
        :return: 是否覆盖及可选的拒绝结果
        """
        def execute() -> TransferStepResult:
            """
            目录叶与单文件共用覆盖合同，并冻结拒绝原因。
            """
            over_flag, _, failure = self.__resolve_overwrite(
                fileitem=FileItem.model_validate(planned_item.source_fileitem),
                meta=meta, mediainfo=mediainfo, target_oper=target_oper,
                target_storage=planned_item.target_storage, target_file=Path(planned_item.target_path),
                transfer_type=transfer_type, overwrite_mode=overwrite_mode, need_notify=need_notify,
            )
            return TransferStepResult(payload={
                "over_flag": over_flag, "policy_checked": True,
                "failure": failure.model_dump(mode="json") if failure else None,
            })

        decision = self.__run_persisted_step(
            step_runner, phase="decision", kind="resolve_leaf_overwrite",
            payload={"source": planned_item.source_fileitem,
                     "target_storage": planned_item.target_storage,
                     "target_path": planned_item.target_path, "transfer_type": transfer_type},
            execute=execute,
            observe=lambda: TransferOperationObservation(
                state=TransferOperationObservationState.NOT_APPLIED,
                evidence=TransferStepResult(payload={"reason": "read-only leaf decision may be repeated"}),
            ),
        )
        failure = decision.payload.get("failure")
        if (decision.payload.get("over_flag") is True and overwrite_mode == "never"
                and decision.payload.get("policy_checked") is not True):
            # 旧版本只记录存在性，不能用旧回执授权本次禁止的破坏性覆盖。
            return False, TransferInfo(
                success=False, overwrite_skipped=True,
                message="旧目录覆盖裁决未校验不覆盖策略，保留源文件和目标，需人工复核",
                fileitem=FileItem.model_validate(planned_item.source_fileitem),
                transfer_type=transfer_type, need_notify=need_notify,
            )
        return decision.payload.get("over_flag") is True, (
            TransferInfo.model_validate(failure) if isinstance(failure, dict) else None
        )

    def __resolve_overwrite_with_step(
            self,
            *,
            step_runner: Optional[TransferStepRunner],
            fileitem: FileItem,
            source_fileitem: dict[str, Any],
            meta: MetaBase,
            mediainfo: MediaInfo | MusicInfo,
            target_oper: StorageBase,
            target_storage: str,
            target_file: Path,
            transfer_type: str,
            overwrite_mode: Optional[str],
            need_notify: bool,
    ) -> tuple[bool, bool, Optional[TransferInfo]]:
        """
        以原始源快照冻结覆盖判定，避免模型补字段改变旧任务的步骤身份。

        :param step_runner: 步骤执行器；None 时直接判断
        :param fileitem: 当前源文件投影
        :param source_fileitem: 原始冻结源快照，参与稳定步骤身份
        :param meta: 源文件元数据
        :param mediainfo: 识别后的媒体信息
        :param target_oper: 目标严格查询适配器
        :param target_storage: 目标存储标识
        :param target_file: 冻结目标文件路径
        :param transfer_type: 整理方式
        :param overwrite_mode: 已冻结的覆盖策略；可为空
        :param need_notify: 失败结果是否请求通知
        :return: 是否覆盖、是否删除其他版本、可选失败结果
        """
        def execute() -> TransferStepResult:
            """
            执行一次覆盖策略判定并冻结完整裁决。
            """
            over_flag, delete_versions, failure = self.__resolve_overwrite(
                fileitem=fileitem,
                meta=meta,
                mediainfo=mediainfo,
                target_oper=target_oper,
                target_storage=target_storage,
                target_file=target_file,
                transfer_type=transfer_type,
                overwrite_mode=overwrite_mode,
                need_notify=need_notify,
            )
            return TransferStepResult(payload={
                "over_flag": over_flag,
                "delete_versions": delete_versions,
                "failure": failure.model_dump(mode="json") if failure else None,
            })

        result = self.__run_persisted_step(
            step_runner,
            phase="decision",
            kind="resolve_overwrite",
            payload={
                "source": source_fileitem,
                "target_storage": target_storage,
                "target_path": target_file.as_posix(),
                "transfer_type": transfer_type,
                "overwrite_mode": overwrite_mode,
                "need_notify": need_notify,
            },
            execute=execute,
            observe=lambda: TransferOperationObservation(
                state=TransferOperationObservationState.NOT_APPLIED,
                evidence=TransferStepResult(payload={
                    "reason": "read-only overwrite decision may be repeated",
                }),
            ),
        )
        failure_payload = result.payload.get("failure")
        return (
            bool(result.payload.get("over_flag")),
            bool(result.payload.get("delete_versions")),
            (
                TransferInfo.model_validate(failure_payload)
                if isinstance(failure_payload, dict)
                else None
            ),
        )

    def __intercept_with_step(
            self,
            *,
            step_runner: Optional[TransferStepRunner],
            payload: dict[str, Any],
            invoke: Callable[[], tuple[bool, str]],
    ) -> tuple[bool, str]:
        """
        冻结插件拦截裁决，将没有回执的遗留调用视为未知。

        插件可能有副作用，没有稳定回执的遗留调用不自动重试。

        :param step_runner: 步骤执行器；None 时直接调用插件
        :param payload: 稳定的插件调用意图
        :param invoke: 返回是否允许和拒绝原因的插件调用回调
        :return: 是否允许执行及取消原因
        """
        def execute() -> TransferStepResult:
            """
            执行插件拦截并冻结允许标记与原因。
            """
            allowed, reason = invoke()
            return TransferStepResult(payload={
                "allowed": allowed,
                "reason": reason,
            })

        result = self.__run_persisted_step(
            step_runner,
            phase="decision",
            kind="plugin_transfer_intercept",
            payload=payload,
            execute=execute,
            observe=lambda: TransferOperationObservation(
                state=TransferOperationObservationState.UNKNOWN,
                evidence=TransferStepResult(payload={
                    "reason": "plugin intercept has no stable invocation receipt",
                }),
            ),
        )
        return bool(result.payload.get("allowed")), str(result.payload.get("reason") or "")

    def __execute_directory_leaf(
            self, *, planned_item: TransferPlanItem, checkpoint: TransferPlanCheckpoint,
            meta: MetaBase, mediainfo: MediaInfo | MusicInfo,
            source_oper: StorageBase, target_oper: StorageBase,
            step_runner: Optional[TransferStepRunner],
    ) -> tuple[Optional[FileItem], Optional[TransferInfo]]:
        """
        校验目录叶操作并消费覆盖裁决，拒绝时不执行文件副作用。

        :param planned_item: 冻结的目录叶整理操作
        :param checkpoint: 目录计划及其覆盖、通知策略
        :param meta: 目录媒体元数据
        :param mediainfo: 目录媒体识别结果
        :param source_oper: 源存储适配器
        :param target_oper: 目标存储适配器
        :param step_runner: 持久步骤执行器；为空时同步执行
        :return: 新目标及可选失败结果；拒绝覆盖保留 overwrite_skipped 语义
        """
        transfer_type = checkpoint.resolved_transfer_type
        if planned_item.action != "transfer" or planned_item.target_storage != checkpoint.target_storage:
            return None, TransferInfo(
                success=False, message="整理计划包含不支持的操作或目标存储",
                fileitem=FileItem.model_validate(checkpoint.planning_input.source_fileitem),
                transfer_type=transfer_type, need_notify=checkpoint.need_notify,
            )
        over_flag, failure = self.__resolve_leaf_overwrite_with_step(
            step_runner=step_runner, planned_item=planned_item,
            target_oper=target_oper, transfer_type=transfer_type,
            meta=meta, mediainfo=mediainfo, overwrite_mode=checkpoint.overwrite_mode,
            need_notify=checkpoint.need_notify,
        )
        if failure:
            return None, failure
        execute_transfer = self.__replace_target_with_steps if over_flag else self.__execute_transfer_with_steps
        new_item, error = execute_transfer(
            step_runner=step_runner, source_fileitem=planned_item.source_fileitem,
            target_storage=planned_item.target_storage, source_oper=source_oper, target_oper=target_oper,
            target_file=Path(planned_item.target_path), transfer_type=transfer_type,
        )
        if not new_item:
            return None, TransferInfo(
                success=False, message=error,
                fileitem=FileItem.model_validate(checkpoint.planning_input.source_fileitem),
                transfer_type=transfer_type, need_notify=checkpoint.need_notify,
            )
        return new_item, None

    def execute_transfer_plan(
        self,
        checkpoint: TransferPlanCheckpoint,
        *,
        meta: MetaBase,
        mediainfo: MediaInfo | MusicInfo,
        source_oper: StorageBase,
        target_oper: StorageBase,
        cleanup_before_transfer: Optional[Callable[[], None]] = None,
        observe_cleanup_before_transfer: Optional[Callable[[], bool]] = None,
        step_runner: Optional[TransferStepRunner] = None,
    ) -> TransferInfo:
        """
        只消费冻结目标和有序操作，并在执行期处理覆盖与插件拦截。

        持久步骤意图中的源文件必须使用规划输入快照；目录执行时可能重新统计
        STREAM 大小，但这类运行期投影不属于步骤身份。
        预览只构造结果，不创建目录或执行插件拦截。执行冲突、证据不足或存储异常可向上传播。

        :param checkpoint: 包含冻结目标、有序操作和策略的检查点
        :param meta: 恢复后的源元数据，用于覆盖和插件事件
        :param mediainfo: 恢复后的媒体信息
        :param source_oper: 源存储适配器
        :param target_oper: 目标存储适配器
        :param cleanup_before_transfer: 兼容清理回调；尽管参数名沿用旧名，当前在目标成功落地后调用
        :param observe_cleanup_before_transfer: 只读确认兼容清理是否完成的回调
        :param step_runner: 可恢复步骤执行器；None 时沿用同步执行
        :return: 整理结果，包含目标和成功文件统计或拒绝原因
        """
        fileitem = FileItem.model_validate(checkpoint.planning_input.source_fileitem)
        frozen_source_payload = checkpoint.planning_input.source_fileitem
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
                file_list=[cast(str, fileitem.path)],
                file_list_new=[target_path.as_posix()],
                file_count=0 if item_type == "dir" else 1,
                total_size=0 if item_type == "dir" else fileitem.size or 0,
                need_scrape=checkpoint.need_scrape,
                transfer_type=transfer_type,
                need_notify=False,
            )

        if fileitem.type == "dir":
            stream_path = Path(cast(str, fileitem.path)) / "BDMV" / "STREAM"
            stream_sizes = [
                FileItem.model_validate(item.source_fileitem).size or 0
                for item in checkpoint.items
                if Path(str(item.source_fileitem.get("path", ""))).parent
                == stream_path
            ]
            if stream_sizes:
                fileitem.size = sum(stream_sizes)
            allowed, reason = self.__intercept_with_step(
                step_runner=step_runner,
                payload={
                    "source": frozen_source_payload,
                    "target_storage": target_storage,
                    "target_path": target_path.as_posix(),
                    "transfer_type": transfer_type,
                },
                invoke=lambda: self.__intercept_transfer(
                    fileitem=fileitem,
                    meta=meta,
                    mediainfo=mediainfo,
                    target_storage=target_storage,
                    target_path=target_path,
                    transfer_type=transfer_type,
                ),
            )
            if not allowed:
                return TransferInfo(
                    success=False,
                    message=reason,
                    fileitem=fileitem,
                    transfer_type=transfer_type,
                    need_notify=checkpoint.need_notify,
                )
            target_diritem = self.__ensure_directory_with_step(
                step_runner=step_runner,
                target_oper=target_oper,
                target_storage=target_storage,
                path=target_path,
            )
            if not target_diritem:
                return TransferInfo(
                    success=False,
                    message=f"获取目标目录失败：{target_path}",
                    fileitem=fileitem,
                    transfer_type=transfer_type,
                    need_notify=checkpoint.need_notify,
                )
            for planned_item in checkpoint.items:
                new_item, failure = self.__execute_directory_leaf(
                    planned_item=planned_item, checkpoint=checkpoint, meta=meta, mediainfo=mediainfo,
                    source_oper=source_oper, target_oper=target_oper, step_runner=step_runner,
                )
                if failure:
                    return failure
                self.__update_result(
                    result=result,
                    file_list=[planned_item.source_fileitem.get("path")],
                    file_list_new=[cast(FileItem, new_item).path],
                )
            self.__cleanup_with_step(
                step_runner=step_runner,
                cleanup=cleanup_before_transfer,
                observe_cleanup=observe_cleanup_before_transfer,
                source_path=cast(str, fileitem.path),
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
                fail_list=[fileitem.path] if fileitem.path else [],
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
                fail_list=[fileitem.path] if fileitem.path else [],
                transfer_type=transfer_type,
                need_notify=checkpoint.need_notify,
            )
        target_file = Path(planned_item.target_path)
        over_flag, delete_versions, overwrite_failure = self.__resolve_overwrite_with_step(
            step_runner=step_runner,
            fileitem=fileitem,
            source_fileitem=frozen_source_payload,
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
        allowed, reason = self.__intercept_with_step(
            step_runner=step_runner,
            payload={
                "source": frozen_source_payload,
                "target_storage": target_storage,
                "target_path": target_file.as_posix(),
                "transfer_type": transfer_type,
                "over_flag": over_flag,
            },
            invoke=lambda: self.__intercept_transfer(
                fileitem=fileitem,
                meta=meta,
                mediainfo=mediainfo,
                target_storage=target_storage,
                target_path=target_file,
                transfer_type=transfer_type,
                over_flag=over_flag,
            ),
        )
        if not allowed:
            return TransferInfo(
                success=False,
                message=reason,
                fileitem=fileitem,
                fail_list=[fileitem.path] if fileitem.path else [],
                transfer_type=transfer_type,
                need_notify=checkpoint.need_notify,
            )

        target_diritem = self.__ensure_directory_with_step(
            step_runner=step_runner,
            target_oper=target_oper,
            target_storage=target_storage,
            path=target_file.parent,
        )
        if not target_diritem:
            return TransferInfo(
                success=False,
                message=f"目标目录 {target_file.parent} 获取失败",
                fileitem=fileitem,
                fail_list=[fileitem.path] if fileitem.path else [],
                transfer_type=transfer_type,
                need_notify=checkpoint.need_notify,
            )
        if step_runner is None:
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
        else:
            execute_transfer = (
                self.__replace_target_with_steps if over_flag
                else self.__execute_transfer_with_steps
            )
            new_item, error = execute_transfer(
                step_runner=step_runner,
                source_fileitem=planned_item.source_fileitem,
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
        if not new_item:
            error = error or f"{fileitem.path} 整理后未获取到目标文件信息"
            return TransferInfo(
                success=False,
                message=error,
                fileitem=fileitem,
                fail_list=[fileitem.path] if fileitem.path else [],
                transfer_type=transfer_type,
                need_notify=checkpoint.need_notify,
            )
        if delete_versions:
            if step_runner is None:
                self.__delete_version_files(target_oper, target_file)
            else:
                self.__delete_version_files_with_steps(
                    step_runner=step_runner,
                    storage_oper=target_oper,
                    target_storage=target_storage,
                    path=target_file,
                )
        self.__cleanup_with_step(
            step_runner=step_runner,
            cleanup=cleanup_before_transfer,
            observe_cleanup=observe_cleanup_before_transfer,
            source_path=cast(str, fileitem.path),
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
    def __download_to_local(
            fileitem: FileItem,
            source_oper: StorageBase,
            target_file: Path,
            transfer_type: str,
    ) -> tuple[bool, str]:
        """
        通过隔离临时目录下载，落地后按需删除远端源文件。

        临时目录与目标在同一父目录下，退出时自动清理；move 的源删除失败时目标可能已落地。

        :param fileitem: 待下载的远端源文件
        :param source_oper: 负责下载及可选删除的源适配器
        :param target_file: 最终本地路径；自动创建父目录
        :param transfer_type: 仅接受 copy 或 move
        :return: 是否完成及错误信息；成功时错误文本为空
        """
        if transfer_type not in ("copy", "move"):
            return False, f"不支持的整理方式：{transfer_type}"
        target_file.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix=".mp-download-", dir=target_file.parent) as temp_dir:
            tmp_file = source_oper.download(fileitem=fileitem, path=Path(temp_dir))
            if not tmp_file:
                return False, f"{fileitem.path} {fileitem.storage} 下载失败"
            move_result, move_error = SystemUtils.move(tmp_file, target_file)
            if move_result != 0:
                return False, f"{fileitem.path} 下载后移动到 {target_file} 失败：{move_error}"
            if transfer_type == "move" and not source_oper.delete(fileitem):
                return False, f"{fileitem.path} 源文件删除失败"
        return True, ""

    @staticmethod
    def __transfer_local_to_local(
        source_oper: StorageBase,
        fileitem: FileItem,
        target_file: Path,
        transfer_type: str,
    ) -> Tuple[bool, str]:
        """
        执行本地存储之间的单文件传输操作。

        :param source_oper: 本地存储适配器
        :param fileitem: 源文件项
        :param target_file: 目标文件完整路径
        :param transfer_type: 整理方式
        :return: 操作成功标记及错误说明
        """
        if not target_file.parent.exists():
            target_file.parent.mkdir(parents=True, exist_ok=True)
        if transfer_type == "copy":
            state = source_oper.copy(fileitem, target_file.parent, target_file.name)
        elif transfer_type == "move":
            state = source_oper.move(fileitem, target_file.parent, target_file.name)
        elif transfer_type in ("link", "softlink", "reflink"):
            action = getattr(source_oper, transfer_type, None)
            state = bool(callable(action) and action(fileitem, target_file))
        else:
            return False, f"不支持的整理方式：{transfer_type}"
        if state:
            return True, ""
        return False, f"{fileitem.path} {transfer_type} 失败"

    @staticmethod
    def __transfer_same_remote_storage(
        fileitem: FileItem,
        target_storage: str,
        source_oper: StorageBase,
        target_oper: StorageBase,
        target_file: Path,
        transfer_type: str,
        get_remote_targetitem: Callable[[FileItem, Path], FileItem],
    ) -> Tuple[Optional[FileItem], str]:
        """
        在相同的非本地远端存储间执行文件复制、移动或链接操作。

        :param fileitem: 源文件项
        :param target_storage: 目标存储标识
        :param source_oper: 源存储适配器
        :param target_oper: 目标存储适配器
        :param target_file: 目标文件完整路径
        :param transfer_type: 整理方式
        :param get_remote_targetitem: 获取远端目标项回调
        :return: 目标文件项及错误说明
        """
        if not source_oper.is_support_transtype(transfer_type):
            return None, f"存储 {fileitem.storage} 不支持 {transfer_type} 整理方式"

        if transfer_type in ("copy", "move"):
            target_fileitem = target_oper.get_folder(target_file.parent)
            if not target_fileitem or not target_fileitem.path:
                return None, f"【{target_storage}】{target_file.parent} 目录获取失败"
            action_name = "copy_item" if transfer_type == "copy" else "move_item"
            custom_item_action = getattr(source_oper, action_name, None)
            fallback_action = getattr(source_oper, transfer_type, None)
            if callable(custom_item_action):
                new_item = custom_item_action(
                    fileitem, Path(target_fileitem.path), target_file.name
                )
                if new_item:
                    return new_item, ""
            elif callable(fallback_action) and fallback_action(
                fileitem, Path(target_fileitem.path), target_file.name
            ):
                return get_remote_targetitem(fileitem, target_file), ""
            action_desc = "复制" if transfer_type == "copy" else "移动"
            return None, f"【{target_storage}】{fileitem.path} {action_desc}文件失败"

        if transfer_type in ("link", "reflink"):
            action = getattr(source_oper, transfer_type, None)
            if callable(action) and action(fileitem, target_file):
                return get_remote_targetitem(fileitem, target_file), ""
            action_desc = "引用链接" if transfer_type == "reflink" else "硬链接"
            return None, f"【{target_storage}】{fileitem.path} 创建{action_desc}失败"

        return None, f"不支持的整理方式：{transfer_type}"

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
        执行单文件存储操作，按本地与远端组合选择传输路径。

        不同远端存储之间不支持直接整理。远端元数据延迟可见时可按目标路径合成结果。
        兼容的本地到远端 move 分支不检查源删除返回值；持久任务在上层拆分目标落地与源删除。

        :param fileitem: 源文件项及所属存储
        :param target_storage: 目标存储标识
        :param source_oper: 源存储适配器
        :param target_oper: 目标存储适配器
        :param target_file: 最终文件路径，必要时创建父目录
        :param transfer_type: copy、move、link、softlink 或 reflink，支持范围取决于存储组合
        :return: 目标文件项及错误文本；失败时文件项为 None
        """

        def __get_targetitem(_path: Path) -> FileItem:
            """
            读取已落地本地目标的大小和修改时间。

            :param _path: 已存在的本地目标文件
            :return: 实际本地文件元数据
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
            为元数据延迟可见的远端目标合成临时文件项。

            :param _source_item: 提供大小、修改时间和缩略图的源项
            :param _path: 已确认的目标路径
            :return: 没有远端文件标识的目标文件项
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
            查询远端目标，索引未刷新时按源信息构造兼容结果。

            :param _source_item: 用于缺失元数据回退的源文件项
            :param _path: 目标路径
            :return: 远端查询结果或按路径合成的文件项
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
            state, error = TransHandler.__transfer_local_to_local(
                source_oper=source_oper,
                fileitem=fileitem,
                target_file=target_file,
                transfer_type=transfer_type,
            )
            if state:
                return __get_targetitem(target_file), ""
            return None, error
        elif fileitem.storage == "local" and target_storage != "local":
            filepath = Path(cast(str, fileitem.path))
            if not filepath.exists():
                return None, f"文件 {filepath} 不存在"
            if transfer_type == "copy":
                target_fileitem = target_oper.get_folder(target_file.parent)
                if target_fileitem:
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
                target_fileitem = target_oper.get_folder(target_file.parent)
                if target_fileitem:
                    new_item = target_oper.upload(
                        target_fileitem, filepath, target_file.name
                    )
                    if new_item:
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
            if target_file.exists():
                logger.warn(f"文件已存在：{target_file}")
                return __get_targetitem(target_file), ""
            state, error = TransHandler.__download_to_local(
                fileitem, source_oper, target_file, transfer_type,
            )
            return (__get_targetitem(target_file), "") if state else (None, error)
        elif fileitem.storage == target_storage:
            return TransHandler.__transfer_same_remote_storage(
                fileitem=fileitem,
                target_storage=target_storage,
                source_oper=source_oper,
                target_oper=target_oper,
                target_file=target_file,
                transfer_type=transfer_type,
                get_remote_targetitem=__get_remote_targetitem,
            )

        return None, "未知错误"

    @staticmethod
    def __rename_subtitles(sub_item: FileItem, new_file: Path) -> Path:
        """
        在主媒体目标名中加入字幕语言及默认字幕标记。

        繁中优先于简中匹配；匹配 DEFAULT_SUB 配置时添加 default 标记。

        :param sub_item: 提供字幕名称和扩展名的源文件项
        :param new_file: 模板生成的目标文件路径
        :return: 保留字幕扩展名并追加已识别语言标记的路径
        """
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

        file_ext = f".{sub_item.extension}"
        new_file_type = ""

        # 先识别繁中，避免“繁体中文/繁中字”等名称被后面的“中文/中字”简中兜底规则误判。
        if re.search(_zhtw_sub_re, sub_item.name or "", re.I):
            new_file_type = ".zh-tw"
        elif re.search(_zhcn_sub_re, sub_item.name or "", re.I):
            new_file_type = ".chi.zh-cn"
        elif re.search(_ja_sub_re, sub_item.name or "", re.I):
            new_file_type = ".ja"
        elif re.search(_eng_sub_re, sub_item.name or "", re.I):
            new_file_type = ".eng"

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
        同步整理单个文件，按需安全替换旧目标并累加成功统计。

        不允许覆盖时若目标已存在则拒绝；覆盖流程中的 RuntimeError 转为失败原因。

        :param fileitem: 源文件项
        :param source_oper: 源存储适配器
        :param target_oper: 目标存储适配器
        :param target_storage: 目标存储标识
        :param target_file: 最终目标文件路径
        :param transfer_type: 整理方式
        :param result: 原地累加成功文件列表、数量和字节数的结果对象
        :param over_flag: 是否覆盖；为真时先完整暂存再替换旧目标
        :return: 成功目标及提示，或 None 和失败原因
        """
        logger.info(
            f"正在整理文件：【{fileitem.storage}】{fileitem.path} 到 【{target_storage}】{target_file}，"
            f"操作类型：{transfer_type}"
        )
        if not over_flag and target_oper.get_item_strict(target_file) is not None:
            logger.warn(f"文件已存在：【{target_storage}】{target_file}")
            return None, f"【{target_storage}】{target_file} 已存在"
        if over_flag:
            try:
                new_item, errmsg = self.__replace_target_with_steps(
                    step_runner=None, source_fileitem=fileitem.model_dump(mode="json"),
                    target_storage=target_storage, source_oper=source_oper,
                    target_oper=target_oper, target_file=target_file,
                    transfer_type=transfer_type,
                )
            except RuntimeError as error:
                return None, str(error)
        else:
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
        mediainfo: Union[MediaInfo, MusicInfo],
        target_path: Path,
        need_type_folder: Optional[bool] = False,
        need_category_folder: Optional[bool] = False,
    ) -> Path:
        """
        在自定义目标根目录下追加媒体类型和分类路径。

        分类路径经过安全检查，并避免重复追加媒体类型层级。

        :param mediainfo: 决定媒体类型和分类的识别结果
        :param target_path: 调用方提供的目标根路径
        :param need_type_folder: 是否追加媒体类型目录，默认不追加
        :param need_category_folder: 是否追加分类目录，默认不追加
        :return: 拼接后的目标路径；不创建目录
        """
        if need_type_folder and mediainfo.type:
            target_path = target_path / mediainfo.type.value
        if need_category_folder:
            category_path = DirectoryHelper().resolve_media_category(mediainfo).path
            if category_path:
                category_path = category_path_below_media_type(
                    category_path,
                    mediainfo.type,
                    type_folder_enabled=bool(need_type_folder),
                )
            if category_path:
                target_path = append_classification_category_path(
                    target_path,
                    category_path,
                )
        return target_path

    @staticmethod
    def get_dest_dir(
        mediainfo: Union[MediaInfo, MusicInfo],
        target_dir: TransferDirectoryConf,
        need_type_folder: Optional[bool] = None,
        need_category_folder: Optional[bool] = None,
    ) -> Path:
        """
        根据目录配置解析媒体库类型和分类子目录。

        手动指定媒体类型时优先使用配置类型；分类路径按固定分类标识或安全路径快照解析。

        :param mediainfo: 用于自动分类的媒体信息
        :param target_dir: 包含媒体库根路径和分类配置的目录设置
        :param need_type_folder: 是否添加类型目录；None 时沿用目录设置
        :param need_category_folder: 是否添加分类目录；None 时沿用目录设置
        :return: 最终媒体库目录路径；不创建目录
        """
        if not target_dir.library_path:
            raise ValueError("媒体库目录路径为空")
        if need_type_folder is None:
            need_type_folder = target_dir.library_type_folder
        if need_category_folder is None:
            need_category_folder = target_dir.library_category_folder
        if not target_dir.media_type and need_type_folder and mediainfo.type:
            library_dir = Path(target_dir.library_path) / mediainfo.type.value
        elif target_dir.media_type and need_type_folder:
            type_folder = (
                classification_media_type(target_dir.media_type)
                or target_dir.media_type
            )
            library_dir = Path(target_dir.library_path) / type_folder
        else:
            library_dir = Path(target_dir.library_path)
        if need_category_folder:
            helper = DirectoryHelper()
            category_path = helper.category_path_for_directory(
                target_dir,
                mediainfo,
            )
            if category_path:
                category_path = category_path_below_media_type(
                    category_path,
                    target_dir.media_type or mediainfo.type,
                    type_folder_enabled=bool(need_type_folder),
                )
            if category_path:
                # 固定 ID 使用当前策略路径，旧配置和人工覆盖使用安全路径快照。
                library_dir = append_classification_category_path(
                    library_dir,
                    category_path,
                )

        return library_dir

    @staticmethod
    def get_naming_dict(
        meta: MetaBase,
        mediainfo: Union[MediaInfo, MusicInfo],
        file_ext: Optional[str] = None,
        episodes_info: Optional[List[TmdbEpisode]] = None,
    ) -> dict[str, Any]:
        """
        构造重命名模板使用的媒体变量和安全分类路径。

        保留各数据源原有标识变量，不暴露通用 media_source 和 media_id。

        :raises ValueError: 媒体分类配置解析为无效路径
        :param meta: 文件名及音视频属性等源元数据
        :param mediainfo: 已识别的媒体详情
        :param file_ext: 包含点号的文件扩展名；可为空
        :param episodes_info: 当前季全部集详情；可为空
        :return: 重命名模板上下文字典
        """
        naming_context = TemplateHelper().builder.build(
            meta=meta,
            mediainfo=mediainfo,
            file_extension=file_ext,
            episodes_info=episodes_info,
        )
        category_resolution = DirectoryHelper().resolve_media_category(mediainfo)
        if category_resolution.state == "invalid_path":
            raise ValueError(
                category_resolution.message or "媒体分类目录路径无效"
            )
        naming_context["category"] = "/".join(category_resolution.path)
        # 重命名格式是独立的用户配置契约，继续只暴露各数据源原有 ID 变量。
        naming_context.pop("media_source", None)
        naming_context.pop("media_id", None)
        return naming_context

    @staticmethod
    def __find_version_files(
            storage_oper: StorageBase,
            path: Path,
    ) -> list[FileItem]:
        """
        稳定列出目标同目录、同季集且分片标识不冲突的其他视频版本。

        仅当双方都有 Part 标识且不同才排除；不会把新目标或非视频附件列入候选。

        :param storage_oper: 用于严格查询父目录和列举文件的适配器
        :param path: 应保留的新版本文件路径
        :return: 按路径和文件标识排序的候选列表
        """
        meta = MetaInfoPath(path)
        parent_item = storage_oper.get_item_strict(path.parent)
        if not parent_item:
            return []
        media_files = storage_oper.list(parent_item) or []
        result: list[FileItem] = []
        for media_file in media_files:
            if not media_file.path or not media_file.extension:
                continue
            media_path = Path(media_file.path)
            if media_path == path or media_file.type != "file":
                continue
            if f".{media_file.extension.lower()}" not in get_runtime_setting('RMT_MEDIAEXT'):
                continue
            filemeta = MetaInfoPath(media_path)
            if filemeta.season != meta.season or filemeta.episode != meta.episode:
                continue
            if meta.part and filemeta.part and filemeta.part != meta.part:
                continue
            result.append(media_file)
        return sorted(result, key=lambda item: (item.path or "", item.fileid or ""))

    @classmethod
    def __delete_version_files_with_steps(
            cls,
            *,
            step_runner: TransferStepRunner,
            storage_oper: StorageBase,
            target_storage: str,
            path: Path,
    ) -> None:
        """
        先冻结旧版本候选清单，再逐个记录删除步骤。

        候选路径已不存在视为成功；当前删除仅按冻结路径查询，不额外核对候选文件身份。

        :param step_runner: 持久化步骤执行器
        :param storage_oper: 负责候选发现、查询和删除的适配器
        :param target_storage: 媒体库所在的存储标识
        :param path: 已落地且应保留的新版本路径
        """
        def discover() -> TransferStepResult:
            """
            读取并冻结当前版本删除候选，读失败不产生副作用。
            """
            candidates = cls.__find_version_files(storage_oper, path)
            return TransferStepResult(payload={
                "items": [item.model_dump(mode="json") for item in candidates],
            })

        discovery = cls.__run_persisted_step(
            step_runner,
            phase="prepare",
            kind="discover_version_targets",
            payload={"storage": target_storage, "path": path.as_posix()},
            execute=discover,
            observe=lambda: TransferOperationObservation(
                state=TransferOperationObservationState.NOT_APPLIED,
                evidence=TransferStepResult(payload={
                    "reason": "read-only discovery may be repeated",
                }),
            ),
        )
        raw_items = discovery.payload.get("items")
        if not isinstance(raw_items, list):
            raise RuntimeError("版本删除候选检查点格式无效")
        for raw_item in raw_items:
            candidate = FileItem.model_validate(raw_item)

            def delete_candidate(item: FileItem = candidate) -> TransferStepResult:
                """
                删除冻结候选路径当前指向的文件，不存在时保持成功。

                删除失败时抛出 RuntimeError；不核对路径当前文件是否仍是冻结的同一版本。

                :param item: 本次冻结的版本候选，默认使用循环中的候选
                :return: 记录路径和删除成功的步骤结果
                """
                current = storage_oper.get_item_strict(Path(cast(str, item.path)))
                if current is not None and not storage_oper.delete(current):
                    raise RuntimeError(f"版本文件 {item.path} 删除失败")
                return TransferStepResult(payload={
                    "path": item.path,
                    "deleted": True,
                })

            def observe_candidate(item: FileItem = candidate) -> TransferOperationObservation:
                """
                只读观察冻结候选路径是否已经消失。

                :param item: 本次冻结的版本候选，默认使用循环中的候选
                :return: 删除完成、尚未完成或查询未知的观察结果
                """
                return cls.__observe_item_presence(
                    storage_oper,
                    Path(cast(str, item.path)),
                    applied_when_present=False,
                )

            cls.__run_persisted_step(
                step_runner,
                phase="prepare",
                kind="delete_version_target",
                payload={
                    "storage": target_storage,
                    "item": candidate.model_dump(mode="json"),
                },
                execute=delete_candidate,
                observe=observe_candidate,
            )

    @staticmethod
    def __delete_version_files(storage_oper: StorageBase, path: Path) -> bool:
        """
        同步删除同目录中季集相同且分片标识不冲突的其他视频版本。

        仅处理配置的视频扩展名；未检查逐个删除的返回值，True 不保证所有旧版本均已删除。

        :param storage_oper: 负责列举和删除的目标存储适配器
        :param path: 应保留的新版本文件路径，并非目录路径
        :return: 完成候选遍历时为 True；存储、父目录或目录内容不可用时为 False
        """
        if not storage_oper:
            return False
        meta = MetaInfoPath(path)
        season = meta.season
        episode = meta.episode
        part = meta.part
        logger.warn(f"正在删除目标目录中其它版本的文件：{path.parent}")
        parent_item = storage_oper.get_item(path.parent)
        if not parent_item:
            logger.warn(f"目录 {path.parent} 不存在")
            return False
        media_files = storage_oper.list(parent_item)
        if not media_files:
            logger.info(f"目录 {path.parent} 中没有文件")
            return False
        for media_file in media_files:
            if not media_file.path or not media_file.extension:
                continue
            media_path = Path(media_file.path)
            if media_path == path:
                continue
            if media_file.type != "file":
                continue
            # 当前只有视频文件需要保留最新版本，其余格式无需处理，以避免误删 (issue 5449)
            if f".{media_file.extension.lower()}" not in get_runtime_setting('RMT_MEDIAEXT'):
                continue
            filemeta = MetaInfoPath(media_path)
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
        rename_dict: dict[str, Any],
        path: Optional[Path] = None,
        source_path: Optional[str] = None,
        source_item: Optional[FileItem] = None,
    ) -> Path:
        """
        渲染命名模板并经过插件事件生成目标路径。

        先发送变量构建事件，再渲染模板并发送路径调整事件。

        :raises ValueError: 提供根目录时生成路径越出该目录
        :param template_string: Jinja2 重命名模板
        :param rename_dict: 模板上下文；渲染前插件可修改或替换此字典
        :param path: 目标根路径；提供时验证生成路径仍在根目录内
        :param source_path: 供插件参考的源路径；可为空
        :param source_item: 供插件参考的源文件项；可为空
        :return: 生成的目标路径；未提供根目录时直接返回渲染路径
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
        if build_event and isinstance(build_event.event_data, TransferRenameBuildEventData):
            rename_dict = build_event.event_data.rename_dict

        template = Template(template_string)
        render_str = template.render(rename_dict)

        logger.debug(f"Initial render string: {render_str}")
        event_data = TransferRenameEventData(
            template_string=template_string,
            rename_dict=rename_dict,
            render_str=render_str,
            path=path,
            source_path=source_path,
            source_item=source_item,
        )
        event = eventmanager.send_event(ChainEventType.TransferRename, event_data)
        if event and isinstance(event.event_data, TransferRenameEventData):
            event_data = event.event_data
            if event_data.updated and event_data.updated_str:
                logger.debug(
                    f"Render string updated by event: "
                    f"{render_str} -> {event_data.updated_str} (source: {event_data.source})"
                )
                render_str = event_data.updated_str

        if path:
            return ensure_path_within_root(path, path / render_str)
        else:
            return Path(render_str)
