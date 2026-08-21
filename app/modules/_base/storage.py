"""存储后端基类与存储模块业务样板。

``StorageBase`` 定义单个存储后端的读写契约，``_StorageModuleBase`` 把一个存储后端
按实例包装成可被分发触达的一级模块：模块按实例名各持有一个后端对象，能力方法按
存储令牌自筛，令牌的类型部分不属于本存储、或该类型下没有令牌指定的实例时返回
``None`` 让给下一个模块。
"""
from abc import ABCMeta, abstractmethod
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Optional, List, Dict, Tuple, Callable, Union

from tqdm import tqdm

from app.schemas.file import FileURI as _SchemaFileURI
from app.schemas.file import StorageUsage as _SchemaStorageUsage
from app.schemas.system import StorageConf as _SchemaStorageConf
from app.schemas.workflow import FileItem as _SchemaFileItem
from app.modules import _ModuleBase
from app.runtime.progress import ProgressHelper
from app.runtime.storages import storage_config_port
from app.runtime.extensions.storage_registry import (
    storage_backend_identity,
    storage_backend_registry,
)
from app.runtime.log import logger
from app.schemas.exception import StorageQueryError
from app.schemas.types import StorageAction, StorageSchema
from app.foundation.crypto import HashUtils


def transfer_process(path: str) -> Callable[[int | float], None]:
    """
    传输进度回调
    """
    pbar = tqdm(total=100, desc="进度", unit="%")
    progress = ProgressHelper(HashUtils.md5(path))
    progress.start()

    def update_progress(percent: Union[int, float]) -> None:
        """
        更新进度百分比
        """
        percent_value = round(percent, 2) if isinstance(percent, float) else percent
        pbar.n = percent_value
        # 更新进度
        pbar.refresh()
        progress.update(value=percent_value, text=f"{path} 进度：{percent_value}%")
        # 完成时结束
        if percent_value >= 100:
            progress.end()
            pbar.close()

    return update_progress


class StorageBase(metaclass=ABCMeta):
    """
    存储基类

    一个对象服务一个存储实例，``storage_instance`` 为该对象所属的实例名，
    ``None`` 表示该存储类型的默认实例位。
    """
    schema = None
    transtype = {}
    snapshot_check_folder_modtime = True
    storage_instance: Optional[str] = None

    @abstractmethod
    def init_storage(self):
        """
        初始化
        """
        pass

    def generate_qrcode(self, *args, **kwargs) -> Optional[Tuple[dict, str]]:
        """生成存储登录二维码"""
        pass

    def generate_auth_url(self, *args, **kwargs) -> Optional[Tuple[dict, str]]:
        """
        生成 OAuth2 授权 URL
        """
        return {}, "此存储不支持 OAuth2 授权"

    def check_login(self, *args, **kwargs) -> Optional[Dict[str, str]]:
        """检查存储登录状态"""
        pass

    def get_config(self) -> Optional[_SchemaStorageConf]:
        """
        获取配置
        """
        return storage_config_port.resolve().get_storage(storage_backend_identity(self))

    def get_conf(self) -> dict:
        """
        获取配置
        """
        conf = self.get_config()
        return conf.config if conf else {}

    def set_config(self, conf: dict):
        """
        设置配置
        """
        storage_config_port.resolve().set_storage(storage_backend_identity(self), conf)
        self.init_storage()

    def support_transtype(self) -> dict:
        """
        支持的整理方式
        """
        return self.transtype

    def is_support_transtype(self, transtype: str) -> bool:
        """
        是否支持整理方式
        """
        return transtype in self.transtype

    def reset_config(self):
        """
        重置置配置
        """
        storage_config_port.resolve().reset_storage(storage_backend_identity(self))
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
        检查存储是否可用
        """
        pass

    @abstractmethod
    def list(self, fileitem: _SchemaFileItem) -> List[_SchemaFileItem]:
        """
        浏览文件
        """
        pass

    @abstractmethod
    def create_folder(self, fileitem: _SchemaFileItem, name: str) -> Optional[_SchemaFileItem]:
        """
        创建目录
        :param fileitem: 父目录
        :param name: 目录名
        """
        pass

    @abstractmethod
    def get_folder(self, path: Path) -> Optional[_SchemaFileItem]:
        """
        获取目录，如目录不存在则创建
        """
        pass

    @abstractmethod
    def get_item(self, path: Path) -> Optional[_SchemaFileItem]:
        """
        获取文件或目录，不存在返回None
        """
        pass

    def get_item_strict(self, path: Path) -> Optional[_SchemaFileItem]:
        """
        获取文件或目录，确认不存在返回None；无法确认状态时抛出 StorageQueryError。

        默认保守失败：未覆写的存储无法区分「不存在」与「查询失败」，沿用
        get_item() 会让 overwrite_mode=size 的覆盖保护在查询失败时被绕过，
        把「无法确认」当成「目标不存在」而放行覆盖。具体存储必须先实现
        「确认不存在」的判定，再覆写本方法。
        """
        raise StorageQueryError(f"存储 {self.schema} 未实现严格查询，无法确认目标状态: {path}")

    def get_parent(self, fileitem: _SchemaFileItem) -> Optional[_SchemaFileItem]:
        """
        获取父目录
        """
        return self.get_item(Path(fileitem.path).parent)

    @abstractmethod
    def delete(self, fileitem: _SchemaFileItem) -> bool:
        """
        删除文件
        """
        pass

    @abstractmethod
    def rename(self, fileitem: _SchemaFileItem, name: str) -> bool:
        """
        重命名文件
        """
        pass

    @abstractmethod
    def download(self, fileitem: _SchemaFileItem, path: Path = None) -> Path:
        """
        下载文件，保存到本地，返回本地临时文件地址
        :param fileitem: 文件项
        :param path: 文件保存路径
        """
        pass

    @abstractmethod
    def upload(self, fileitem: _SchemaFileItem, path: Path,
               new_name: Optional[str] = None) -> Optional[_SchemaFileItem]:
        """
        上传文件
        :param fileitem: 上传目录项
        :param path: 本地文件路径
        :param new_name: 上传后文件名
        """
        pass

    @abstractmethod
    def detail(self, fileitem: _SchemaFileItem) -> Optional[_SchemaFileItem]:
        """
        获取文件详情
        """
        pass

    @abstractmethod
    def copy(self, fileitem: _SchemaFileItem, path: Path, new_name: str) -> bool:
        """
        复制文件
        :param fileitem: 文件项
        :param path: 目标目录
        :param new_name: 新文件名
        """
        pass

    @abstractmethod
    def move(self, fileitem: _SchemaFileItem, path: Path, new_name: str) -> bool:
        """
        移动文件
        :param fileitem: 文件项
        :param path: 目标目录
        :param new_name: 新文件名
        """
        pass

    @abstractmethod
    def link(self, fileitem: _SchemaFileItem, target_file: Path) -> bool:
        """
        硬链接文件
        """
        pass

    @abstractmethod
    def softlink(self, fileitem: _SchemaFileItem, target_file: Path) -> bool:
        """
        软链接文件
        """
        pass

    @abstractmethod
    def usage(self) -> Optional[_SchemaStorageUsage]:
        """
        存储使用情况
        """
        pass

    def snapshot(self, path: Path, last_snapshot_time: float = None, max_depth: int = 5,
                 previous_snapshot: Optional[Dict[str, Dict]] = None) -> Dict[str, Dict]:
        """
        快照文件系统，输出所有层级文件信息（不含目录）
        :param path: 路径
        :param last_snapshot_time: 上次快照时间，用于增量快照
        :param max_depth: 最大递归深度，避免过深遍历
        :param previous_snapshot: 上次完整快照，用于保留未变化目录并清理已删除文件
        """
        root_path = PurePosixPath(path.as_posix())
        files_info = {
            file_path: file_info
            for file_path, file_info in (previous_snapshot or {}).items()
            if PurePosixPath(file_path).is_relative_to(root_path)
        }

        def __remove_deleted_children(_fileitm: _SchemaFileItem,
                                      sub_files: List[_SchemaFileItem]) -> None:
            """
            清理已确认遍历目录中不再存在的直接子项。
            未变化的子目录仍保留旧基线，避免增量遍历将其误删。
            """
            directory_path = PurePosixPath(_fileitm.path)
            child_paths = {PurePosixPath(sub_file.path) for sub_file in sub_files}
            for old_file_path in list(files_info):
                try:
                    relative_path = PurePosixPath(old_file_path).relative_to(directory_path)
                except ValueError:
                    continue
                if not relative_path.parts:
                    continue
                direct_child_path = directory_path / relative_path.parts[0]
                if direct_child_path not in child_paths:
                    files_info.pop(old_file_path, None)

        def __snapshot_file(_fileitm: _SchemaFileItem, current_depth: int = 0):
            """
            递归获取文件信息
            """
            try:
                if _fileitm.type == "dir":
                    # 检查递归深度限制
                    if current_depth >= max_depth:
                        return

                    # 根目录每轮至少列举一次，用于清理已移走的直接子项；子目录仍按修改时间增量遍历
                    if (current_depth > 0 and
                            self.snapshot_check_folder_modtime and
                            last_snapshot_time and
                            _fileitm.modify_time and
                            _fileitm.modify_time <= last_snapshot_time):
                        return

                    # 只有目录列表成功返回后才清理旧基线，查询异常时继续保留待下轮重试
                    sub_files = self.list(_fileitm)
                    if sub_files is None:
                        return
                    sub_files = list(sub_files)
                    __remove_deleted_children(_fileitm, sub_files)
                    for sub_file in sub_files:
                        __snapshot_file(sub_file, current_depth + 1)
                else:
                    # 记录文件的完整信息用于比对（始终包含所有文件，由 compare_snapshots 负责检测变化）
                    files_info[_fileitm.path] = {
                        'size': _fileitm.size or 0,
                        'modify_time': getattr(_fileitm, 'modify_time', 0),
                        'fileid': getattr(_fileitm, 'fileid', None),
                        'type': _fileitm.type
                    }

            except Exception as e:
                logger.debug(f"Snapshot error for {_fileitm.path}: {e}")

        fileitem = self.get_item(path)
        if not fileitem:
            return {}

        __snapshot_file(fileitem)

        return files_info


def list_storage_files(storage: StorageBase, fileitem: _SchemaFileItem,
                       recursion: Optional[bool] = False) -> List[_SchemaFileItem]:
    """
    浏览目录下的文件项

    :param storage: 存储操作对象
    :param fileitem: 源文件项
    :param recursion: 是否递归，递归时只返回文件
    :return: 文件项列表
    """
    result: List[_SchemaFileItem] = []

    def __get_files(_item: _SchemaFileItem, _r: Optional[bool] = False):
        """
        递归处理
        """
        _items = storage.list(_item)
        if _items:
            if _r:
                for t in _items:
                    if t.type == "dir":
                        __get_files(t, _r)
                    else:
                        result.append(t)
            else:
                result.extend(_items)

    __get_files(fileitem, recursion)

    return result


def any_storage_file(storage: StorageBase, fileitem: _SchemaFileItem,
                     extensions: list = None) -> bool:
    """
    查询目录下是否存在指定扩展名的任意文件

    :param storage: 存储操作对象
    :param fileitem: 源文件项
    :param extensions: 扩展名列表，为空表示存在任意文件即可
    :return: 存在符合条件的文件时为 True
    """

    def __any_file(_item: _SchemaFileItem) -> bool:
        """
        递归处理
        """
        _items = storage.list(_item)
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


@dataclass(frozen=True, slots=True)
class StorageInstanceSpec:
    """存储模块要扇出的一个存储实例。

    :param instance: 实例名，None 表示该存储类型的默认实例位，裸令牌指向它
    :param is_default: 该具名实例是否为所属存储类型的默认实例，未具名时不生效
    """

    instance: Optional[str] = None
    is_default: bool = False


def select_default_storage(
    instances: List[Tuple[StorageInstanceSpec, StorageBase]]
) -> Optional[StorageBase]:
    """
    裁决一组存储实例中的默认实例，与存储后端注册表的默认裁决同一套规则

    未具名实例占据默认实例位，优先命中；全为具名实例时只认唯一一个自称默认的；
    没有默认、或多个实例同时自称默认，一律认定为无默认，绝不按登记顺序取任意一个。

    :param instances: 已建立的 (实例描述, 存储操作对象) 序列
    :return: 默认实例的存储操作对象；裁决不出默认实例时为 None
    """
    for spec, storage in instances:
        if spec.instance is None:
            return storage
    marked = [storage for spec, storage in instances if spec.is_default]
    return marked[0] if len(marked) == 1 else None


class _StorageModuleBase(_ModuleBase):
    """
    存储模块业务样板基类。

    子类只需声明 ``storage_class``，本基类按实例建立后端对象、逐个登记到存储后端
    注册表，并按存储令牌自筛后转发全部存储能力方法。
    """

    # 本模块承载的存储后端类，由子类声明
    storage_class: type = None

    def __init__(self) -> None:
        """初始化模块并留空按实例组织的存储操作对象表。"""
        super().__init__()
        self._storages: Dict[Optional[str], StorageBase] = {}
        self._default_storage: Optional[StorageBase] = None

    @classmethod
    def storage_id(cls) -> Optional[str]:
        """
        获取本模块承载的存储标识

        :return: 存储标识；后端未声明标识时为 None
        """
        return storage_backend_identity(cls.storage_class)

    def _instance_specs(self) -> Tuple[StorageInstanceSpec, ...]:
        """
        列出本模块要扇出的存储实例

        存储配置是一个存储类型一份，因此只产出默认实例位。覆写本方法即可让同一个
        模块按实例扇出多个后端对象。

        :return: 存储实例描述元组
        """
        return (StorageInstanceSpec(),)

    def _create_storage(self, instance: Optional[str]) -> StorageBase:
        """
        构造指定实例的存储操作对象

        :param instance: 实例名，None 表示该存储类型的默认实例位
        :return: 该实例的存储操作对象
        """
        storage = self.storage_class()
        storage.storage_instance = instance
        return storage

    def init_module(self) -> None:
        """按实例建立存储操作对象，并把后端逐个登记到存储后端注册表。

        单个实例构造失败只跳过它自己，本模块其余实例照常建立——一条坏配置不应
        让整个存储模块连同其余可用实例一起失效。登记被拒的实例同样不予持有，
        模块持有的实例与注册表可取用的实例始终一致。
        """
        instances: List[Tuple[StorageInstanceSpec, StorageBase]] = []
        for spec in self._instance_specs():
            try:
                storage = self._create_storage(spec.instance)
            except Exception as err:
                logger.error(
                    f"【存储】{self.__class__.__name__} 实例 {spec.instance or '默认'} 构造失败，已跳过：{err}"
                )
                continue
            registered = storage_backend_registry.register(
                self.storage_class,
                owner=self.__class__.__name__,
                instance=spec.instance,
                is_default=spec.is_default,
            )
            if not registered:
                continue
            instances.append((spec, storage))
        self._storages = {spec.instance: storage for spec, storage in instances}
        self._default_storage = select_default_storage(instances)

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        """存储模块不使用应用设置开关。"""
        pass

    @classmethod
    def get_subtype(cls) -> StorageSchema:
        """获取模块子类型，取存储后端声明的存储标识。"""
        return getattr(cls.storage_class, "schema", None)

    def stop(self) -> None:
        """按实例注销存储后端登记并释放存储操作对象。

        注销逐个实例位进行并给出自身归属，某个实例位若已被扩展接管则跳过——
        本模块停止不应连带撤掉接管方的登记，同标识其余实例位也不受牵连。
        """
        storage_id = self.storage_id()
        if storage_id:
            for instance in self._storages:
                storage_backend_registry.unregister(
                    _SchemaFileURI.join_storage(storage_id, instance),
                    owner=self.__class__.__name__,
                )
        self._storages = {}
        self._default_storage = None

    def test(self) -> Optional[Tuple[bool, str]]:
        """存储可用性由文件整理模块按目录配置统一自检，本模块不单独给出结论。"""
        return None

    def _claim(self, storage: Optional[str]) -> Optional[StorageBase]:
        """
        判断请求是否属于本存储并取用该实例的存储操作对象

        令牌的类型部分与本模块的存储标识一致才认领，取用的是令牌指定的那个实例；
        本模块没有该实例时让出，绝不回落到默认实例——回落等于拿默认实例的账号去
        执行用户没选的实例的操作。裸令牌指向默认实例，裁决不出默认实例时同样让出。
        畸形令牌不与任何存储类型相等，因此一律让出。

        :param storage: 请求携带的存储令牌，如 u115 或 u115@work
        :return: 该实例的存储操作对象；令牌不属于本存储或本模块没有该实例时为 None
        """
        if not _SchemaFileURI.is_same_storage_type(storage, self.storage_id()):
            return None
        instance = _SchemaFileURI.storage_parts(storage)[1]
        if instance is None:
            return self._default_storage
        return self._storages.get(instance)

    def list_files(self, fileitem: _SchemaFileItem,
                   recursion: Optional[bool] = False) -> Optional[List[_SchemaFileItem]]:
        """
        浏览文件

        :param fileitem: 源文件项，存储标识决定是否由本模块认领
        :param recursion: 是否递归，递归时只返回文件
        :return: 文件项列表；非本存储时为 None
        """
        storage = self._claim(fileitem.storage)
        if not storage:
            return None
        return list_storage_files(storage, fileitem, recursion)

    def any_files(self, fileitem: _SchemaFileItem, extensions: list = None) -> Optional[bool]:
        """
        查询当前目录下是否存在指定扩展名任意文件

        :param fileitem: 源文件项，存储标识决定是否由本模块认领
        :param extensions: 扩展名列表
        :return: 是否存在；非本存储时为 None
        """
        storage = self._claim(fileitem.storage)
        if not storage:
            return None
        return any_storage_file(storage, fileitem, extensions)

    def create_folder(self, fileitem: _SchemaFileItem, name: str) -> Optional[_SchemaFileItem]:
        """
        创建目录

        :param fileitem: 父目录项，存储标识决定是否由本模块认领
        :param name: 目录名
        :return: 创建的目录项；非本存储时为 None
        """
        storage = self._claim(fileitem.storage)
        if not storage:
            return None
        return storage.create_folder(fileitem, name)

    def get_folder(self, storage: str, path: Path) -> Optional[_SchemaFileItem]:
        """
        获取目录，如目录不存在则创建

        :param storage: 存储标识，决定是否由本模块认领
        :param path: 目录路径
        :return: 目录项；非本存储时为 None
        """
        storage_oper = self._claim(storage)
        if not storage_oper:
            return None
        return storage_oper.get_folder(path)

    def delete_file(self, fileitem: _SchemaFileItem) -> Optional[bool]:
        """
        删除文件或目录

        :param fileitem: 文件项，存储标识决定是否由本模块认领
        :return: 是否删除成功；非本存储时为 None
        """
        storage = self._claim(fileitem.storage)
        if not storage:
            return None
        return storage.delete(fileitem)

    def rename_file(self, fileitem: _SchemaFileItem, name: str) -> Optional[bool]:
        """
        重命名文件或目录

        :param fileitem: 文件项，存储标识决定是否由本模块认领
        :param name: 新名称
        :return: 是否重命名成功；非本存储时为 None
        """
        storage = self._claim(fileitem.storage)
        if not storage:
            return None
        return storage.rename(fileitem, name)

    def download_file(self, fileitem: _SchemaFileItem, path: Path = None) -> Optional[Path]:
        """
        下载文件

        :param fileitem: 文件项，存储标识决定是否由本模块认领
        :param path: 本地保存路径
        :return: 本地文件路径；非本存储时为 None
        """
        storage = self._claim(fileitem.storage)
        if not storage:
            return None
        return storage.download(fileitem, path=path)

    def upload_file(self, fileitem: _SchemaFileItem, path: Path,
                    new_name: Optional[str] = None) -> Optional[_SchemaFileItem]:
        """
        上传文件

        :param fileitem: 上传目录项，存储标识决定是否由本模块认领
        :param path: 本地文件路径
        :param new_name: 上传后的文件名
        :return: 上传后的文件项；非本存储时为 None
        """
        storage = self._claim(fileitem.storage)
        if not storage:
            return None
        return storage.upload(fileitem, path, new_name)

    def get_file_item(self, storage: str, path: Path) -> Optional[_SchemaFileItem]:
        """
        根据路径获取文件项

        :param storage: 存储标识，决定是否由本模块认领
        :param path: 文件路径
        :return: 文件项；非本存储时为 None
        """
        storage_oper = self._claim(storage)
        if not storage_oper:
            return None
        return storage_oper.get_item(path)

    def get_parent_item(self, fileitem: _SchemaFileItem) -> Optional[_SchemaFileItem]:
        """
        获取上级目录项

        :param fileitem: 文件项，存储标识决定是否由本模块认领
        :return: 上级目录项；非本存储时为 None
        """
        storage = self._claim(fileitem.storage)
        if not storage:
            return None
        return storage.get_parent(fileitem)

    def snapshot_storage(self, storage: str, path: Path,
                         last_snapshot_time: float = None, max_depth: int = 5,
                         previous_snapshot: Optional[Dict[str, Dict]] = None
                         ) -> Optional[Dict[str, Dict]]:
        """
        快照存储

        :param storage: 存储标识，决定是否由本模块认领
        :param path: 路径
        :param last_snapshot_time: 上次快照时间，用于增量快照
        :param max_depth: 最大递归深度，避免过深遍历
        :param previous_snapshot: 上次完整快照，用于增量对账
        :return: 快照结果；非本存储时为 None
        """
        storage_oper = self._claim(storage)
        if not storage_oper:
            return None
        return storage_oper.snapshot(
            path,
            last_snapshot_time=last_snapshot_time,
            max_depth=max_depth,
            previous_snapshot=previous_snapshot
        )

    def storage_manage(self, storage: str, action: StorageAction, **params) -> Optional[Dict[str, Any]]:
        """
        存储管理入口

        动作语义与参数解释交给具体存储实现，统一返回
        ``{"success": bool, "message": ..., "data": ...}``

        :param storage: 存储标识，决定是否由本模块认领
        :param action: 通用管理动作
        :param params: 动作参数，原样交给存储实现
        :return: 统一管理结果；非本存储时为 None
        """
        storage_oper = self._claim(storage)
        if not storage_oper:
            return None
        try:
            action = StorageAction(action)
        except ValueError:
            return {"success": False, "message": f"不支持的存储管理动作：{action}"}

        if action == StorageAction.SAVE_CONFIG:
            storage_oper.set_config(params.get("conf") or {})
            return {"success": True}
        if action == StorageAction.RESET_CONFIG:
            storage_oper.reset_config()
            return {"success": True}
        if action == StorageAction.SUPPORT_TRANSTYPE:
            # 与旧契约一致：返回值包装为 transtype，空结果同样返回成功空结构
            return {"success": True, "data": {"transtype": storage_oper.support_transtype() or {}}}
        if action == StorageAction.USAGE:
            # 实现返回 pydantic 模型，转为 dict 后才能透过通用响应的开放映射校验
            return {"success": True, "data": (storage_oper.usage() or _SchemaStorageUsage()).model_dump()}

        # 登录类动作：存储实现不支持时返回失败信息
        oper_method = action.value
        if not hasattr(storage_oper, oper_method):
            return {"success": False, "message": f"{storage} 不支持 {oper_method}"}
        result = getattr(storage_oper, oper_method)(**params)
        if result is None:
            return {"success": False, "message": f"{storage} 的 {oper_method} 执行失败"}
        data, errmsg = result
        return {"success": bool(data), "message": errmsg, "data": data}
