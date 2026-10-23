"""
文件管理模块的存储路由、整理计划边界和标准媒体库查询。
"""

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from app.adapters.system.fsproxy import fsproxy
from app.adapters.system.host import SystemUtils
from app.application.directory import DirectoryHelper
from app.application.messaging.message import MessageHelper
from app.application.transfer.execution import TransferPlanningRejectedError, TransferStepRunner
from app.application.transfer.workflow import TransferPlanCheckpoint, TransferPlanningInput
from app.domain.context import MediaInfo, MusicInfo
from app.domain.meta.metabase import MetaBase
from app.domain.meta.metamusic import MetaMusic
from app.domain.metainfo import MetaInfo
from app.foundation import text as text_tools
from app.foundation.reflection import ModuleHelper
from app.modules import _ModuleBase
from app.modules.filemanager.storages import StorageBase
from app.modules.filemanager.transhandler import TransHandler
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.schemas.file import FileItem, StorageUsage
from app.schemas.mediaserver import ExistMediaInfo
from app.schemas.system import TransferDirectoryConf
from app.schemas.tmdb import TmdbEpisode
from app.schemas.transfer import TransferInfo
from app.schemas.types import MUSIC_ENTITY_ALBUM, MediaType, ModuleType, OtherModulesType, StorageAction


class FileManagerModule(_ModuleBase):
    """
    提供存储能力调度、媒体文件查询和整理计划执行入口。

    按存储标识解析适配器；整理路径规划与写入执行分开，具体传输交给 TransHandler。
    """

    _storage_schemas: list[type[StorageBase]] = []
    _support_storages: list[str] = []

    def __init__(self) -> None:
        """
        初始化文件整理使用的目录和消息能力。
        """
        super().__init__()
        self.directoryhelper = DirectoryHelper()
        self.messagehelper = MessageHelper()

    def init_module(self) -> None:
        """
        发现声明存储类型的实现，并建立模块支持的存储标识列表。

        此阶段仅加载实现类，不主动实例化各存储客户端。
        """
        self._storage_schemas = ModuleHelper.load('app.modules.filemanager.storages',
                                                  filter_func=lambda _, obj: hasattr(obj, 'schema') and obj.schema)
        self._support_storages = [storage.schema.value for storage in self._storage_schemas if storage.schema]

    @staticmethod
    def get_name() -> str:
        """
        返回供用户选择的文件整理模块名称。

        :return: 固定为文件整理
        """
        return "文件整理"

    @staticmethod
    def get_type() -> ModuleType:
        """
        返回文件整理模块所属的大类。

        :return: 其他能力模块类型
        """
        return ModuleType.Other

    @staticmethod
    def get_subtype() -> OtherModulesType:
        """
        返回文件整理能力的模块子类型。

        :return: 文件管理子类型
        """
        return OtherModulesType.FileManager

    @staticmethod
    def get_priority() -> int:
        """
        返回同一能力接口内的模块选择优先级。

        :return: 固定为 4，数值越小越优先
        """
        return 4

    def stop(self) -> bool:
        """
        关闭已创建的存储实例并释放其弱单例。

        未使用的实现不会因关停而创建；关闭返回 False 或抛出异常时保留实例供后续清理。

        :return: 所有已创建实例均关闭并释放成功时为 True；任一失败时为 False
        """
        converged = True
        for storage_schema in self._storage_schemas:
            get_existing = getattr(storage_schema, "get_existing_instance", None)
            release_existing = getattr(storage_schema, "release_existing_instance", None)
            if not get_existing or not release_existing:
                continue
            instance = get_existing()
            if instance is None:
                continue
            try:
                stopped = instance.close()
            except Exception as err:
                converged = False
                logger.error(
                    f"关闭存储 {storage_schema.schema.value} 失败：{str(err)}"
                )
                continue
            if stopped is False:
                converged = False
                continue
            if not release_existing(instance):
                converged = False
        return converged

    def test(self) -> Tuple[bool, str]:
        """
        检查目录配置、存储凭据和整理方式是否可用。

        本地目录检查存在性，启用本地硬链接时还检查同盘约束；远端检查语义由存储实现决定。

        :return: 检查结果及首个失败原因；成功时原因为空字符串
        """
        dirs = self.directoryhelper.get_dirs()
        if not dirs:
            return False, "未设置任何目录"
        for d in dirs:
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
                if d.transfer_type in ("link", "reflink") \
                        and d.storage == "local" \
                        and d.library_storage == "local" \
                        and not SystemUtils.is_same_disk(Path(download_path), Path(library_path)):
                    action_name = "引用链接" if d.transfer_type == "reflink" else "硬链接"
                    return False, f"{d.name} 的下载目录 {download_path} 与媒体库目录 {library_path} 不在同一磁盘，无法{action_name}"
            storage_oper = self.__get_storage_oper(d.storage)
            if storage_oper:
                if not storage_oper.check():
                    return False, f"{d.name} 的存储测试不通过"
                if d.transfer_type and d.transfer_type not in storage_oper.support_transtype():
                    return False, f"{d.name} 的存储不支持 {d.transfer_type} 整理方式"

        return True, ""

    def __get_storage_oper(self, _storage: Optional[str], _func: Optional[str] = None) -> Optional[StorageBase]:
        """
        按存储标识和可选方法要求取得适配器实例。

        :param _storage: 已注册的存储标识；为空时无匹配结果
        :param _func: 要求实现的方法名；为空时不限制方法
        :return: 匹配的存储实例；无匹配时返回 None
        """
        for storage_schema in self._storage_schemas:
            if storage_schema.schema \
                    and storage_schema.schema.value == _storage \
                    and (not _func or hasattr(storage_schema, _func)):
                return storage_schema()
        return None

    def init_setting(self) -> Optional[Tuple[str, Union[str, bool]]]:
        """
        保留文件整理模块的设置初始化扩展入口。

        :return: 当前返回 None，不提供额外设置初始化结果
        """
        return None

    def storage_manage(
        self, storage: str, action: StorageAction, **params: Any
    ) -> Dict[str, Any]:
        """
        按存储标识路由配置、容量和登录管理操作。

        不支持的存储或动作返回失败结果；具体实现抛出的异常不在此处统一捕获。

        :param storage: 已注册的存储标识
        :param action: 存储管理动作枚举或其对应值
        :param params: 具体动作参数；保存配置读取 conf，登录动作原样透传
        :return: 含 success 的结果字典；按动作附带 message 或 data
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
        根据配置的重命名模板生成建议的媒体相对路径。

        :param meta: 文件名解析得到的元数据，原始标题用于推导扩展名
        :param mediainfo: 已识别媒体信息，决定模板和命名变量
        :param episodes_info: 由链层预取的当前季集信息；可为空
        :return: 包含目录的建议路径文本；无法生成路径时返回空字符串
        """
        handler = TransHandler()
        rename_format = get_runtime_setting('RENAME_FORMAT')(mediainfo.type)
        path = handler.get_rename_path(
            template_string=rename_format,
            rename_dict=handler.get_naming_dict(meta=meta,
                                                mediainfo=mediainfo,
                                                episodes_info=episodes_info,
                                                file_ext=Path(meta.title or "").suffix)
        )
        return path.as_posix() if path else ""

    def list_files(self, fileitem: FileItem, recursion: Optional[bool] = False) -> Optional[List[FileItem]]:
        """
        通过对应存储列举目录子项或递归收集文件。

        存储查询异常向上传播，递归结果保持存储返回的遍历顺序。

        :param fileitem: 提供存储标识及源路径的文件项
        :param recursion: 为真时递归进入子目录且只返回文件，默认仅列举直接子项
        :return: 查询结果列表；不支持该存储时返回 None
        """
        if fileitem.storage not in self._support_storages:
            return None
        storage_oper = self.__get_storage_oper(fileitem.storage)
        if not storage_oper:
            logger.error(f"不支持 {fileitem.storage} 的文件浏览")
            return None

        def __get_files(_item: FileItem, _r: Optional[bool] = False) -> None:
            """
            将当前目录子项追加到外层结果，按需递归子目录。

            :param _item: 当前待查询文件项
            :param _r: 是否递归并只收集文件
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

        result: list[FileItem] = []
        __get_files(fileitem, recursion)

        return result

    def any_files(self, fileitem: FileItem, extensions: Optional[list[str]] = None) -> Optional[bool]:
        """
        查询目录下是否存在指定扩展名的文件或任意子项。

        提供扩展名过滤时递归检查子目录；未提供时目录子项本身也会满足条件。

        :param fileitem: 待检查目录及其存储标识
        :param extensions: 带点的小写扩展名列表；为空时只要直接子项非空就返回 True
        :return: 匹配时为 True，无匹配时为 False，不支持该存储时为 None
        """
        if fileitem.storage not in self._support_storages:
            return None
        storage_oper = self.__get_storage_oper(fileitem.storage)
        if not storage_oper:
            logger.error(f"不支持 {fileitem.storage} 的文件浏览")
            return None

        def __any_file(_item: FileItem) -> bool:
            """
            按外层扩展名过滤条件查找任意匹配子项。

            :param _item: 当前待检查的目录项
            :return: 出现匹配文件或未过滤的非空目录时为 True
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

        return __any_file(fileitem)

    def create_folder(self, fileitem: FileItem, name: str) -> Optional[FileItem]:
        """
        在指定存储的父目录下创建子目录。

        :param fileitem: 带存储标识的父目录项
        :param name: 新的直接子目录名称
        :return: 存储返回的目录项；不支持或创建失败时返回 None
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
        取得目录并由存储实现按需创建缺失路径。

        :param storage: 已注册的存储标识
        :param path: 存储内的目录路径
        :return: 目录项；不支持或无法取得时返回 None
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
        委托对应存储删除文件或目录。

        :param fileitem: 携带存储和文件定位信息的目标项
        :return: 存储返回的删除结果；未注册存储返回 None，找不到适配器返回 False
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
        委托对应存储重命名文件或目录。

        :param fileitem: 携带存储和文件定位信息的目标项
        :param name: 新的名称，不包含父目录
        :return: 存储返回的结果；未注册存储返回 None，找不到适配器返回 False
        """
        if fileitem.storage not in self._support_storages:
            return None
        storage_oper = self.__get_storage_oper(fileitem.storage)
        if not storage_oper:
            logger.error(f"不支持 {fileitem.storage} 的重命名处理")
            return False
        return storage_oper.rename(fileitem, name)

    def download_file(self, fileitem: FileItem, path: Optional[Path] = None) -> Optional[Path]:
        """
        委托源存储将文件下载到本地目录。

        :param fileitem: 携带存储和源文件信息的文件项
        :param path: 本地目标目录；为空时由存储选择默认目录
        :return: 本地文件路径；不支持或下载失败时返回 None
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
        委托目标存储接收本地文件。

        源文件是否保留以及同名处理方式遵循具体存储的上传契约。

        :param fileitem: 带目标存储标识的目录项
        :param path: 本地源文件路径
        :param new_name: 远端文件名；为空时由存储沿用源名称
        :return: 目标文件项；不支持或上传失败时返回 None
        """
        if fileitem.storage not in self._support_storages:
            return None
        storage_oper = self.__get_storage_oper(fileitem.storage)
        if not storage_oper:
            logger.error(f"不支持 {fileitem.storage} 的上传处理")
            return None
        return storage_oper.upload(fileitem, path, new_name)

    def link_file(self, fileitem: FileItem, target_file: Path) -> bool:
        """
        在源文件所属存储内创建硬链接。

        :param fileitem: 已存在的源文件项
        :param target_file: 目标存储中的完整文件路径
        :return: 存储实现完成链接时返回 True，否则返回 False
        """
        if not fileitem.storage or fileitem.storage not in self._support_storages:
            return False
        storage_oper = self.__get_storage_oper(fileitem.storage, "link")
        if not storage_oper:
            return False
        return bool(storage_oper.link(fileitem, target_file))

    def get_file_item(self, storage: str, path: Path) -> Optional[FileItem]:
        """
        根据路径严格查询文件或目录。

        本方法保留查询异常；普通兼容模块调度层可能隔离异常并投影为空结果。

        :param storage: 已注册的存储标识
        :param path: 存储内的目标路径
        :return: 查询到的文件项；不存在或不支持存储时返回 None
        """
        if storage not in self._support_storages:
            return None
        storage_oper = self.__get_storage_oper(storage)
        if not storage_oper:
            logger.error(f"不支持 {storage} 的文件获取")
            return None
        return storage_oper.get_item_strict(path)

    def get_parent_item(self, fileitem: FileItem) -> Optional[FileItem]:
        """
        通过对应存储查询上级目录。

        :param fileitem: 带存储标识的当前文件或目录项
        :return: 父目录项；不支持或查询失败时返回 None
        """
        if fileitem.storage not in self._support_storages:
            return None
        storage_oper = self.__get_storage_oper(fileitem.storage)
        if not storage_oper:
            logger.error(f"不支持 {fileitem.storage} 的文件获取")
            return None
        return storage_oper.get_parent(fileitem)

    def snapshot_storage(self, storage: str, path: Path,
                         last_snapshot_time: Optional[float] = None, max_depth: int = 5,
                         previous_snapshot: Optional[Dict[str, Dict[str, Any]]] = None) -> Optional[Dict[str, Dict[str, Any]]]:
        """
        委托存储生成目录树快照或增量快照。

        扫描失败语义由存储实现定义，调用方不得将未知状态视为文件已删除。

        :param storage: 已注册的存储标识
        :param path: 快照根目录
        :param last_snapshot_time: 上次快照的 Unix 时间秒数；为空时执行完整扫描
        :param max_depth: 最大递归深度，默认 5，具体层级语义由存储实现定义
        :param previous_snapshot: 用于增量对账的上次完整快照；为空时沿用存储的兼容增量模式
        :return: 以路径为键的元数据快照；不支持存储时返回 None
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

    def plan_transfer(
            self,
            fileitem: FileItem,
            meta: MetaBase,
            mediainfo: Union[MediaInfo, MusicInfo],
            target_directory: Optional[TransferDirectoryConf] = None,
            target_storage: Optional[str] = None,
            target_path: Optional[Path] = None,
            transfer_type: Optional[str] = None, scrape: Optional[bool] = None,
            library_type_folder: Optional[bool] = None,
            library_category_folder: Optional[bool] = None,
            episodes_info: Optional[List[TmdbEpisode]] = None,
            source_oper: Optional[StorageBase] = None,
            preview: Optional[bool] = False,
            planning_input: Optional[TransferPlanningInput] = None,
    ) -> TransferPlanCheckpoint:
        """
        解析整理策略并生成零写副作用的冻结计划。

        目录配置优先于显式目标路径；未匹配配置时保留自定义路径能力并告警，
        未提供显式路径时业务拒绝，不自动使用存储根目录。

        :param fileitem: 文件信息
        :param meta: 预识别的元数据
        :param mediainfo: 识别的媒体信息
        :param target_directory: 目标目录配置
        :param target_storage: 目标存储
        :param target_path: 调用方显式指定的目标根路径；未匹配目录配置时使用
        :param transfer_type: 转移模式
        :param scrape: 是否刮削元数据
        :param library_type_folder: 是否按媒体类型创建目录
        :param library_category_folder: 是否按媒体类别创建目录
        :param episodes_info: 当前季的全部集信息
        :param source_oper: 源存储操作对象
        :param preview: 是否仅预览；目录预览不冻结叶子操作
        :param planning_input: 请求接纳阶段冻结的原始请求，传入时不得改写
        :return: 可持久化的整理计划检查点
        :raises TransferPlanningRejectedError: 未匹配到可用媒体库目录
        :raises ValueError: 源路径、目标路径、整理方式或存储配置无效
        """
        handler = TransHandler()
        if (
                fileitem.storage == "local"
                and (not fileitem.path or not Path(fileitem.path).exists())
        ):
            raise ValueError(f"{fileitem.path} 不存在")
        if target_path and target_path.is_file():
            logger.error(f"整理目标路径 {target_path} 是一个文件")
            raise ValueError(f"{target_path} 不是有效目录")
        if target_directory:
            if not target_directory.library_path:
                logger.error(f"目标媒体库目录未设置，无法整理文件，源路径：{fileitem.path}")
                raise ValueError("目标媒体库目录未设置")
            if not transfer_type:
                transfer_type = target_directory.transfer_type
            if not target_storage:
                target_storage = target_directory.library_storage
            need_rename = target_directory.renaming
            need_notify = target_directory.notify
            overwrite_mode = target_directory.overwrite_mode
            need_scrape = target_directory.scraping if scrape is None else scrape
            target_path = handler.get_dest_dir(mediainfo=mediainfo, target_dir=target_directory,
                                               need_type_folder=library_type_folder,
                                               need_category_folder=library_category_folder)
        elif target_path:
            explicit_target_path = target_path
            need_scrape = scrape or False
            need_rename = True
            need_notify = False
            overwrite_mode = "never"
            target_path = handler.get_dest_path(mediainfo=mediainfo, target_path=target_path,
                                                need_type_folder=library_type_folder,
                                                need_category_folder=library_category_folder)
            logger.warning(
                "未匹配到媒体库目录配置，使用调用方显式指定的目标路径："
                f"【{target_storage or fileitem.storage or 'local'}】{explicit_target_path}，"
                f"计算后的目标路径：{target_path}，源路径："
                f"【{fileitem.storage or 'local'}】{fileitem.path}；"
                "如需使用存储目录配置的媒体库路径，请将目标路径设为自动匹配"
            )
        else:
            # 目录匹配失败按业务拒绝结算，避免进入异常重试和堆栈日志路径。
            logger.error(
                f"{mediainfo.type.value if mediainfo.type else '未知类型'} {mediainfo.title_year} 未找到有效的媒体库目录，无法整理文件，源路径：{fileitem.path}")
            raise TransferPlanningRejectedError("未找到有效的媒体库目录")
        if not transfer_type:
            directory_name = target_directory.name if target_directory else "目标目录"
            logger.error(f"{directory_name} 未设置整理方式")
            raise ValueError(f"{directory_name} 未设置整理方式")
        if target_path is None:
            raise ValueError("整理规划缺少目标路径")

        source_storage = fileitem.storage or "local"
        if not source_oper:
            source_oper = self.__get_storage_oper(source_storage)
        if not source_oper:
            raise ValueError(f"不支持的存储类型：{source_storage}")

        if not target_storage:
            target_storage = source_storage
        if planning_input is None:
            planning_input = TransferPlanningInput(
                source_fileitem=fileitem.model_dump(mode="json"),
                meta=self._serialize_transfer_model(meta) if meta else None,
                mediainfo=(
                    self._serialize_transfer_model(mediainfo) if mediainfo else None
                ),
                target_directory=(
                    target_directory.model_dump(mode="json")
                    if target_directory
                    else None
                ),
                target_storage=target_storage,
                target_path=target_path.as_posix(),
                requested_transfer_type=transfer_type,
                media_type=mediainfo.type.value if mediainfo.type else None,
                need_scrape=bool(need_scrape),
                need_rename=bool(need_rename),
                need_notify=bool(need_notify),
                overwrite_mode=overwrite_mode,
                episodes_info=tuple(
                    episode.model_dump(mode="json") for episode in episodes_info or []
                ),
                preview=bool(preview),
            )

        logger.info(f"获取整理目标路径：【{target_storage}】{target_path}")
        return handler.plan_transfer(
            planning_input,
            meta=meta,
            mediainfo=mediainfo,
            source_oper=source_oper,
            target_storage=target_storage,
            target_path=target_path,
            transfer_type=transfer_type,
            need_scrape=bool(need_scrape),
            need_rename=bool(need_rename),
            need_notify=bool(need_notify),
            overwrite_mode=overwrite_mode,
            episodes_info=episodes_info,
            preview=bool(preview),
        )

    @staticmethod
    def __cleanup_overlaps_new_target(checkpoint: TransferPlanCheckpoint, cleanup_item: FileItem) -> bool:
        """
        检查历史清理路径与新目标是否重合或互为祖先。

        本地路径解析符号链接父目录，避免不同字符串实际指向同一目标。

        :raises ValueError: 历史目标缺少路径
        :param checkpoint: 包含最终目标和叶子目标的冻结计划
        :param cleanup_item: 拟清理的历史目标项
        :return: 同一存储且路径重叠时为 True，否则为 False
        """
        if not cleanup_item.path:
            raise ValueError("历史整理目的缺少文件路径")
        if cleanup_item.storage != checkpoint.target_storage:
            return False
        cleanup_path = Path(cleanup_item.path)
        new_paths = [Path(checkpoint.final_target_path)] + [Path(item.target_path) for item in checkpoint.items]
        if checkpoint.target_storage == "local":
            # 历史路径可通过符号链接父目录指向新目标；仅比较字符串会误删新文件。
            # strict=False 允许尚未落地的目标，已有父目录的别名仍必须解析。
            cleanup_path = fsproxy.realpath(cleanup_path)
            new_paths = [fsproxy.realpath(path) for path in new_paths]
        return any(cleanup_path == path or cleanup_path in path.parents or path in cleanup_path.parents
                   for path in new_paths)

    def execute_transfer_plan(
            self,
            checkpoint: TransferPlanCheckpoint,
            *,
            meta: MetaBase,
            mediainfo: Union[MediaInfo, MusicInfo],
            source_oper: Optional[StorageBase] = None,
            target_oper: Optional[StorageBase] = None,
            cleanup_media_file: Optional[Callable[[FileItem], bool]] = None,
            observe_cleanup_media_file: Optional[Callable[[FileItem], bool]] = None,
            step_runner: Optional[TransferStepRunner] = None,
            raise_exception: bool = False,
    ) -> TransferInfo:
        """
        解析存储适配器并通过统一删除能力执行已冻结计划。

        :param checkpoint: 冻结的整理计划检查点
        :param meta: 预识别的元数据
        :param mediainfo: 识别的媒体信息
        :param source_oper: 源存储操作对象
        :param target_oper: 目标存储操作对象
        :param cleanup_media_file: 统一删除能力，返回 True 表示已清理
        :param observe_cleanup_media_file: 统一删除能力的只读观察接口，返回 True 表示已清理
        :param step_runner: 传入时使用自定义的步骤执行器，None 时使用默认的串行执行器
        :param raise_exception: 仅供模块调度器控制异常是否传播，模块不改变执行语义
        :return: 整理执行结果；存储不支持时返回失败结果，执行冲突或清理异常可向上传播
        """
        del raise_exception
        source_fileitem = FileItem.model_validate(checkpoint.planning_input.source_fileitem)
        cleanup_before_transfer = None
        observe_cleanup_before_transfer = None
        cleanup_payload = checkpoint.planning_input.options.get(
            "cleanup_dest_fileitem"
        )
        cleanup_fileitem: Optional[FileItem] = None
        if (
                isinstance(cleanup_payload, dict)
                and not checkpoint.preview
                and not checkpoint.pre_execution_cleanup_completed
        ):
            cleanup_fileitem = FileItem.model_validate(cleanup_payload)
            # 重合目的由文件级覆盖处理，统一删除目录会误删已经落地的新版本。
            if self.__cleanup_overlaps_new_target(checkpoint, cleanup_fileitem):
                cleanup_fileitem = None
        else:
            cleanup_fileitem = None
        if cleanup_fileitem is not None:
            cleanup_target: FileItem = cleanup_fileitem
            if not cleanup_media_file:
                raise RuntimeError("整理计划缺少统一媒体删除兼容能力")

            def cleanup_before_transfer(item: FileItem = cleanup_target) -> None:
                """
                新目标落地后清理不重叠的历史目标及其空父目录。

                执行前再次核对路径是否重叠；统一删除能力失败时抛出 RuntimeError 以保留重试状态。

                :param item: 冻结的历史目标，默认使用计划中的清理项
                """
                # 落地期间目录链接也可能变化，清理边界再次核对实际目标。
                if self.__cleanup_overlaps_new_target(checkpoint, item):
                    return
                if not cleanup_media_file(item):
                    raise RuntimeError(
                        f"{item.path} 删除失败，整理计划保留待重试"
                    )

            if observe_cleanup_media_file:
                def observe_cleanup_before_transfer(item: FileItem = cleanup_target) -> bool:
                    """
                    只读确认历史目标是否已由统一删除能力清理。

                    :param item: 冻结的历史目标，默认使用计划中的清理项
                    :return: 观察接口确认已清理时为 True
                    """
                    return observe_cleanup_media_file(item)

        source_storage = source_fileitem.storage or "local"
        if not source_oper:
            source_oper = self.__get_storage_oper(source_storage)
        if not source_oper:
            return TransferInfo(
                success=False,
                message=f"不支持的存储类型：{source_storage}",
                fileitem=source_fileitem,
                fail_list=[source_fileitem.path or ""],
                transfer_type=checkpoint.resolved_transfer_type,
                need_notify=checkpoint.need_notify,
            )
        if not target_oper:
            target_oper = self.__get_storage_oper(checkpoint.target_storage)
        if not target_oper:
            return TransferInfo(
                success=False,
                message=f"不支持的存储类型：{checkpoint.target_storage}",
                fileitem=source_fileitem,
                fail_list=[source_fileitem.path or ""],
                transfer_type=checkpoint.resolved_transfer_type,
                need_notify=checkpoint.need_notify,
            )
        return TransHandler().execute_transfer_plan(
            checkpoint,
            meta=meta,
            mediainfo=mediainfo,
            source_oper=source_oper,
            target_oper=target_oper,
            cleanup_before_transfer=cleanup_before_transfer,
            observe_cleanup_before_transfer=observe_cleanup_before_transfer,
            step_runner=step_runner,
        )

    @staticmethod
    def _serialize_transfer_model(value: object) -> dict[str, Any]:
        """
        校验领域对象的动态序列化结果可作为整理快照。

        :raises TypeError: 不提供序列化方法或返回值不是字典
        :param value: 提供 to_dict 方法的领域对象
        :return: 对象返回的字典
        """
        serializer = getattr(value, "to_dict", None)
        if not callable(serializer):
            raise TypeError(f"{type(value).__name__} 不支持整理快照序列化")
        payload = serializer()
        if not isinstance(payload, dict):
            raise TypeError(f"{type(value).__name__} 返回了无效整理快照")
        return payload

    @staticmethod
    def _build_library_lookup_meta(
            mediainfo: Union[MediaInfo, MusicInfo],
    ) -> MetaBase:
        """
        构造标准媒体库路径反查使用的最小命名元数据。

        :param mediainfo: 已识别的影视或音乐信息
        :return: 带必要命名字段的元数据；影视缺少季集时补为第 1 季第 1 集
        """
        if mediainfo.type == MediaType.MUSIC:
            music_type = getattr(mediainfo, "music_type", None)
            album = getattr(mediainfo, "album", None)
            if not album and music_type == MUSIC_ENTITY_ALBUM:
                album = mediainfo.title
            year_value = mediainfo.year
            year = (
                year_value
                if isinstance(year_value, int)
                else int(year_value)
                if isinstance(year_value, str) and year_value.isdigit()
                else None
            )
            return MetaMusic(
                title=mediainfo.title or "",
                artists=list(getattr(mediainfo, "artists", None) or []),
                album=album,
                album_artist=getattr(mediainfo, "album_artist", None),
                year=year,
                disc_number=getattr(mediainfo, "disc_number", None),
                track_number=getattr(mediainfo, "track_number", None),
                total_tracks=getattr(mediainfo, "total_tracks", None),
                media_source=getattr(mediainfo, "source", None),
                media_id=getattr(mediainfo, "media_id", None),
            )

        meta = MetaInfo(mediainfo.title or "")
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
        """
        从标准音乐路径解析曲目去重身份。

        :param fileitem: 提供文件路径或名称的音乐文件项
        :return: 碟号、曲序和归一化曲名；缺失碟号或曲序保留为 None
        """
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
        """
        按曲名及可用碟号、曲序判断目标单曲是否已存在。

        :param fileitems: 媒体库中候选音频文件
        :param mediainfo: 待查询的单曲身份
        :return: 存在曲名相同且已知碟号、曲序不冲突的文件时为 True
        """
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
        """
        按去重曲目身份判断专辑是否达到目标曲目数。

        缺少碟号按第 1 碟处理；有曲序时用曲序，否则用归一化曲名去重。

        :param fileitems: 媒体库中候选音频文件
        :param total_tracks: 目标总曲数；缺失或为 0 时无法认定完整
        :return: 去重曲数不少于目标总曲数时为 True
        """
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
        按标准命名模板反查各媒体库中的匹配媒体文件。

        音乐使用音频扩展名配置，影视使用媒体扩展名配置；无法安全限定媒体根目录时跳过扫描。

        :param mediainfo: 影视或音乐身份，用于推导分类目录和命名路径
        :return: 去重后的媒体文件列表；未匹配到有效路径时为空
        """
        handler = TransHandler()
        ret_fileitems = []
        dest_dirs = DirectoryHelper().get_library_dirs()
        for dest_dir in dest_dirs:
            storage_oper = self.__get_storage_oper(dest_dir.library_storage)
            if not storage_oper:
                continue
            dir_path = handler.get_dest_dir(mediainfo=mediainfo, target_dir=dest_dir)
            rename_format = get_runtime_setting('RENAME_FORMAT')(mediainfo.type)
            # 元数据补上常用属性，尽可能确保重命名后的路径不出现空白
            meta = self._build_library_lookup_meta(mediainfo)
            target_path = handler.get_rename_path(
                path=dir_path,
                template_string=rename_format,
                rename_dict=handler.get_naming_dict(meta=meta,
                                                    mediainfo=mediainfo)
            )
            media_path = DirectoryHelper.get_media_root_path(
                rename_format,
                rename_path=target_path,
                media_type=mediainfo.type,
            )
            if not media_path:
                continue
            if dir_path != media_path and dir_path.is_relative_to(media_path):
                # 兜底检查，避免不必要的扫盘
                logger.warn(f"{media_path} 是媒体库目录 {dir_path} 的父目录，忽略获取媒体文件列表，请检查重命名格式！")
                continue
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
                    get_runtime_setting('RMT_AUDIOEXT')
                    if mediainfo.type == MediaType.MUSIC
                    else get_runtime_setting('RMT_MEDIAEXT')
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
            **kwargs: object,
    ) -> Optional[ExistMediaInfo]:
        """
        判断配置的本地或网盘媒体库是否已包含目标媒体。

        仅支持标准媒体库结构。电影存在任一匹配文件即可；音乐按单曲身份或完整专辑曲数判断；
        剧集从文件名提取季集，未标季号按第 1 季处理。

        :param mediainfo: 待查询的影视或音乐信息
        :param kwargs: 兼容调度选项；server 非空时跳过文件系统检查
        :return: 存在媒体时返回类型及已知季集信息；未找到或未启用检查时返回 None
        """
        if kwargs.get("server"):
            return None

        if not get_runtime_setting('LOCAL_EXISTS_SEARCH'):
            return None

        logger.debug(f"正在本地媒体库中查找 {mediainfo.title_year}...")

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
                if not isinstance(mediainfo, MusicInfo):
                    return None
                exists = self._music_recording_exists(fileitems, mediainfo)
            if not exists:
                logger.debug(f"{mediainfo.title_year} 在本地音乐库中尚不完整")
                return None
            logger.info(f"{mediainfo.title_year} 在本地音乐库中找到了")
            return ExistMediaInfo(type=MediaType.MUSIC)
        if mediainfo.type == MediaType.TV:
            seasons: Dict[int, list[int]] = {}
            for fileitem in fileitems:
                file_meta = MetaInfo(fileitem.basename or "")
                season_index = file_meta.begin_season if file_meta.begin_season is not None else 1
                episode_indexes = file_meta.episode_list
                if not episode_indexes:
                    continue
                if season_index not in seasons:
                    seasons[season_index] = []
                for episode_index in episode_indexes:
                    if episode_index not in seasons[season_index]:
                        seasons[season_index].append(episode_index)
            logger.info(f"{mediainfo.title_year} 在本地文件系统中找到了这些季集：{seasons}")
            return ExistMediaInfo(type=MediaType.TV, seasons=seasons)
        return None
