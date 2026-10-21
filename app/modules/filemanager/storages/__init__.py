"""
文件管理存储接口、传输进度展示和可增量更新的目录快照。
"""

from abc import ABCMeta, abstractmethod
from pathlib import Path, PurePosixPath
from typing import Any, Callable, ClassVar, Dict, List, Optional, Tuple, Union

from tqdm import tqdm

from app.application.storage import StorageHelper
from app.foundation.crypto import HashUtils
from app.runtime.log import logger
from app.runtime.progress import ProgressHelper
from app.schemas.exception import StorageQueryError
from app.schemas.file import FileItem as _SchemaFileItem
from app.schemas.file import StorageUsage as _SchemaStorageUsage
from app.schemas.system import StorageConf as _SchemaStorageConf
from app.schemas.types import StorageSchema

_SnapshotInfo = Dict[str, Dict[str, Any]]


def transfer_process(path: str) -> Callable[[int | float], None]:
    """
    启动终端与界面进度展示，并返回更新回调。

    :param path: 传输源标识，用于进度展示和进度任务键
    :return: 接收百分比的回调；达到 100 时结束进度并关闭终端进度条
    """
    pbar = tqdm(total=100, desc="进度", unit="%")
    progress = ProgressHelper(HashUtils.md5(path))
    progress.start()

    def update_progress(percent: Union[int, float]) -> None:
        """
        更新一次传输进度，达到完成值时释放展示资源。

        :param percent: 完成百分比，浮点数保留两位小数；不在此方法校验范围
        """
        percent_value = round(percent, 2) if isinstance(percent, float) else percent
        pbar.n = percent_value
        pbar.refresh()
        progress.update(value=percent_value, text=f"{path} 进度：{percent_value}%")
        if percent_value >= 100:
            progress.end()
            pbar.close()

    return update_progress


class StorageBase(metaclass=ABCMeta):
    """
    定义文件管理的存储接口及通用目录快照行为。

    具体实现负责外部资源、路径含义和失败语义。普通查询可返回空结果，
    覆盖保护等需要区分不存在与失败的场景必须使用严格查询接口。
    """
    schema: ClassVar[StorageSchema]
    transtype: dict[str, str] = {}

    @property
    def snapshot_check_folder_modtime(self) -> bool:
        """
        决定增量快照是否根据目录修改时间跳过未变化子树。

        :return: 默认 True；目录时间不能反映子项变化的存储应覆盖为 False
        """
        return True

    @property
    def snapshot_strict_query(self) -> bool:
        """
        声明快照是否启用能区分缺失与故障的严格查询。

        :return: 默认 False；适配严格目录和路径查询的存储可覆盖为 True
        """
        return False

    def __init__(self) -> None:
        """
        取得用于读取和保存存储配置的应用服务。
        """
        self.storagehelper = StorageHelper()

    def close(self) -> bool | None:
        """
        提供存储资源释放钩子，默认没有资源需要清理。

        :return: 默认 None；实现返回 False 时表示资源尚未释放完成，调用方须保留实例
        """
        return None

    @abstractmethod
    def init_storage(self) -> None:
        """
        按当前配置初始化具体存储的连接或客户端。
        """
        pass

    def generate_qrcode(self, *args: Any, **kwargs: Any) -> Optional[Tuple[dict[str, object], str]]:
        """
        提供存储二维码登录扩展入口。

        :param args: 由具体存储定义的登录位置参数
        :param kwargs: 由具体存储定义的登录关键字参数
        :return: 二维码数据及说明的二元组；基类默认返回 None
        """
        pass

    def generate_auth_url(self, *args: Any, **kwargs: Any) -> Optional[Tuple[dict[str, object], str]]:
        """
        提供存储 OAuth2 授权地址扩展入口。

        :param args: 由具体存储定义的授权位置参数
        :param kwargs: 由具体存储定义的授权关键字参数
        :return: 授权数据与说明的二元组；基类返回空数据和不支持说明
        """
        return {}, "此存储不支持 OAuth2 授权"

    def check_login(self, *args: Any, **kwargs: Any) -> Optional[Tuple[dict[str, Any], str]]:
        """
        提供存储登录状态查询扩展入口。

        :param args: 由具体存储定义的登录位置参数
        :param kwargs: 由具体存储定义的登录关键字参数
        :return: 具体存储定义的登录结果字典；基类默认返回 None
        """
        pass

    def get_config(self) -> Optional[_SchemaStorageConf]:
        """
        按当前存储类型读取完整配置。

        :return: 存储配置对象；未配置时返回 None
        """
        return self.storagehelper.get_storage(self.schema.value)

    def get_conf(self) -> dict[str, Any]:
        """
        读取当前存储的配置内容。

        :return: 配置字典；未配置时返回空字典
        """
        conf = self.get_config()
        return conf.config or {} if conf else {}

    def set_config(self, conf: dict[str, object]) -> None:
        """
        持久化当前存储配置并立即重新初始化。

        :param conf: 当前存储的新配置字典
        """
        self.storagehelper.set_storage(self.schema.value, conf)
        self.init_storage()

    def support_transtype(self) -> dict[str, str]:
        """
        提供当前存储支持的整理方式。

        :return: 整理方式标识到显示名称的映射，直接返回类配置
        """
        return self.transtype

    def is_support_transtype(self, transtype: str) -> bool:
        """
        判断当前存储是否声明支持某种整理方式。

        :param transtype: 整理方式标识，如 copy 或 move
        :return: 标识存在于 transtype 中返回 True
        """
        return transtype in self.transtype

    def reset_config(self) -> None:
        """
        清除当前存储配置并重新初始化实例。
        """
        self.storagehelper.reset_storage(self.schema.value)
        self.init_storage()

    @staticmethod
    def _safe_download_name(name: Optional[str]) -> Optional[str]:
        """
        提取可安全落盘的文件名。
        """
        if not name:
            return None

        safe_name = PurePosixPath(str(name).replace("\\", "/")).name
        if safe_name in ("", ".", ".."):
            return None
        return safe_name

    def _build_download_path(
        self, fileitem: _SchemaFileItem, path: Path
    ) -> Optional[Path]:
        """
        构造本地下载路径，避免远端文件名携带目录片段时越过目标目录。
        """
        safe_name = self._safe_download_name(fileitem.name)
        if not safe_name:
            logger.error(f"【存储】下载文件名无效：{fileitem.name}")
            return None

        local_path = path / safe_name
        try:
            local_path.resolve().relative_to(path.resolve())
        except ValueError:
            logger.error(f"【存储】下载路径越界：{fileitem.name} -> {local_path}")
            return None
        return local_path

    @abstractmethod
    def check(self) -> bool:
        """
        检查存储是否可用。
        """
        pass

    @abstractmethod
    def list(self, fileitem: _SchemaFileItem) -> List[_SchemaFileItem]:
        """
        列举目录内容，或按具体存储约定返回单个文件。

        :param fileitem: 需要浏览的存储文件或目录项
        :return: 子项列表；普通查询的空结果不保证能区分空目录与查询失败
        """
        pass

    def list_strict(self, fileitem: _SchemaFileItem) -> List[_SchemaFileItem]:
        """
        完整列举目录，并区分空目录与查询失败。

        :param fileitem: 需要确认全部直接子项的目录
        :return: 完整直接子项列表；空列表只表示确认无子项
        :raises StorageQueryError: 基类未实现严格列举，具体存储也必须在无法确认完整结果时抛出
        """
        raise StorageQueryError(f"存储 {self.schema} 未实现严格目录查询: {fileitem.path}")

    @abstractmethod
    def create_folder(self, fileitem: _SchemaFileItem, name: str) -> Optional[_SchemaFileItem]:
        """
        在指定父目录下创建子目录。

        :param fileitem: 父目录项
        :param name: 需要创建的子目录名称
        :return: 创建或已存在的目录项；无法取得结果时为 None
        """
        pass

    @abstractmethod
    def get_folder(self, path: Path) -> Optional[_SchemaFileItem]:
        """
        取得目录项，按存储实现约定创建缺失目录。

        :param path: 存储中的目录路径；实现可按需创建缺失目录
        :return: 目标目录项；无法取得结果时为 None
        """
        pass

    @abstractmethod
    def get_item(self, path: Path) -> Optional[_SchemaFileItem]:
        """
        查询路径对应的文件或目录。

        :param path: 存储中的文件或目录路径
        :return: 文件或目录项；不存在或实现无法取得结果时为 None
        """
        pass

    def get_item_strict(self, path: Path) -> Optional[_SchemaFileItem]:
        """
        严格查询路径，无法确认状态时拒绝返回不存在。

        基类保守失败，具体存储必须自行实现。覆盖保护不能把查询故障当作目标不存在。

        :param path: 需要确认是否存在的目标路径
        :return: 文件或目录项；只有确认不存在时返回 None
        :raises StorageQueryError: 未实现严格查询或无法确认路径状态
        """
        raise StorageQueryError(f"存储 {self.schema} 未实现严格查询，无法确认目标状态: {path}")

    def get_parent(self, fileitem: _SchemaFileItem) -> Optional[_SchemaFileItem]:
        """
        查询存储项父路径对应的目录。

        :param fileitem: 需要查找父目录的文件或目录项
        :return: 父路径对应的存储项；普通查询无结果时返回 None
        """
        if not fileitem.path:
            return None
        return self.get_item(Path(fileitem.path).parent)

    @abstractmethod
    def delete(self, fileitem: _SchemaFileItem) -> bool:
        """
        删除指定存储项。

        :param fileitem: 需要删除的文件或目录项
        :return: 实现报告的操作成功标记，缺失项语义由具体存储定义
        """
        pass

    @abstractmethod
    def rename(self, fileitem: _SchemaFileItem, name: str) -> bool:
        """
        在同一父目录中重命名存储项。

        :param fileitem: 需要改名的文件或目录项
        :param name: 同一父目录下的新名称
        :return: 实现报告的操作成功标记
        """
        pass

    @abstractmethod
    def download(self, fileitem: _SchemaFileItem, path: Optional[Path] = None) -> Optional[Path]:
        """
        取得源文件可供本地读取的路径。

        :param fileitem: 需要下载的源文件
        :param path: 可选本地保存目录；具体默认位置由实现决定
        :return: 本地可读路径；实现失败时可返回 None，本地存储可直接返回源路径
        """
        pass

    @abstractmethod
    def upload(self, fileitem: _SchemaFileItem, path: Path,
               new_name: Optional[str] = None) -> Optional[_SchemaFileItem]:
        """
        把本地文件放入目标存储目录。

        :param fileitem: 目标存储目录项
        :param path: 需要上传的本地文件路径
        :param new_name: 上传后的可选名称；未指定时通常使用源文件名
        :return: 上传后的目标文件项；无法取得结果时为 None
        """
        pass

    @abstractmethod
    def detail(self, fileitem: _SchemaFileItem) -> Optional[_SchemaFileItem]:
        """
        查询文件详细信息。

        :param fileitem: 需要重新查询详情的文件项
        :return: 最新文件项；无法取得结果时为 None
        """
        pass

    @abstractmethod
    def copy(
        self,
        fileitem: _SchemaFileItem,
        path: Path,
        new_name: str,
        **kwargs: Any,
    ) -> bool:
        """
        复制文件到同一存储的目标目录。

        :param fileitem: 源文件项
        :param path: 同一存储中的目标目录
        :param new_name: 目标文件名
        :param kwargs: 存储特定的扩展参数（如本地存储的 try_reflink）
        :return: 实现报告的复制成功标记
        """
        pass

    @abstractmethod
    def move(self, fileitem: _SchemaFileItem, path: Path, new_name: str) -> bool:
        """
        移动文件到同一存储的目标目录。

        :param fileitem: 源文件项
        :param path: 同一存储中的目标目录
        :param new_name: 目标文件名
        :return: 实现报告的移动成功标记
        """
        pass

    @abstractmethod
    def link(self, fileitem: _SchemaFileItem, target_file: Path) -> bool:
        """
        在支持硬链接的存储中创建目标链接。

        :param fileitem: 硬链接源文件项
        :param target_file: 完整目标链接路径
        :return: 链接创建成功标记；不支持的实现可不执行并返回 None
        """
        pass

    @abstractmethod
    def softlink(self, fileitem: _SchemaFileItem, target_file: Path) -> bool:
        """
        在支持符号链接的存储中创建目标链接。

        :param fileitem: 符号链接源文件项
        :param target_file: 完整目标链接路径
        :return: 链接创建成功标记；不支持的实现可不执行并返回 None
        """
        pass

    def reflink(self, fileitem: _SchemaFileItem, target_file: Path) -> bool:
        """
        在支持引用链接的存储中创建目标文件。

        :param fileitem: 源文件项
        :param target_file: 完整目标文件路径
        :return: 引用链接创建成功标记；不支持的实现返回 False
        """
        return False

    @abstractmethod
    def usage(self) -> Optional[_SchemaStorageUsage]:
        """
        查询存储容量使用情况。

        :return: 以字节为单位的总容量与可用容量；不支持或查询失败时可返回 None
        """
        pass

    def _snapshot_list(self, fileitem: _SchemaFileItem) -> Optional[List[_SchemaFileItem]]:
        """
        按存储能力读取目录，严格快照不能把无结果当作可以跳过的子树。
        """
        if not self.snapshot_strict_query:
            return self.list(fileitem)
        items = self.list_strict(fileitem)
        if items is None:
            raise StorageQueryError(f"目录查询没有返回完整结果: {fileitem.path}")
        return items

    def _snapshot_read_error(self, fileitem: _SchemaFileItem, error: Exception) -> None:
        """
        严格快照传播读取故障；未适配的存储保留原来的子树容错行为。
        """
        if self.snapshot_strict_query:
            if isinstance(error, StorageQueryError):
                raise error
            raise StorageQueryError(f"读取快照路径失败: {fileitem.path} - {error}") from error
        logger.debug(f"Snapshot error for {fileitem.path}: {error}")

    @staticmethod
    def _snapshot_previous_state(
        root_path: PurePosixPath,
        previous_snapshot: Optional[_SnapshotInfo],
    ) -> Tuple[
        _SnapshotInfo,
        Dict[PurePosixPath, set[PurePosixPath]],
        Dict[PurePosixPath, str],
    ]:
        """
        为增量快照建立旧文件和目录直接子项索引。

        :param root_path: 本轮快照的根路径
        :param previous_snapshot: 上一轮完整快照
        :return: 当前文件信息、目录直接子项索引和路径反向索引
        """
        files_info = {
            file_path: file_info
            for file_path, file_info in (previous_snapshot or {}).items()
            if PurePosixPath(file_path).is_relative_to(root_path)
        }
        previous_children: Dict[PurePosixPath, set[PurePosixPath]] = {}
        previous_files: Dict[PurePosixPath, str] = {}
        for file_path in files_info:
            file_posix_path = PurePosixPath(file_path)
            previous_files[file_posix_path] = file_path
            try:
                relative_parts = file_posix_path.relative_to(root_path).parts
            except ValueError:
                # files_info 已经按 root_path 过滤；保留防御性判断，避免
                # 非标准路径对象让增量索引中断整轮监控。
                continue
            for depth in range(len(relative_parts)):
                directory_path = root_path.joinpath(*relative_parts[:depth])
                child_path = directory_path / relative_parts[depth]
                previous_children.setdefault(directory_path, set()).add(child_path)
        return files_info, previous_children, previous_files

    @staticmethod
    def _snapshot_remove_previous_subtree(
        path: PurePosixPath,
        files_info: _SnapshotInfo,
        previous_children: Dict[PurePosixPath, set[PurePosixPath]],
        previous_files: Dict[PurePosixPath, str],
    ) -> None:
        """
        沿旧快照索引移除一个已经消失的直接子树。
        """
        pending = [path]
        while pending:
            current_path = pending.pop()
            old_file_path = previous_files.pop(current_path, None)
            if old_file_path is not None:
                files_info.pop(old_file_path, None)
            pending.extend(previous_children.pop(current_path, ()))

    def _snapshot_remove_deleted_children(
        self,
        fileitem: _SchemaFileItem,
        sub_files: List[_SchemaFileItem],
        files_info: _SnapshotInfo,
        previous_children: Dict[PurePosixPath, set[PurePosixPath]],
        previous_files: Dict[PurePosixPath, str],
    ) -> None:
        """
        清理已确认遍历目录中不再存在的直接子项。

        :param fileitem: 当前已列举的目录
        :param sub_files: 当前目录返回的直接子项
        :param files_info: 当前正在构建的快照
        :param previous_children: 旧快照目录直接子项索引
        :param previous_files: 旧快照路径反向索引
        """
        if not fileitem.path:
            return
        directory_path = PurePosixPath(fileitem.path)
        child_paths = {
            PurePosixPath(sub_file.path)
            for sub_file in sub_files
            if sub_file.path
        }
        for direct_child_path in previous_children.pop(directory_path, ()):
            if direct_child_path not in child_paths:
                StorageBase._snapshot_remove_previous_subtree(
                    direct_child_path, files_info, previous_children, previous_files
                )

    def _snapshot_file(
        self,
        fileitem: _SchemaFileItem,
        files_info: _SnapshotInfo,
        previous_children: Dict[PurePosixPath, set[PurePosixPath]],
        previous_files: Dict[PurePosixPath, str],
        last_snapshot_time: Optional[float],
        max_depth: int,
        current_depth: int = 0,
    ) -> None:
        """
        递归读取目录并更新快照文件信息。

        :param fileitem: 当前文件或目录
        :param files_info: 当前正在构建的快照
        :param previous_children: 旧快照目录直接子项索引
        :param previous_files: 旧快照路径反向索引
        :param last_snapshot_time: 上次快照的 Unix 秒级时间
        :param max_depth: 最大递归深度
        :param current_depth: 当前递归深度
        """
        try:
            if not fileitem.path:
                return
            if fileitem.type == "dir":
                if current_depth >= max_depth:
                    return

                # 根目录每轮至少列举一次；子目录按修改时间跳过未变化分支。
                if (current_depth > 0 and
                        self.snapshot_check_folder_modtime and
                        last_snapshot_time and
                        fileitem.modify_time and
                        fileitem.modify_time <= last_snapshot_time):
                    return

                # 只有目录列表成功返回后才清理旧基线，查询异常时保留待下轮重试。
                sub_files = self._snapshot_list(fileitem)
                if sub_files is None:
                    return
                sub_files = list(sub_files)
                StorageBase._snapshot_remove_deleted_children(
                    self,
                    fileitem, sub_files, files_info, previous_children, previous_files
                )
                for sub_file in sub_files:
                    StorageBase._snapshot_file(
                        self,
                        sub_file,
                        files_info,
                        previous_children,
                        previous_files,
                        last_snapshot_time,
                        max_depth,
                        current_depth + 1,
                    )
            else:
                # 始终记录文件完整信息，由 compare_snapshots 负责检测变化。
                files_info[fileitem.path] = {
                    'size': fileitem.size or 0,
                    'modify_time': getattr(fileitem, 'modify_time', 0),
                    'fileid': getattr(fileitem, 'fileid', None),
                    'type': fileitem.type
                }
        except Exception as error:
            self._snapshot_read_error(fileitem, error)

    def snapshot(self, path: Path, last_snapshot_time: Optional[float] = None, max_depth: int = 5,
                 previous_snapshot: Optional[_SnapshotInfo] = None) -> Optional[_SnapshotInfo]:
        """
        递归记录文件信息，并复用增量快照中未变化的子树。

        空字典为有效结果；返回 None 时调用方必须保留旧基线。
        普通查询模式仍沿用具体存储的容错行为，无法保证区分缺失与故障。

        :param path: 快照根路径
        :param last_snapshot_time: 上一轮快照的 Unix 秒级时间；为空时不按目录时间跳过子树
        :param max_depth: 目录递归深度上限，根目录深度为 0，默认 5
        :param previous_snapshot: 上一轮完整快照；保留未遍历子树并删除已确认消失的文件
        :return: 路径到文件大小、修改时间、标识和类型的映射，不含目录；严格查询失败返回 None
        """
        strict_query = self.snapshot_strict_query
        root_path = PurePosixPath(path.as_posix())
        files_info, previous_children, previous_files = StorageBase._snapshot_previous_state(
            root_path, previous_snapshot
        )

        try:
            fileitem = self.get_item_strict(path) if strict_query else self.get_item(path)
            if not fileitem:
                return {}
            StorageBase._snapshot_file(
                self,
                fileitem,
                files_info,
                previous_children,
                previous_files,
                last_snapshot_time,
                max_depth,
            )
        except StorageQueryError as err:
            logger.warning(f"存储快照失败，保留上次基线: {self.schema}:{path} - {err}")
            return None

        return files_info
