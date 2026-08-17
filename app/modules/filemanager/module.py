from pathlib import Path
from typing import Any, Optional, List, Tuple, Union, Dict, Callable

from app.runtime.config import settings
from app.domain.context import MediaInfo, MusicInfo
from app.domain.meta.metabase import MetaBase
from app.domain.meta.metamusic import MetaMusic
from app.domain.metainfo import MetaInfo
from app.application.directory import DirectoryHelper
from app.application.messaging.message import MessageHelper
from app.foundation.reflection import ModuleHelper
from app.runtime.log import logger
from app.modules import _ModuleBase
from app.modules.filemanager.storages import StorageBase
from app.modules.filemanager.transhandler import TransHandler
from app.schemas.transfer import TransferInfo
from app.schemas.mediaserver import ExistMediaInfo
from app.schemas.tmdb import TmdbEpisode
from app.schemas.system import TransferDirectoryConf
from app.schemas.workflow import FileItem
from app.schemas.file import StorageUsage
from app.schemas.types import MUSIC_ENTITY_ALBUM, MediaType, ModuleType, OtherModulesType, StorageAction
from app.adapters.system.host import SystemUtils
from app.foundation import text as text_tools


class FileManagerModule(_ModuleBase):
    """
    文件整理模块
    """

    _storage_schemas = []
    _support_storages = []

    def __init__(self):
        super().__init__()
        self.directoryhelper = DirectoryHelper()
        self.messagehelper = MessageHelper()

    def init_module(self) -> None:
        """初始化文件整理模块支持的存储实现"""
        # 加载模块
        self._storage_schemas = ModuleHelper.load('app.modules.filemanager.storages',
                                                  filter_func=lambda _, obj: hasattr(obj, 'schema') and obj.schema)
        # 获取存储类型
        self._support_storages = [storage.schema.value for storage in self._storage_schemas if storage.schema]

    @staticmethod
    def get_name() -> str:
        """获取模块名称"""
        return "文件整理"

    @staticmethod
    def get_type() -> ModuleType:
        """
        获取模块类型
        """
        return ModuleType.Other

    @staticmethod
    def get_subtype() -> OtherModulesType:
        """
        获取模块子类型
        """
        return OtherModulesType.FileManager

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效
        """
        return 4

    def stop(self):
        """停止文件整理模块"""
        pass

    def test(self) -> Tuple[bool, str]:
        """
        测试模块连接性
        """
        # 检查目录
        dirs = self.directoryhelper.get_dirs()
        if not dirs:
            return False, "未设置任何目录"
        for d in dirs:
            # 下载目录
            download_path = d.download_path
            if not download_path:
                return False, f"{d.name} 的下载目录未设置"
            if d.storage == "local" and not Path(download_path).exists():
                return False, f"{d.name} 的下载目录 {download_path} 不存在"
            # 仅在启用整理时检查媒体库目录
            library_path = d.library_path
            if d.transfer_type:
                if not library_path:
                    return False, f"{d.name} 的媒体库目录未设置"
                if d.library_storage == "local" and not Path(library_path).exists():
                    return False, f"{d.name} 的媒体库目录 {library_path} 不存在"
                # 硬链接
                if d.transfer_type == "link" \
                        and d.storage == "local" \
                        and d.library_storage == "local" \
                        and not SystemUtils.is_same_disk(Path(download_path), Path(library_path)):
                    return False, f"{d.name} 的下载目录 {download_path} 与媒体库目录 {library_path} 不在同一磁盘，无法硬链接"
            # 存储
            storage_oper = self.__get_storage_oper(d.storage)
            if storage_oper:
                if not storage_oper.check():
                    return False, f"{d.name} 的存储测试不通过"
                if d.transfer_type and d.transfer_type not in storage_oper.support_transtype():
                    return False, f"{d.name} 的存储不支持 {d.transfer_type} 整理方式"

        return True, ""

    def __get_storage_oper(self, _storage: str, _func: Optional[str] = None) -> Optional[StorageBase]:
        """
        获取存储操作对象
        """
        for storage_schema in self._storage_schemas:
            if storage_schema.schema \
                    and storage_schema.schema.value == _storage \
                    and (not _func or hasattr(storage_schema, _func)):
                return storage_schema()
        return None

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        pass

    def storage_manage(self, storage: str, action: StorageAction, **params) -> Dict[str, Any]:
        """
        网盘存储统一管理入口，按存储标识路由

        动作语义与参数解释交给具体存储实现，
        统一返回 {"success": bool, "message": ..., "data": ...}
        """
        try:
            action = StorageAction(action)
        except ValueError:
            return {"success": False, "message": f"不支持的存储管理动作：{action}"}
        if storage not in self._support_storages:
            return {"success": False, "message": f"不支持的存储类型：{storage}"}

        if action == StorageAction.SAVE_CONFIG:
            storage_oper = self.__get_storage_oper(storage)
            if not storage_oper:
                return {"success": False, "message": f"不支持 {storage} 的配置保存"}
            storage_oper.set_config(params.get("conf") or {})
            return {"success": True}
        if action == StorageAction.RESET_CONFIG:
            storage_oper = self.__get_storage_oper(storage)
            if not storage_oper:
                return {"success": False, "message": f"不支持 {storage} 的重置存储配置"}
            storage_oper.reset_config()
            return {"success": True}
        if action == StorageAction.SUPPORT_TRANSTYPE:
            storage_oper = self.__get_storage_oper(storage)
            if not storage_oper:
                return {"success": False, "message": f"不支持 {storage} 的整理方式获取"}
            # 与旧契约一致：返回值包装为 transtype，空结果同样返回成功空结构
            return {"success": True, "data": {"transtype": storage_oper.support_transtype() or {}}}
        if action == StorageAction.USAGE:
            storage_oper = self.__get_storage_oper(storage)
            if not storage_oper:
                return {"success": False, "message": f"不支持 {storage} 的存储使用情况"}
            # 实现返回 pydantic 模型，转为 dict 后才能透过通用响应的开放映射校验
            return {"success": True, "data": (storage_oper.usage() or StorageUsage()).model_dump()}

        # 登录类动作：存储实现不支持时返回失败信息
        oper_method = action.value
        storage_oper = self.__get_storage_oper(storage, oper_method)
        if not storage_oper:
            return {"success": False, "message": f"{storage} 不支持 {oper_method}"}
        result = getattr(storage_oper, oper_method)(**params)
        if result is None:
            return {"success": False, "message": f"{storage} 的 {oper_method} 执行失败"}
        data, errmsg = result
        return {"success": bool(data), "message": errmsg, "data": data}

    @staticmethod
    def recommend_name(meta: MetaBase, mediainfo: MediaInfo,
                       episodes_info: Optional[List[TmdbEpisode]] = None) -> Optional[str]:
        """
        获取重命名后的名称
        :param meta: 元数据
        :param mediainfo: 媒体信息
        :param episodes_info: 集信息，由调用方链层预先获取
        :return: 重命名后的名称（含目录）
        """
        handler = TransHandler()
        # 重命名格式
        rename_format = settings.RENAME_FORMAT(mediainfo.type)
        # 获取重命名后的名称
        path = handler.get_rename_path(
            template_string=rename_format,
            rename_dict=handler.get_naming_dict(meta=meta,
                                                mediainfo=mediainfo,
                                                episodes_info=episodes_info,
                                                file_ext=Path(meta.title).suffix)
        )
        return path.as_posix() if path else ""

    def list_files(self, fileitem: FileItem, recursion: Optional[bool] = False) -> Optional[List[FileItem]]:
        """
        浏览文件
        :param fileitem: 源文件
        :param recursion: 是否递归，此时只浏览文件
        :return: 文件项列表
        """
        if fileitem.storage not in self._support_storages:
            return None
        storage_oper = self.__get_storage_oper(fileitem.storage)
        if not storage_oper:
            logger.error(f"不支持 {fileitem.storage} 的文件浏览")
            return None

        def __get_files(_item: FileItem, _r: Optional[bool] = False):
            """
            递归处理
            """
            _items = storage_oper.list(_item)
            if _items:
                if _r:
                    for t in _items:
                        if t.type == "dir":
                            __get_files(t, _r)
                        else:
                            result.append(t)
                else:
                    result.extend(_items)

        # 返回结果
        result = []
        __get_files(fileitem, recursion)

        return result

    def any_files(self, fileitem: FileItem, extensions: list = None) -> Optional[bool]:
        """
        查询当前目录下是否存在指定扩展名任意文件
        """
        if fileitem.storage not in self._support_storages:
            return None
        storage_oper = self.__get_storage_oper(fileitem.storage)
        if not storage_oper:
            logger.error(f"不支持 {fileitem.storage} 的文件浏览")
            return None

        def __any_file(_item: FileItem):
            """
            递归处理
            """
            _items = storage_oper.list(_item)
            if _items:
                if not extensions:
                    return True
                for t in _items:
                    if (t.type == "file"
                            and t.extension
                            and f".{t.extension.lower()}" in extensions):
                        return True
                    elif t.type == "dir":
                        if __any_file(t):
                            return True
            return False

        # 返回结果
        return __any_file(fileitem)

    def create_folder(self, fileitem: FileItem, name: str) -> Optional[FileItem]:
        """
        创建目录
        :param fileitem: 源文件
        :param name: 目录名
        :return: 创建的目录
        """
        if fileitem.storage not in self._support_storages:
            return None
        storage_oper = self.__get_storage_oper(fileitem.storage)
        if not storage_oper:
            logger.error(f"不支持 {fileitem.storage} 的目录创建")
            return None
        return storage_oper.create_folder(fileitem, name)

    def get_folder(self, storage: str, path: Path) -> Optional[FileItem]:
        """
        获取目录，如目录不存在则创建
        """
        if storage not in self._support_storages:
            return None
        storage_oper = self.__get_storage_oper(storage)
        if not storage_oper:
            logger.error(f"不支持 {storage} 的目录获取")
            return None
        return storage_oper.get_folder(path)

    def delete_file(self, fileitem: FileItem) -> Optional[bool]:
        """
        删除文件或目录
        """
        if fileitem.storage not in self._support_storages:
            return None
        storage_oper = self.__get_storage_oper(fileitem.storage)
        if not storage_oper:
            logger.error(f"不支持 {fileitem.storage} 的删除处理")
            return False
        return storage_oper.delete(fileitem)

    def rename_file(self, fileitem: FileItem, name: str) -> Optional[bool]:
        """
        重命名文件或目录
        """
        if fileitem.storage not in self._support_storages:
            return None
        storage_oper = self.__get_storage_oper(fileitem.storage)
        if not storage_oper:
            logger.error(f"不支持 {fileitem.storage} 的重命名处理")
            return False
        return storage_oper.rename(fileitem, name)

    def download_file(self, fileitem: FileItem, path: Path = None) -> Optional[Path]:
        """
        下载文件
        """
        if fileitem.storage not in self._support_storages:
            return None
        storage_oper = self.__get_storage_oper(fileitem.storage)
        if not storage_oper:
            logger.error(f"不支持 {fileitem.storage} 的下载处理")
            return None
        return storage_oper.download(fileitem, path=path)

    def upload_file(self, fileitem: FileItem, path: Path, new_name: Optional[str] = None) -> Optional[FileItem]:
        """
        上传文件
        """
        if fileitem.storage not in self._support_storages:
            return None
        storage_oper = self.__get_storage_oper(fileitem.storage)
        if not storage_oper:
            logger.error(f"不支持 {fileitem.storage} 的上传处理")
            return None
        return storage_oper.upload(fileitem, path, new_name)

    def get_file_item(self, storage: str, path: Path) -> Optional[FileItem]:
        """
        根据路径获取文件项
        """
        if storage not in self._support_storages:
            return None
        storage_oper = self.__get_storage_oper(storage)
        if not storage_oper:
            logger.error(f"不支持 {storage} 的文件获取")
            return None
        return storage_oper.get_item(path)

    def get_parent_item(self, fileitem: FileItem) -> Optional[FileItem]:
        """
        获取上级目录项
        """
        if fileitem.storage not in self._support_storages:
            return None
        storage_oper = self.__get_storage_oper(fileitem.storage)
        if not storage_oper:
            logger.error(f"不支持 {fileitem.storage} 的文件获取")
            return None
        return storage_oper.get_parent(fileitem)

    def snapshot_storage(self, storage: str, path: Path,
                         last_snapshot_time: float = None, max_depth: int = 5,
                         previous_snapshot: Optional[Dict[str, Dict]] = None) -> Optional[Dict[str, Dict]]:
        """
        快照存储
        :param storage: 存储类型
        :param path: 路径
        :param last_snapshot_time: 上次快照时间，用于增量快照
        :param max_depth: 最大递归深度，避免过深遍历
        :param previous_snapshot: 上次完整快照，用于增量对账
        """
        if storage not in self._support_storages:
            return None
        storage_oper = self.__get_storage_oper(storage)
        if not storage_oper:
            logger.error(f"不支持 {storage} 的快照处理")
            return None
        return storage_oper.snapshot(
            path,
            last_snapshot_time=last_snapshot_time,
            max_depth=max_depth,
            previous_snapshot=previous_snapshot
        )

    def transfer(self, fileitem: FileItem, meta: MetaBase, mediainfo: MediaInfo,
                 target_directory: TransferDirectoryConf = None,
                 target_storage: Optional[str] = None, target_path: Path = None,
                 transfer_type: Optional[str] = None, scrape: Optional[bool] = None,
                 library_type_folder: Optional[bool] = None, library_category_folder: Optional[bool] = None,
                 episodes_info: List[TmdbEpisode] = None,
                 source_oper: Callable = None, target_oper: Callable = None,
                 preview: Optional[bool] = False) -> TransferInfo:
        """
        文件整理
        :param fileitem:  文件信息
        :param meta: 预识别的元数据
        :param mediainfo:  识别的媒体信息
        :param target_directory:  目标目录配置
        :param target_storage:  目标存储
        :param target_path:  目标路径
        :param transfer_type:  转移模式
        :param scrape: 是否刮削元数据
        :param library_type_folder: 是否按媒体类型创建目录
        :param library_category_folder: 是否按媒体类别创建目录
        :param episodes_info: 当前季的全部集信息
        :param source_oper: 源存储操作对象
        :param target_oper: 目标存储操作对象
        :return: {path, target_path, message}
        """
        handler = TransHandler()
        # 检查目录路径
        if fileitem.storage == "local" and not Path(fileitem.path).exists():
            return TransferInfo(success=False,
                                fileitem=fileitem,
                                message=f"{fileitem.path} 不存在")
        # 目标路径不能是文件
        if target_path and target_path.is_file():
            logger.error(f"整理目标路径 {target_path} 是一个文件")
            return TransferInfo(success=False,
                                fileitem=fileitem,
                                message=f"{target_path} 不是有效目录")
        # 获取目标路径
        if target_directory:
            # 目标媒体库目录未设置
            if not target_directory.library_path:
                logger.error(f"目标媒体库目录未设置，无法整理文件，源路径：{fileitem.path}")
                return TransferInfo(success=False,
                                    fileitem=fileitem,
                                    message="目标媒体库目录未设置")
            # 整理方式
            if not transfer_type:
                transfer_type = target_directory.transfer_type
            # 目标存储
            if not target_storage:
                target_storage = target_directory.library_storage
            # 是否需要重命名
            need_rename = target_directory.renaming
            # 是否需要通知
            need_notify = target_directory.notify
            # 覆盖模式
            overwrite_mode = target_directory.overwrite_mode
            # 是否需要刮削
            need_scrape = target_directory.scraping if scrape is None else scrape
            # 拼装媒体库一、二级子目录
            target_path = handler.get_dest_dir(mediainfo=mediainfo, target_dir=target_directory,
                                               need_type_folder=library_type_folder,
                                               need_category_folder=library_category_folder)
        elif target_path:
            need_scrape = scrape or False
            need_rename = True
            need_notify = False
            overwrite_mode = "never"
            # 手动整理的场景，有自定义目标路径
            target_path = handler.get_dest_path(mediainfo=mediainfo, target_path=target_path,
                                                need_type_folder=library_type_folder,
                                                need_category_folder=library_category_folder)
        else:
            # 未找到有效的媒体库目录
            logger.error(
                f"{mediainfo.type.value if mediainfo.type else '未知类型'} {mediainfo.title_year} 未找到有效的媒体库目录，无法整理文件，源路径：{fileitem.path}")
            return TransferInfo(success=False,
                                fileitem=fileitem,
                                message="未找到有效的媒体库目录")
        # 整理方式
        if not transfer_type:
            logger.error(f"{target_directory.name} 未设置整理方式")
            return TransferInfo(success=False,
                                fileitem=fileitem,
                                message=f"{target_directory.name} 未设置整理方式")

        # 源操作对象
        if not source_oper:
            source_oper = self.__get_storage_oper(fileitem.storage)
        if not source_oper:
            return TransferInfo(success=False,
                                message=f"不支持的存储类型：{fileitem.storage}",
                                fileitem=fileitem,
                                fail_list=[fileitem.path],
                                transfer_type=transfer_type,
                                need_notify=need_notify
                                )
        # 目的操作对象
        if not target_oper:
            if not target_storage:
                target_storage = fileitem.storage
            target_oper = self.__get_storage_oper(target_storage)
        if not target_oper:
            return TransferInfo(success=False,
                                message=f"不支持的存储类型：{target_storage}",
                                fileitem=fileitem,
                                fail_list=[fileitem.path],
                                transfer_type=transfer_type,
                                need_notify=need_notify)

        # 整理
        logger.info(f"获取整理目标路径：【{target_storage}】{target_path}")
        return handler.transfer_media(fileitem=fileitem,
                                      in_meta=meta,
                                      mediainfo=mediainfo,
                                      target_storage=target_storage,
                                      target_path=target_path,
                                      transfer_type=transfer_type,
                                      need_scrape=need_scrape,
                                      need_rename=need_rename,
                                      need_notify=need_notify,
                                      overwrite_mode=overwrite_mode,
                                      episodes_info=episodes_info,
                                      preview=preview,
                                      source_oper=source_oper,
                                      target_oper=target_oper)

    @staticmethod
    def _build_library_lookup_meta(
            mediainfo: Union[MediaInfo, MusicInfo],
    ) -> MetaBase:
        """构造标准媒体库路径反查使用的最小元数据。"""
        if mediainfo.type == MediaType.MUSIC:
            music_type = getattr(mediainfo, "music_type", None)
            album = getattr(mediainfo, "album", None)
            if not album and music_type == MUSIC_ENTITY_ALBUM:
                album = mediainfo.title
            return MetaMusic(
                title=mediainfo.title,
                artists=list(getattr(mediainfo, "artists", None) or []),
                album=album,
                album_artist=getattr(mediainfo, "album_artist", None),
                year=mediainfo.year,
                disc_number=getattr(mediainfo, "disc_number", None),
                track_number=getattr(mediainfo, "track_number", None),
                total_tracks=getattr(mediainfo, "total_tracks", None),
                media_source=getattr(mediainfo, "source", None),
                media_id=getattr(mediainfo, "media_id", None),
            )

        meta = MetaInfo(mediainfo.title)
        if meta.type == MediaType.UNKNOWN and mediainfo.type is not None:
            meta.type = mediainfo.type
        if meta.year is None:
            meta.year = mediainfo.year
        if meta.begin_season is None:
            meta.begin_season = 1
        if meta.begin_episode is None:
            meta.begin_episode = 1
        return meta

    @staticmethod
    def _music_file_identity(fileitem: FileItem) -> Tuple[Optional[int], Optional[int], str]:
        """从标准音乐文件路径提取碟号、曲序和归一化曲名。"""
        file_path = Path(fileitem.path or fileitem.name or "")
        file_meta = MetaMusic(
            org_string=file_path.name,
            title=file_path.stem,
        ).apply_path_context(file_path)
        return (
            file_meta.disc_number,
            file_meta.track_number,
            text_tools.normalize_upper(file_meta.title or file_path.stem),
        )

    @classmethod
    def _music_recording_exists(
            cls,
            fileitems: List[FileItem],
            mediainfo: MusicInfo,
    ) -> bool:
        """按曲名和可用曲序判断单曲是否存在，避免专辑内任一文件造成误判。"""
        target_title = text_tools.normalize_upper(mediainfo.title or "")
        target_track = getattr(mediainfo, "track_number", None)
        target_disc = getattr(mediainfo, "disc_number", None)
        if not target_title:
            return False
        for fileitem in fileitems:
            disc_number, track_number, title = cls._music_file_identity(fileitem)
            if title != target_title:
                continue
            if target_track is not None and track_number not in (None, target_track):
                continue
            if target_disc is not None and disc_number not in (None, target_disc):
                continue
            return True
        return False

    @classmethod
    def _music_album_is_complete(
            cls,
            fileitems: List[FileItem],
            total_tracks: Optional[int],
    ) -> bool:
        """按去重后的曲序或曲名判断本地专辑是否达到目标曲目数。"""
        if not fileitems:
            return False
        if not total_tracks:
            return False
        track_identities = {
            (
                disc_number or 1,
                track_number if track_number is not None else title,
            )
            for disc_number, track_number, title in (
                cls._music_file_identity(fileitem) for fileitem in fileitems
            )
            if track_number is not None or title
        }
        return len(track_identities) >= total_tracks

    def media_files(self, mediainfo: Union[MediaInfo, MusicInfo]) -> List[FileItem]:
        """
        获取对应媒体的媒体库文件列表
        :param mediainfo: 媒体信息
        """
        handler = TransHandler()
        ret_fileitems = []
        # 检查本地媒体库
        dest_dirs = DirectoryHelper().get_library_dirs()
        # 检查每一个媒体库目录
        for dest_dir in dest_dirs:
            # 存储
            storage_oper = self.__get_storage_oper(dest_dir.library_storage)
            if not storage_oper:
                continue
            # 媒体分类路径
            dir_path = handler.get_dest_dir(mediainfo=mediainfo, target_dir=dest_dir)
            # 重命名格式
            rename_format = settings.RENAME_FORMAT(mediainfo.type)
            # 元数据补上常用属性，尽可能确保重命名后的路径不出现空白
            meta = self._build_library_lookup_meta(mediainfo)
            # 获取路径（重命名路径）
            target_path = handler.get_rename_path(
                path=dir_path,
                template_string=rename_format,
                rename_dict=handler.get_naming_dict(meta=meta,
                                                    mediainfo=mediainfo)
            )
            # 获取重命名后的媒体文件根路径
            media_path = DirectoryHelper.get_media_root_path(
                rename_format,
                rename_path=target_path,
                media_type=mediainfo.type,
            )
            if not media_path:
                # 忽略
                continue
            if dir_path != media_path and dir_path.is_relative_to(media_path):
                # 兜底检查，避免不必要的扫盘
                logger.warn(f"{media_path} 是媒体库目录 {dir_path} 的父目录，忽略获取媒体文件列表，请检查重命名格式！")
                continue
            # 检索媒体文件
            fileitem = storage_oper.get_item(media_path)
            if not fileitem:
                continue
            try:
                media_files = self.list_files(fileitem, True)
            except Exception as e:
                logger.debug(f"获取媒体文件列表失败：{str(e)}")
                continue
            if media_files:
                media_extensions = (
                    settings.RMT_AUDIOEXT
                    if mediainfo.type == MediaType.MUSIC
                    else settings.RMT_MEDIAEXT
                )
                for media_file in media_files:
                    if (
                            media_file.extension
                            and f".{media_file.extension.lower()}" in media_extensions
                    ):
                        if media_file not in ret_fileitems:
                            ret_fileitems.append(media_file)
        return ret_fileitems

    def media_exists(
            self,
            mediainfo: Union[MediaInfo, MusicInfo],
            **kwargs,
    ) -> Optional[ExistMediaInfo]:
        """
        判断媒体文件是否存在于文件系统（网盘或本地文件），只支持标准媒体库结构
        :param mediainfo:  识别的媒体信息
        :param server:  指定媒体服务器名称时跳过本地文件系统检查
        :return: 如不存在返回None，存在时返回信息，包括每季已存在所有集{type: movie/tv, seasons: {season: [episodes]}}
        """
        if kwargs.get("server"):
            return None

        if not settings.LOCAL_EXISTS_SEARCH:
            return None

        logger.debug(f"正在本地媒体库中查找 {mediainfo.title_year}...")

        # 检查媒体库
        fileitems = self.media_files(mediainfo)
        if not fileitems:
            logger.debug(f"{mediainfo.title_year} 不在本地媒体库中")
            return None

        if mediainfo.type == MediaType.MOVIE:
            # 电影存在任何文件为存在
            logger.info(f"{mediainfo.title_year} 在本地文件系统中找到了")
            return ExistMediaInfo(type=MediaType.MOVIE)
        if mediainfo.type == MediaType.MUSIC:
            if getattr(mediainfo, "music_type", None) == MUSIC_ENTITY_ALBUM:
                exists = self._music_album_is_complete(
                    fileitems,
                    getattr(mediainfo, "total_tracks", None),
                )
            else:
                exists = self._music_recording_exists(fileitems, mediainfo)
            if not exists:
                logger.debug(f"{mediainfo.title_year} 在本地音乐库中尚不完整")
                return None
            logger.info(f"{mediainfo.title_year} 在本地音乐库中找到了")
            return ExistMediaInfo(type=MediaType.MUSIC)
        if mediainfo.type == MediaType.TV:
            # 电视剧检索集数
            seasons: Dict[int, list] = {}
            for fileitem in fileitems:
                file_meta = MetaInfo(fileitem.basename)
                season_index = file_meta.begin_season if file_meta.begin_season is not None else 1
                episode_index = file_meta.begin_episode
                if not episode_index:
                    continue
                if season_index not in seasons:
                    seasons[season_index] = []
                if episode_index not in seasons[season_index]:
                    seasons[season_index].append(episode_index)
            # 返回剧集情况
            logger.info(f"{mediainfo.title_year} 在本地文件系统中找到了这些季集：{seasons}")
            return ExistMediaInfo(type=MediaType.TV, seasons=seasons)
        return None
