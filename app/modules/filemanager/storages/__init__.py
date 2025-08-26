from abc import ABCMeta, abstractmethod
from pathlib import Path
from typing import Optional, List, Dict, Tuple, Callable, Union

from tqdm import tqdm

from app import schemas
from app.helper.progress import ProgressHelper
from app.helper.storage import StorageHelper
from app.log import logger
from app.utils.crypto import HashUtils


def transfer_process(path: str, src_path: str = None, dest_path: str = None) -> Callable[[int | float], None]:
    """
    传输进度回调
    :param path: 文件路径
    :param src_path: 源路径（可选）
    :param dest_path: 目标路径（可选）
    """
    from app.core.config import settings
    from app.utils.system import SystemUtils
    
    # 检查是否启用进度显示
    if not settings.ENABLE_TRANSFER_PROGRESS:
        # 禁用进度显示时，返回空函数
        def no_progress(percent: Union[int, float]) -> None:
            pass
        return no_progress
    
    # 智能检测：如果源路径和目标路径都是挂载的同一网盘，则禁用进度显示
    if src_path and dest_path:
        src_path_obj = Path(src_path)
        dest_path_obj = Path(dest_path)
        
        # 检查是否都是网络挂载且是同一设备
        if (SystemUtils.is_network_mount(src_path_obj) and 
            SystemUtils.is_network_mount(dest_path_obj) and 
            SystemUtils.is_same_mount(src_path_obj, dest_path_obj)):
            
            logger.debug(f"检测到同网盘挂载传输，禁用进度显示: {src_path} -> {dest_path}")
            def no_progress(percent: Union[int, float]) -> None:
                pass
            return no_progress
    
    pbar = tqdm(total=100, desc="整理进度", unit="%")
    progress = ProgressHelper(HashUtils.md5(path))
    progress.start()

    def update_progress(percent: Union[int, float]) -> None:
        """
        更新进度百分比
        """
        percent_value = int(percent)
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
        pass

    def check_login(self, *args, **kwargs) -> Optional[Dict[str, str]]:
        pass

    def get_config(self) -> Optional[schemas.StorageConf]:
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

    @abstractmethod
    def check(self) -> bool:
        """
        检查存储是否可用
        """
        pass

    @abstractmethod
    def list(self, fileitem: schemas.FileItem) -> List[schemas.FileItem]:
        """
        浏览文件
        """
        pass

    @abstractmethod
    def create_folder(self, fileitem: schemas.FileItem, name: str) -> Optional[schemas.FileItem]:
        """
        创建目录
        :param fileitem: 父目录
        :param name: 目录名
        """
        pass

    @abstractmethod
    def get_folder(self, path: Path) -> Optional[schemas.FileItem]:
        """
        获取目录，如目录不存在则创建
        """
        pass

    @abstractmethod
    def get_item(self, path: Path) -> Optional[schemas.FileItem]:
        """
        获取文件或目录，不存在返回None
        """
        pass

    def get_parent(self, fileitem: schemas.FileItem) -> Optional[schemas.FileItem]:
        """
        获取父目录
        """
        return self.get_item(Path(fileitem.path).parent)

    @abstractmethod
    def delete(self, fileitem: schemas.FileItem) -> bool:
        """
        删除文件
        """
        pass

    @abstractmethod
    def rename(self, fileitem: schemas.FileItem, name: str) -> bool:
        """
        重命名文件
        """
        pass

    @abstractmethod
    def download(self, fileitem: schemas.FileItem, path: Path = None, 
                 src_path: str = None, dest_path: str = None) -> Path:
        """
        下载文件，保存到本地，返回本地临时文件地址
        :param fileitem: 文件项
        :param path: 文件保存路径
        :param src_path: 源路径（用于进度显示优化）
        :param dest_path: 目标路径（用于进度显示优化）
        """
        pass

    @abstractmethod
    def upload(self, fileitem: schemas.FileItem, path: Path,
               new_name: Optional[str] = None, 
               src_path: str = None, dest_path: str = None) -> Optional[schemas.FileItem]:
        """
        上传文件
        :param fileitem: 上传目录项
        :param path: 本地文件路径
        :param new_name: 上传后文件名
        :param src_path: 源路径（用于进度显示优化）
        :param dest_path: 目标路径（用于进度显示优化）
        """
        pass

    @abstractmethod
    def detail(self, fileitem: schemas.FileItem) -> Optional[schemas.FileItem]:
        """
        获取文件详情
        """
        pass

    @abstractmethod
    def copy(self, fileitem: schemas.FileItem, path: Path, new_name: str) -> bool:
        """
        复制文件
        :param fileitem: 文件项
        :param path: 目标目录
        :param new_name: 新文件名
        """
        pass

    @abstractmethod
    def move(self, fileitem: schemas.FileItem, path: Path, new_name: str) -> bool:
        """
        移动文件
        :param fileitem: 文件项
        :param path: 目标目录
        :param new_name: 新文件名
        """
        pass

    @abstractmethod
    def link(self, fileitem: schemas.FileItem, target_file: Path) -> bool:
        """
        硬链接文件
        """
        pass

    @abstractmethod
    def softlink(self, fileitem: schemas.FileItem, target_file: Path) -> bool:
        """
        软链接文件
        """
        pass

    @abstractmethod
    def usage(self) -> Optional[schemas.StorageUsage]:
        """
        存储使用情况
        """
        pass

    def snapshot(self, path: Path, last_snapshot_time: float = None, max_depth: int = 5) -> Dict[str, Dict]:
        """
        快照文件系统，输出所有层级文件信息（不含目录）
        :param path: 路径
        :param last_snapshot_time: 上次快照时间，用于增量快照
        :param max_depth: 最大递归深度，避免过深遍历
        """
        files_info = {}

        def __snapshot_file(_fileitm: schemas.FileItem, current_depth: int = 0):
            """
            递归获取文件信息
            """
            try:
                if _fileitm.type == "dir":
                    # 检查递归深度限制
                    if current_depth >= max_depth:
                        return

                    # 增量检查：如果目录修改时间早于上次快照，跳过
                    if (self.snapshot_check_folder_modtime and
                            last_snapshot_time and
                            _fileitm.modify_time and
                            _fileitm.modify_time <= last_snapshot_time):
                        return

                    # 遍历子文件
                    sub_files = self.list(_fileitm)
                    for sub_file in sub_files:
                        __snapshot_file(sub_file, current_depth + 1)
                else:
                    # 记录文件的完整信息用于比对
                    if getattr(_fileitm, 'modify_time', 0) > last_snapshot_time:
                        files_info[_fileitm.path] = {
                            'size': _fileitm.size or 0,
                            'modify_time': getattr(_fileitm, 'modify_time', 0),
                            'type': _fileitm.type
                        }

            except Exception as e:
                logger.debug(f"Snapshot error for {_fileitm.path}: {e}")

        fileitem = self.get_item(path)
        if not fileitem:
            return {}

        __snapshot_file(fileitem)

        return files_info
