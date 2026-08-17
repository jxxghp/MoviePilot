from abc import ABCMeta, abstractmethod
from pathlib import Path, PurePosixPath
from typing import Optional, List, Dict, Tuple, Callable, Union

from tqdm import tqdm

from app.schemas.file import StorageUsage as _SchemaStorageUsage
from app.schemas.system import StorageConf as _SchemaStorageConf
from app.schemas.workflow import FileItem as _SchemaFileItem
from app.runtime.progress import ProgressHelper
from app.application.storage import StorageHelper
from app.runtime.log import logger
from app.schemas.exception import StorageQueryError
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
    """
    schema = None
    transtype = {}
    snapshot_check_folder_modtime = True

    def __init__(self):
        self.storagehelper = StorageHelper()

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
        return self.storagehelper.get_storage(self.schema.value)

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
        self.storagehelper.set_storage(self.schema.value, conf)
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
