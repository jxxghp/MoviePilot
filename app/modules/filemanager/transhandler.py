import re
import shutil
import subprocess
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List, Tuple

from jinja2 import Template

from app.core.config import settings
from app.core.context import MediaInfo
from app.core.event import eventmanager
from app.core.meta import MetaBase
from app.core.metainfo import MetaInfoPath
from app.helper.directory import DirectoryHelper
from app.helper.message import TemplateHelper
from app.log import logger
from app.modules.filemanager.storages import StorageBase
from app.schemas import (
    TransferInfo,
    TmdbEpisode,
    TransferDirectoryConf,
    FileItem,
    TransferInterceptEventData,
    TransferOverwriteCheckEventData,
    TransferRenameBuildEventData,
    TransferRenameEventData,
)
from app.schemas.types import MediaType, ChainEventType
from app.utils.system import SystemUtils

_BLURAY_MPLS_MAX_SIZE = 1024 * 1024
_BLURAY_MPLS_MAX_PLAYITEMS = 200
_BLURAY_MPLS_MAX_DURATION_SECONDS = 12 * 60 * 60
_BLURAY_MPLS_TIME_BASE = 45000
_BLURAY_PLAYITEM_IN_TIME_OFFSET = 14
_BLURAY_PLAYITEM_OUT_TIME_OFFSET = 18


@dataclass(frozen=True)
class _BlurayRemuxSource:
    """
    蓝光转封装源分片。
    """
    path: Path
    in_time: int = 0
    out_time: int = 0

    @property
    def need_clip(self) -> bool:
        """
        是否需要按 MPLS 时间戳裁剪。
        """
        return self.in_time > 0 or self.out_time > 0


class TransHandler:
    """
    文件转移整理类
    """

    _bluray_remux_locks = {}
    _bluray_remux_locks_guard = threading.Lock()

    def __init__(self):
        pass

    @classmethod
    def __get_bluray_remux_target_lock(cls, target_file: Path) -> threading.Lock:
        """
        获取蓝光转封装目标文件锁，避免并发任务共享临时文件或相互覆盖。
        """
        lock_key = target_file.resolve().as_posix()
        with cls._bluray_remux_locks_guard:
            if lock_key not in cls._bluray_remux_locks:
                cls._bluray_remux_locks[lock_key] = threading.Lock()
            return cls._bluray_remux_locks[lock_key]

    @staticmethod
    def __is_path_relative_to(path: Path, parent: Path) -> bool:
        """
        判断 path 是否位于 parent 内部，兼容目标文件尚未创建的场景。
        """
        try:
            path.resolve().relative_to(parent.resolve())
            return True
        except (OSError, ValueError):
            return False

    @staticmethod
    def __is_same_path(path: Path, other: Path) -> bool:
        """
        判断两个路径是否指向同一位置。
        """
        try:
            return path.resolve() == other.resolve()
        except OSError:
            return False

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
    def __get_local_bluray_root(path: Path) -> Optional[Path]:
        """
        获取本地蓝光原盘根目录。
        """
        if (path / "BDMV" / "STREAM").is_dir():
            return path
        if path.name.upper() == "BDMV" and (path / "STREAM").is_dir():
            return path.parent
        if (
            path.name.upper() == "STREAM"
            and path.parent.name.upper() == "BDMV"
            and path.is_dir()
        ):
            return path.parent.parent
        return None

    @staticmethod
    def __parse_mpls_stream_items(mpls_file: Path) -> List[Tuple[str, int, int]]:
        """
        从 MPLS 播放列表中提取 m2ts 分片名和裁剪时间。
        """
        try:
            if mpls_file.stat().st_size > _BLURAY_MPLS_MAX_SIZE:
                logger.warn(f"蓝光播放列表 {mpls_file} 过大，跳过解析")
                return []
            data = mpls_file.read_bytes()
            if len(data) < 16:
                return []
            playlist_start = int.from_bytes(data[8:12], "big")
            if playlist_start <= 0 or playlist_start + 10 > len(data):
                return []
            pos = playlist_start + 6
            playitem_count = int.from_bytes(data[pos:pos + 2], "big")
            if (
                    playitem_count <= 0
                    or playitem_count > _BLURAY_MPLS_MAX_PLAYITEMS
            ):
                return []
            pos += 4
            stream_items = []
            seen_items = set()
            total_duration = 0
            for _ in range(playitem_count):
                if pos + _BLURAY_PLAYITEM_OUT_TIME_OFFSET + 4 > len(data):
                    return []
                item_length = int.from_bytes(data[pos:pos + 2], "big")
                item_end = pos + 2 + item_length
                if (
                        item_length < _BLURAY_PLAYITEM_OUT_TIME_OFFSET + 4 - 2
                        or item_end > len(data)
                ):
                    return []
                clip_name = data[pos + 2:pos + 7].decode(
                    "ascii", errors="ignore"
                ).strip("\x00 ")
                if not clip_name:
                    return []
                in_time = int.from_bytes(
                    data[
                        pos + _BLURAY_PLAYITEM_IN_TIME_OFFSET:
                        pos + _BLURAY_PLAYITEM_IN_TIME_OFFSET + 4
                    ],
                    "big",
                )
                out_time = int.from_bytes(
                    data[
                        pos + _BLURAY_PLAYITEM_OUT_TIME_OFFSET:
                        pos + _BLURAY_PLAYITEM_OUT_TIME_OFFSET + 4
                    ],
                    "big",
                )
                if out_time <= in_time:
                    return []
                item_key = (clip_name.upper(), in_time, out_time)
                if item_key in seen_items:
                    return []
                seen_items.add(item_key)
                total_duration += out_time - in_time
                if (
                        total_duration
                        > _BLURAY_MPLS_MAX_DURATION_SECONDS * _BLURAY_MPLS_TIME_BASE
                ):
                    return []
                stream_items.append((f"{clip_name}.m2ts", in_time, out_time))
                pos = item_end
            return stream_items
        except Exception as err:
            logger.debug(f"解析蓝光播放列表 {mpls_file} 失败：{err}")
            return []

    @classmethod
    def __parse_mpls_stream_names(cls, mpls_file: Path) -> List[str]:
        """
        从 MPLS 播放列表中提取 m2ts 分片名。
        """
        return [item[0] for item in cls.__parse_mpls_stream_items(mpls_file)]

    @classmethod
    def __get_bluray_remux_sources(
            cls,
            bluray_root: Path,
            allow_largest_fallback: bool = True,
    ) -> Tuple[List[_BlurayRemuxSource], int]:
        """
        获取蓝光原盘转封装源文件列表。
        """
        stream_dir = bluray_root / "BDMV" / "STREAM"
        stream_files = [
            item
            for item in stream_dir.iterdir()
            if (
                not item.is_symlink()
                and item.is_file()
                and item.suffix.lower() == ".m2ts"
                and cls.__is_path_relative_to(item, bluray_root)
            )
        ] if stream_dir.is_dir() else []
        if not stream_files:
            return [], 0

        stream_map = {item.name.upper(): item for item in stream_files}
        playlist_dir = bluray_root / "BDMV" / "PLAYLIST"
        playlist_files = []
        best_files = []
        best_size = 0
        if playlist_dir.is_dir():
            playlist_files = [
                item
                for item in playlist_dir.iterdir()
                if item.is_file() and item.suffix.lower() == ".mpls"
            ]
            for mpls_file in sorted(playlist_files):
                stream_items = cls.__parse_mpls_stream_items(mpls_file)
                files = [
                    stream_map.get(name.upper())
                    for name, _in_time, _out_time in stream_items
                ]
                if not files or any(item is None for item in files):
                    continue
                total_size = sum(item.stat().st_size for item in files)
                if total_size > best_size:
                    best_files = [
                        _BlurayRemuxSource(file, in_time, out_time)
                        for file, (_name, in_time, out_time) in zip(files, stream_items)
                    ]
                    best_size = total_size
        if best_files:
            return best_files, best_size

        if playlist_files:
            logger.warn(
                f"蓝光原盘 {bluray_root} 存在播放列表但无法匹配可转封装分片，跳过转封装"
            )
            return [], 0

        if len(stream_files) > 1 and not allow_largest_fallback:
            logger.warn(
                f"蓝光原盘 {bluray_root} 缺少播放列表，移动模式跳过最大分片回退"
            )
            return [], 0

        largest_file = max(stream_files, key=lambda item: item.stat().st_size)
        return [_BlurayRemuxSource(largest_file)], largest_file.stat().st_size

    @staticmethod
    def __build_local_fileitem(path: Path) -> FileItem:
        """
        根据本地文件路径构造文件项。
        """
        return FileItem(
            storage="local",
            path=path.as_posix(),
            name=path.name,
            basename=path.stem,
            type="file",
            size=path.stat().st_size,
            extension=path.suffix.lstrip("."),
            modify_time=path.stat().st_mtime,
        )

    @staticmethod
    def __escape_ffconcat_path(path: Path) -> str:
        """
        转义 ffconcat 文件中的路径。
        """
        value = path.as_posix()
        if "\n" in value or "\r" in value:
            raise ValueError("蓝光分片路径包含换行控制字符")
        return value.replace("'", "'\\''")

    @staticmethod
    def __format_bluray_time(value: int) -> str:
        """
        将 MPLS 时间戳转换为 ffconcat 秒数。
        """
        return f"{value / _BLURAY_MPLS_TIME_BASE:.6f}".rstrip("0").rstrip(".")

    @classmethod
    def __run_bluray_remux(
            cls,
            source_files: List[_BlurayRemuxSource],
            target_file: Path,
            allow_target_replace: bool,
            recheck_target_size: Optional[int],
    ) -> Tuple[bool, str]:
        """
        使用 ffmpeg 将蓝光原盘分片转封装为单个 MKV。
        """
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return False, "未找到 ffmpeg，无法进行蓝光原盘转封装"

        target_file.parent.mkdir(parents=True, exist_ok=True)
        temp_suffix = uuid.uuid4().hex
        tmp_file = target_file.with_name(
            f".{target_file.stem}.{temp_suffix}.tmp{target_file.suffix}"
        )
        concat_file = target_file.with_name(
            f".{target_file.stem}.{temp_suffix}.ffconcat"
        )
        try:
            if len(source_files) == 1 and not source_files[0].need_clip:
                command = [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    source_files[0].path.as_posix(),
                    "-map",
                    "0",
                    "-c",
                    "copy",
                    "-dn",
                    tmp_file.as_posix(),
                ]
            else:
                concat_lines = ["ffconcat version 1.0"]
                for item in source_files:
                    concat_lines.append(
                        f"file '{cls.__escape_ffconcat_path(item.path)}'"
                    )
                    if item.in_time > 0:
                        concat_lines.append(
                            f"inpoint {cls.__format_bluray_time(item.in_time)}"
                        )
                    if item.out_time > 0:
                        concat_lines.append(
                            f"outpoint {cls.__format_bluray_time(item.out_time)}"
                        )
                concat_file.write_text(
                    "\n".join(concat_lines) + "\n",
                    encoding="utf-8",
                )
                command = [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    concat_file.as_posix(),
                    "-map",
                    "0",
                    "-c",
                    "copy",
                    "-dn",
                    tmp_file.as_posix(),
                ]
            completed = subprocess.run(
                command, capture_output=True, text=True, check=False
            )
            if completed.returncode != 0:
                errmsg = (completed.stderr or completed.stdout or "").strip()
                return False, f"ffmpeg 转封装失败：{errmsg or completed.returncode}"
            if not tmp_file.exists():
                return False, "ffmpeg 转封装完成但未生成目标文件"
            if tmp_file.stat().st_size <= 0:
                return False, "ffmpeg 转封装完成但输出文件为空"
            if (
                    not allow_target_replace
                    and (target_file.exists() or target_file.is_symlink())
            ):
                return False, f"{target_file} 在转封装期间被创建，取消覆盖"
            if (
                    recheck_target_size is not None
                    and tmp_file.stat().st_size <= recheck_target_size
            ):
                return False, "媒体库存在同名文件，且质量更好"
            tmp_file.replace(target_file)
            return True, ""
        except Exception as err:
            return False, f"蓝光原盘转封装失败：{err}"
        finally:
            for cleanup_file in [tmp_file, concat_file]:
                try:
                    if cleanup_file.exists():
                        cleanup_file.unlink()
                except OSError as err:
                    logger.warning(f"清理蓝光转封装临时文件失败：{cleanup_file} - {err}")

    def __get_bluray_remux_target_file(
            self,
            fileitem: FileItem,
            in_meta: MetaBase,
            mediainfo: MediaInfo,
            target_path: Path,
            rename_format: str,
            need_rename: bool,
    ) -> Path:
        """
        获取蓝光原盘转封装目标文件路径。
        """
        if need_rename:
            target_file = self.get_rename_path(
                path=target_path,
                template_string=rename_format,
                rename_dict=self.get_naming_dict(
                    meta=in_meta,
                    mediainfo=mediainfo,
                    file_ext=".mkv",
                ),
                source_path=fileitem.path,
                source_item=fileitem,
            )
            if target_file.suffix.lower() != ".mkv":
                target_file = target_file.parent / f"{target_file.name}.mkv"
            return target_file
        return target_path / f"{fileitem.name}.mkv"

    def __check_bluray_remux_overwrite(
            self,
            fileitem: FileItem,
            target_oper: StorageBase,
            target_file: Path,
            target_storage: str,
            transfer_type: str,
            overwrite_mode: Optional[str],
            source_size: int,
    ) -> Tuple[bool, str, bool]:
        """
        检查蓝光转封装目标文件覆盖策略。
        """
        target_item = target_oper.get_item(target_file)
        if not target_item and (target_file.exists() or target_file.is_symlink()):
            if target_file.is_file():
                target_item = self.__build_local_fileitem(target_file)
            else:
                target_item = FileItem(
                    storage=target_storage,
                    path=target_file.as_posix(),
                    name=target_file.name,
                    basename=target_file.stem,
                    type="file",
                    size=0,
                )
        if not target_item:
            return True, "", False

        logger.info(
            f"目的文件系统中已经存在同名文件 {target_file}，当前整理覆盖模式设置为 {overwrite_mode}"
        )
        overwrite_event_data = TransferOverwriteCheckEventData(
            fileitem=fileitem,
            target_item=target_item,
            target_storage=target_storage,
            target_path=target_file,
            overwrite_mode=overwrite_mode or "",
            transfer_type=transfer_type,
        )
        overwrite_event = eventmanager.send_event(
            ChainEventType.TransferOverwriteCheck,
            overwrite_event_data,
        )
        if overwrite_event and overwrite_event.event_data:
            overwrite_event_data = overwrite_event.event_data
            if overwrite_event_data.overwrite is True:
                return True, "", False
            if overwrite_event_data.overwrite is False:
                return False, overwrite_event_data.reason or "插件决定不覆盖已有文件", False

        if overwrite_mode in ["always", "latest"]:
            return True, "", False
        if overwrite_mode == "size":
            if (target_item.size or 0) < source_size:
                return True, "", True
            return False, "媒体库存在同名文件，且质量更好", False
        if overwrite_mode == "never":
            return False, "媒体库存在同名文件，当前覆盖模式为不覆盖", False
        return False, f"{target_file} 已存在", False

    def __transfer_bluray_remux(
            self,
            fileitem: FileItem,
            in_meta: MetaBase,
            mediainfo: MediaInfo,
            target_storage: str,
            target_path: Path,
            transfer_type: str,
            source_oper: StorageBase,
            target_oper: StorageBase,
            rename_format: str,
            overwrite_mode: Optional[str],
            need_scrape: bool,
            need_rename: bool,
            need_notify: bool,
    ) -> Optional[TransferInfo]:
        """
        在配置开启时将本地蓝光原盘目录转封装为单个 MKV。
        """
        if not settings.BLURAY_REMUX_ENABLED:
            return None
        if (fileitem.storage or "local") != "local" or target_storage != "local":
            logger.warn("蓝光原盘转封装仅支持本地源到本地目标，保持目录整理")
            return None
        if transfer_type not in ["copy", "move"]:
            logger.warn(
                f"蓝光原盘转封装不支持 {transfer_type} 整理方式，保持目录整理"
            )
            return None

        bluray_root = self.__get_local_bluray_root(Path(fileitem.path))
        if not bluray_root:
            return None
        if transfer_type == "move" and not self.__is_same_path(
            Path(fileitem.path), bluray_root
        ):
            return TransferInfo(
                success=False,
                message="蓝光原盘转封装移动模式不支持从子目录入口整理",
                fileitem=fileitem,
                fail_list=[fileitem.path],
                transfer_type=transfer_type,
                need_notify=need_notify,
            )
        if mediainfo.type == MediaType.TV:
            logger.warn("电视剧蓝光原盘可能包含多集或全集播放列表，保持目录整理")
            return None

        target_file = self.__get_bluray_remux_target_file(
            fileitem=fileitem,
            in_meta=in_meta,
            mediainfo=mediainfo,
            target_path=target_path,
            rename_format=rename_format,
            need_rename=need_rename,
        )
        if self.__is_path_relative_to(target_file, bluray_root):
            return TransferInfo(
                success=False,
                message="蓝光原盘转封装目标不能位于源目录内",
                fileitem=fileitem,
                fail_list=[fileitem.path],
                transfer_type=transfer_type,
                need_notify=need_notify,
            )
        if need_rename:
            folder_path = DirectoryHelper.get_media_root_path(
                rename_format, rename_path=target_file
            )
        else:
            folder_path = target_file.parent
        if not folder_path:
            return TransferInfo(
                success=False,
                message="重命名格式无效",
                fileitem=fileitem,
                transfer_type=transfer_type,
                need_notify=need_notify,
            )
        target_diritem = target_oper.get_folder(folder_path)
        if not target_diritem:
            return TransferInfo(
                success=False,
                message=f"目标目录 {folder_path} 获取失败",
                fileitem=fileitem,
                fail_list=[fileitem.path],
                transfer_type=transfer_type,
                need_notify=need_notify,
            )

        event_data = TransferInterceptEventData(
            fileitem=fileitem,
            meta=in_meta,
            mediainfo=mediainfo,
            target_storage=target_storage,
            target_path=target_file,
            transfer_type=transfer_type,
            options={"bluray_remux": True},
        )
        event = eventmanager.send_event(ChainEventType.TransferIntercept, event_data)
        if event and event.event_data:
            event_data = event.event_data
            if event_data.cancel:
                return TransferInfo(
                    success=False,
                    message=event_data.reason,
                    fileitem=fileitem,
                    fail_list=[fileitem.path],
                    transfer_type=transfer_type,
                    need_notify=need_notify,
                )

        source_files, source_size = self.__get_bluray_remux_sources(
            bluray_root,
            allow_largest_fallback=transfer_type == "copy",
        )
        if not source_files:
            return TransferInfo(
                success=False,
                message=f"蓝光原盘 {bluray_root} 未找到可转封装的视频分片",
                fileitem=fileitem,
                fail_list=[fileitem.path],
                transfer_type=transfer_type,
                need_notify=need_notify,
            )
        target_lock = self.__get_bluray_remux_target_lock(target_file)
        with target_lock:
            target_item = target_oper.get_item(target_file)
            target_size = target_item.size if target_item else None
            if target_size is None and target_file.is_file():
                target_size = target_file.stat().st_size
            allow_target_replace = bool(
                target_file.exists()
                or target_file.is_symlink()
                or target_item
            )
            (
                can_overwrite,
                errmsg,
                recheck_output_size,
            ) = self.__check_bluray_remux_overwrite(
                fileitem=fileitem,
                target_oper=target_oper,
                target_file=target_file,
                target_storage=target_storage,
                transfer_type=transfer_type,
                overwrite_mode=overwrite_mode,
                source_size=source_size,
            )
            if not can_overwrite:
                return TransferInfo(
                    success=False,
                    message=errmsg,
                    fileitem=fileitem,
                    target_diritem=target_diritem,
                    fail_list=[fileitem.path],
                    transfer_type=transfer_type,
                    need_notify=need_notify,
                )

            logger.info(f"正在转封装蓝光原盘：{bluray_root} 到 {target_file}")
            state, errmsg = self.__run_bluray_remux(
                source_files,
                target_file,
                allow_target_replace=allow_target_replace,
                recheck_target_size=target_size if recheck_output_size else None,
            )
            if not state:
                logger.error(f"蓝光原盘 {fileitem.path} 转封装失败：{errmsg}")
                return TransferInfo(
                    success=False,
                    message=errmsg,
                    fileitem=fileitem,
                    fail_list=[fileitem.path],
                    transfer_type=transfer_type,
                    need_notify=need_notify,
                )
            if overwrite_mode == "latest":
                self.__delete_version_files(target_oper, target_file)

        if transfer_type == "move" and not source_oper.delete(fileitem):
            return TransferInfo(
                success=False,
                message="转封装成功但删除源目录失败",
                fileitem=fileitem,
                target_diritem=target_diritem,
                fail_list=[fileitem.path],
                transfer_type=transfer_type,
                need_notify=need_notify,
            )

        target_item = target_oper.get_item(target_file)
        if not target_item:
            target_item = self.__build_local_fileitem(target_file)
        logger.info(f"蓝光原盘 {fileitem.path} 转封装整理成功")
        return TransferInfo(
            success=True,
            fileitem=fileitem,
            target_item=target_item,
            target_diritem=target_diritem,
            need_scrape=need_scrape,
            need_notify=need_notify,
            transfer_type=transfer_type,
            file_list=[fileitem.path],
            file_list_new=[target_item.path],
            file_count=1,
            total_size=target_item.size or 0,
        )

    def transfer_media(
        self,
        fileitem: FileItem,
        in_meta: MetaBase,
        mediainfo: MediaInfo,
        target_storage: str,
        target_path: Path,
        transfer_type: str,
        source_oper: StorageBase,
        target_oper: StorageBase,
        need_scrape: Optional[bool] = False,
        need_rename: Optional[bool] = True,
        need_notify: Optional[bool] = True,
        overwrite_mode: Optional[str] = None,
        episodes_info: List[TmdbEpisode] = None,
        preview: Optional[bool] = False,
    ) -> TransferInfo:
        """
        识别并整理一个文件或者一个目录下的所有文件
        :param fileitem: 整理的文件对象，可能是一个文件也可以是一个目录
        :param in_meta：预识别元数据
        :param mediainfo: 媒体信息
        :param target_storage: 目标存储
        :param target_path: 目标路径
        :param transfer_type: 文件整理方式
        :param source_oper: 源存储操作对象
        :param target_oper: 目标存储操作对象
        :param need_scrape: 是否需要刮削
        :param need_rename: 是否需要重命名
        :param need_notify: 是否需要通知
        :param overwrite_mode: 覆盖模式
        :param episodes_info: 当前季的全部集信息
        :param preview: 是否仅预览
        :return: TransferInfo、错误信息
        """

        def __is_subtitle_file(_fileitem: FileItem) -> bool:
            """
            判断是否为字幕文件
            :param _fileitem: 文件项
            :return: True/False
            """
            if not _fileitem.extension:
                return False
            if f".{_fileitem.extension.lower()}" in settings.RMT_SUBEXT:
                return True
            return False

        def __is_extra_file(_fileitem: FileItem) -> bool:
            """
            判断是否为附加文件
            :param _fileitem: 文件项
            :return: True/False
            """
            if not _fileitem.extension:
                return False
            if f".{_fileitem.extension.lower()}" in (
                settings.RMT_SUBEXT + settings.RMT_AUDIOEXT
            ):
                return True
            return False

        # 整理结果
        result = TransferInfo()

        try:
            # 重命名格式
            rename_format = settings.RENAME_FORMAT(mediainfo.type)

            # 判断是否为文件夹
            if fileitem.type == "dir":
                # 整理整个目录，一般为蓝光原盘
                if need_rename:
                    rendered_path = self.get_rename_path(
                        path=target_path,
                        template_string=rename_format,
                        rename_dict=self.get_naming_dict(
                            meta=in_meta, mediainfo=mediainfo
                        ),
                        source_path=fileitem.path,
                        source_item=fileitem,
                    )
                    if mediainfo.type == MediaType.TV:
                        new_path = self.__get_tv_bluray_dir_path(
                            rendered_path=rendered_path,
                            source_item=fileitem,
                            meta=in_meta,
                        )
                    else:
                        new_path = DirectoryHelper.get_media_root_path(
                            rename_format, rename_path=rendered_path
                        )
                    if not new_path:
                        self.__update_result(
                            result=result,
                            success=False,
                            message="重命名格式无效",
                            fileitem=fileitem,
                            transfer_type=transfer_type,
                            need_notify=need_notify,
                        )
                        return result
                else:
                    new_path = target_path / fileitem.name
                if preview:
                    preview_diritem = self.__build_preview_item(
                        storage=target_storage,
                        path=new_path,
                        item_type="dir",
                    )
                    self.__update_result(
                        result=result,
                        success=True,
                        fileitem=fileitem,
                        target_item=preview_diritem,
                        target_diritem=preview_diritem,
                        file_list=[fileitem.path],
                        file_list_new=[new_path.as_posix()],
                        need_scrape=need_scrape,
                        need_notify=False,
                        transfer_type=transfer_type,
                    )
                    return result
                # 原盘大小只计算STREAM目录内的文件大小
                if stream_fileitem := source_oper.get_item(
                    Path(fileitem.path) / "BDMV" / "STREAM"
                ):
                    fileitem.size = sum(
                        file.size for file in source_oper.list(stream_fileitem) or []
                    )
                # 可选：将本地蓝光原盘转封装为单个 MKV 文件
                bluray_remux_result = self.__transfer_bluray_remux(
                    fileitem=fileitem,
                    in_meta=in_meta,
                    mediainfo=mediainfo,
                    target_storage=target_storage,
                    target_path=target_path,
                    transfer_type=transfer_type,
                    source_oper=source_oper,
                    target_oper=target_oper,
                    rename_format=rename_format,
                    overwrite_mode=overwrite_mode,
                    need_scrape=need_scrape,
                    need_rename=need_rename,
                    need_notify=need_notify,
                )
                if bluray_remux_result:
                    return bluray_remux_result
                # 整理目录
                new_diritem, errmsg = self.__transfer_dir(
                    fileitem=fileitem,
                    mediainfo=mediainfo,
                    source_oper=source_oper,
                    target_oper=target_oper,
                    target_storage=target_storage,
                    target_path=new_path,
                    transfer_type=transfer_type,
                    result=result,
                )
                if not new_diritem:
                    logger.error(f"文件夹 {fileitem.path} 整理失败：{errmsg}")
                    self.__update_result(
                        result=result,
                        success=False,
                        message=errmsg,
                        fileitem=fileitem,
                        transfer_type=transfer_type,
                        need_notify=need_notify,
                    )
                    return result

                logger.info(f"文件夹 {fileitem.path} 整理成功")
                # 返回整理后的路径
                self.__update_result(
                    result=result,
                    success=True,
                    fileitem=fileitem,
                    target_item=new_diritem,
                    target_diritem=new_diritem,
                    need_scrape=need_scrape,
                    need_notify=need_notify,
                    transfer_type=transfer_type,
                )
                return result
            else:
                # 整理单个文件
                if mediainfo.type == MediaType.TV:
                    # 电视剧
                    if in_meta.begin_episode is None:
                        logger.warn(f"文件 {fileitem.path} 整理失败：未识别到文件集数")
                        self.__update_result(
                            result=result,
                            success=False,
                            message="未识别到文件集数",
                            fileitem=fileitem,
                            fail_list=[fileitem.path],
                            transfer_type=transfer_type,
                            need_notify=need_notify,
                        )
                        return result

                    # 文件结束季为空
                    in_meta.end_season = None
                    # 文件总季数为1
                    if in_meta.total_season:
                        in_meta.total_season = 1
                    # 文件不可能超过2集
                    if in_meta.total_episode > 2:
                        in_meta.total_episode = 1
                        in_meta.end_episode = None

                # 目的文件名
                if need_rename:
                    new_file = self.get_rename_path(
                        path=target_path,
                        template_string=rename_format,
                        rename_dict=self.get_naming_dict(
                            meta=in_meta,
                            mediainfo=mediainfo,
                            episodes_info=episodes_info,
                            file_ext=f".{fileitem.extension}",
                        ),
                        source_path=fileitem.path,
                        source_item=fileitem,
                    )

                    # 针对字幕文件，文件名中补充额外标识信息
                    if __is_subtitle_file(fileitem):
                        new_file = self.__rename_subtitles(fileitem, new_file)

                    # 文件目录
                    folder_path = DirectoryHelper.get_media_root_path(
                        rename_format, rename_path=new_file
                    )
                    if not folder_path:
                        self.__update_result(
                            result=result,
                            success=False,
                            message="重命名格式无效",
                            fileitem=fileitem,
                            fail_list=[fileitem.path],
                            transfer_type=transfer_type,
                            need_notify=need_notify,
                        )
                        return result
                else:
                    new_file = target_path / fileitem.name
                    folder_path = target_path

                # 目标目录
                if preview:
                    # 预览只做路径推算，不检查目录或同名文件冲突，避免目标存储探测触发真实整理。
                    target_diritem = self.__build_preview_item(
                        storage=target_storage,
                        path=folder_path,
                        item_type="dir",
                    )
                    target_item = self.__build_preview_item(
                        storage=target_storage,
                        path=new_file,
                        item_type="file",
                        size=fileitem.size,
                    )
                    self.__update_result(
                        result=result,
                        success=True,
                        fileitem=fileitem,
                        target_item=target_item,
                        target_diritem=target_diritem,
                        file_list=[fileitem.path],
                        file_list_new=[new_file.as_posix()],
                        file_count=1,
                        total_size=fileitem.size or 0,
                        need_scrape=need_scrape,
                        transfer_type=transfer_type,
                        need_notify=False,
                    )
                    return result

                target_diritem = target_oper.get_folder(folder_path)
                if not target_diritem:
                    logger.error(f"目标目录 {folder_path} 获取失败")
                    self.__update_result(
                        result=result,
                        success=False,
                        message=f"目标目录 {folder_path} 获取失败",
                        fileitem=fileitem,
                        fail_list=[fileitem.path],
                        transfer_type=transfer_type,
                        need_notify=need_notify,
                    )
                    return result

                # 判断是否要覆盖，附加文件强制覆盖
                overflag = False
                if not __is_extra_file(fileitem):
                    # 目标文件
                    target_item = target_oper.get_item(new_file)
                    if target_item:
                        # 目标文件已存在
                        target_file = new_file
                        if target_storage == "local" and new_file.is_symlink():
                            target_file = new_file.readlink()
                            if not target_file.exists():
                                overflag = True
                        if not overflag:
                            # 目标文件已存在
                            logger.info(
                                f"目的文件系统中已经存在同名文件 {target_file}，当前整理覆盖模式设置为 {overwrite_mode}"
                            )
                            # 触发覆盖检查事件，允许插件提供源/目标文件真实大小
                            # 或直接给出覆盖决策（例如 .strm 文件指向网盘原始文件）
                            overwrite_event_data = TransferOverwriteCheckEventData(
                                fileitem=fileitem,
                                target_item=target_item,
                                target_storage=target_storage,
                                target_path=new_file,
                                overwrite_mode=overwrite_mode or "",
                                transfer_type=transfer_type,
                            )
                            overwrite_event = eventmanager.send_event(
                                ChainEventType.TransferOverwriteCheck,
                                overwrite_event_data,
                            )
                            plugin_overwrite: Optional[bool] = None
                            plugin_source_size: Optional[int] = None
                            plugin_target_size: Optional[int] = None
                            if overwrite_event and overwrite_event.event_data:
                                overwrite_event_data = overwrite_event.event_data
                                plugin_overwrite = overwrite_event_data.overwrite
                                plugin_source_size = overwrite_event_data.source_size
                                plugin_target_size = overwrite_event_data.target_size
                                if (
                                    plugin_overwrite is not None
                                    or plugin_source_size is not None
                                    or plugin_target_size is not None
                                ):
                                    logger.info(
                                        f"覆盖检查事件由 {overwrite_event_data.source} 处理："
                                        f"overwrite={plugin_overwrite}, "
                                        f"source_size={plugin_source_size}, "
                                        f"target_size={plugin_target_size}, "
                                        f"reason={overwrite_event_data.reason}"
                                    )
                            if plugin_overwrite is True:
                                overflag = True
                            elif plugin_overwrite is False:
                                self.__update_result(
                                    result=result,
                                    success=False,
                                    message=overwrite_event_data.reason
                                    or "插件决定不覆盖已有文件",
                                    fileitem=fileitem,
                                    target_item=target_item,
                                    target_diritem=target_diritem,
                                    fail_list=[fileitem.path],
                                    transfer_type=transfer_type,
                                    need_notify=need_notify,
                                )
                                return result
                            elif overwrite_mode == "always":
                                # 总是覆盖同名文件
                                overflag = True
                            elif overwrite_mode == "size":
                                # 存在时大覆盖小
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
                                    logger.info(
                                        f"目标文件文件大小更小，将覆盖：{new_file}"
                                    )
                                    overflag = True
                                else:
                                    self.__update_result(
                                        result=result,
                                        success=False,
                                        message=f"媒体库存在同名文件，且质量更好",
                                        fileitem=fileitem,
                                        target_item=target_item,
                                        target_diritem=target_diritem,
                                        fail_list=[fileitem.path],
                                        transfer_type=transfer_type,
                                        need_notify=need_notify,
                                    )
                                    return result
                            elif overwrite_mode == "never":
                                # 存在不覆盖
                                self.__update_result(
                                    result=result,
                                    success=False,
                                    message=f"媒体库存在同名文件，当前覆盖模式为不覆盖",
                                    fileitem=fileitem,
                                    target_item=target_item,
                                    target_diritem=target_diritem,
                                    fail_list=[fileitem.path],
                                    transfer_type=transfer_type,
                                    need_notify=need_notify,
                                )
                                return result
                            elif overwrite_mode == "latest":
                                # 仅保留最新版本
                                logger.info(
                                    f"当前整理覆盖模式设置为仅保留最新版本，将覆盖：{new_file}"
                                )
                                overflag = True
                    else:
                        if overwrite_mode == "latest":
                            # 文件不存在，但仅保留最新版本
                            logger.info(
                                f"当前整理覆盖模式设置为 {overwrite_mode}，仅保留最新版本，正在删除已有版本文件 ..."
                            )
                            self.__delete_version_files(target_oper, new_file)
                else:
                    # 附加文件 总是需要覆盖
                    overflag = True

                # 整理文件
                new_item, err_msg = self.__transfer_file(
                    fileitem=fileitem,
                    meta=in_meta,
                    mediainfo=mediainfo,
                    target_storage=target_storage,
                    target_file=new_file,
                    transfer_type=transfer_type,
                    over_flag=overflag,
                    source_oper=source_oper,
                    target_oper=target_oper,
                    result=result,
                )
                if not new_item:
                    err_msg = err_msg or f"{fileitem.path} 整理后未获取到目标文件信息"
                    logger.error(f"文件 {fileitem.path} 整理失败：{err_msg}")
                    self.__update_result(
                        result=result,
                        success=False,
                        message=err_msg,
                        fileitem=fileitem,
                        fail_list=[fileitem.path],
                        transfer_type=transfer_type,
                        need_notify=need_notify,
                    )
                    return result

                logger.info(f"文件 {fileitem.path} 整理成功")
                self.__update_result(
                    result=result,
                    success=True,
                    fileitem=fileitem,
                    target_item=new_item,
                    target_diritem=target_diritem,
                    need_scrape=need_scrape,
                    transfer_type=transfer_type,
                    need_notify=need_notify,
                )
                return result
        except Exception as e:
            logger.error(f"媒体整理出错：{e}")
            return TransferInfo(success=False, message=str(e))

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
            (settings.DEFAULT_SUB == "zh-cn" and new_file_type == ".chi.zh-cn")
            or (settings.DEFAULT_SUB == "zh-tw" and new_file_type == ".zh-tw")
            or (settings.DEFAULT_SUB == "ja" and new_file_type == ".ja")
            or (settings.DEFAULT_SUB == "eng" and new_file_type == ".eng")
        ):
            new_sub_tag = ".default" + new_file_type
        else:
            new_sub_tag = new_file_type

        return new_file.with_name(new_file.stem + new_sub_tag + file_ext)

    def __transfer_dir(
        self,
        fileitem: FileItem,
        mediainfo: MediaInfo,
        source_oper: StorageBase,
        target_oper: StorageBase,
        transfer_type: str,
        target_storage: str,
        target_path: Path,
        result: TransferInfo,
    ) -> Tuple[Optional[FileItem], str]:
        """
        整理整个文件夹
        :param fileitem: 源文件
        :param mediainfo: 媒体信息
        :param source_oper: 源存储操作对象
        :param target_oper: 目标存储操作对象
        :param transfer_type: 整理方式
        :param target_storage: 目标存储
        :param target_path: 目标路径
        """
        logger.info(f"正在整理目录：{fileitem.path} 到 {target_path}")
        target_item = target_oper.get_folder(target_path)
        if not target_item:
            return None, f"获取目标目录失败：{target_path}"
        event_data = TransferInterceptEventData(
            fileitem=fileitem,
            mediainfo=mediainfo,
            target_storage=target_storage,
            target_path=target_path,
            transfer_type=transfer_type,
        )
        event = eventmanager.send_event(ChainEventType.TransferIntercept, event_data)
        if event and event.event_data:
            event_data = event.event_data
            # 如果事件被取消，跳过文件整理
            if event_data.cancel:
                logger.debug(
                    f"Transfer dir canceled by event: {event_data.source},"
                    f"Reason: {event_data.reason}"
                )
                return None, event_data.reason
        # 处理所有文件
        state, errmsg = self.__transfer_dir_files(
            fileitem=fileitem,
            target_storage=target_storage,
            source_oper=source_oper,
            target_oper=target_oper,
            target_path=target_path,
            transfer_type=transfer_type,
            result=result,
        )
        if state:
            return target_item, errmsg
        else:
            return None, errmsg

    def __transfer_dir_files(
        self,
        fileitem: FileItem,
        target_storage: str,
        source_oper: StorageBase,
        target_oper: StorageBase,
        transfer_type: str,
        target_path: Path,
        result: TransferInfo,
    ) -> Tuple[bool, str]:
        """
        按目录结构整理目录下所有文件
        :param fileitem: 源文件
        :param target_storage: 目标存储
        :param source_oper: 源存储操作对象
        :param target_oper: 目标存储操作对象
        :param target_path: 目标路径
        :param transfer_type: 整理方式
        """
        file_list: List[FileItem] = source_oper.list(fileitem)
        # 整理文件
        for item in file_list:
            if item.type == "dir":
                # 递归整理目录
                new_path = target_path / item.name
                state, errmsg = self.__transfer_dir_files(
                    fileitem=item,
                    target_storage=target_storage,
                    source_oper=source_oper,
                    target_oper=target_oper,
                    transfer_type=transfer_type,
                    target_path=new_path,
                    result=result,
                )
                if not state:
                    return False, errmsg
            else:
                # 整理文件
                new_file = target_path / item.name
                new_item, errmsg = self.__transfer_command(
                    fileitem=item,
                    target_storage=target_storage,
                    source_oper=source_oper,
                    target_oper=target_oper,
                    target_file=new_file,
                    transfer_type=transfer_type,
                )
                if not new_item:
                    return False, errmsg
                self.__update_result(
                    result=result,
                    file_list=[item.path],
                    file_list_new=[new_item.path],
                )
        # 返回成功
        return True, ""

    def __transfer_file(
        self,
        fileitem: FileItem,
        meta: Optional[MetaBase],
        mediainfo: MediaInfo,
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
        :param meta: 元数据
        :param mediainfo: 媒体信息
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
        event_data = TransferInterceptEventData(
            fileitem=fileitem,
            meta=meta,
            mediainfo=mediainfo,
            target_storage=target_storage,
            target_path=target_file,
            transfer_type=transfer_type,
            options={"over_flag": over_flag},
        )
        event = eventmanager.send_event(ChainEventType.TransferIntercept, event_data)
        if event and event.event_data:
            event_data = event.event_data
            # 如果事件被取消，跳过文件整理
            if event_data.cancel:
                logger.debug(
                    f"Transfer file canceled by event: {event_data.source},"
                    f"Reason: {event_data.reason}"
                )
                return None, event_data.reason
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
        return TemplateHelper().builder.build(
            meta=meta,
            mediainfo=mediainfo,
            file_extension=file_ext,
            episodes_info=episodes_info,
        )

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
            if f".{media_file.extension.lower()}" not in settings.RMT_MEDIAEXT:
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
