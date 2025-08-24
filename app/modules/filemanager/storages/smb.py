import threading
import time
from pathlib import Path
from typing import List, Optional, Union

import smbclient
from smbclient import ClientConfig, register_session, reset_connection_cache
from smbprotocol.exceptions import SMBException, SMBResponseException, SMBAuthenticationError

from app import schemas
from app.core.config import settings, global_vars
from app.log import logger
from app.modules.filemanager import StorageBase
from app.modules.filemanager.storages import transfer_process
from app.schemas.types import StorageSchema
from app.utils.singleton import WeakSingleton

lock = threading.Lock()


class SMBConnectionError(Exception):
    """
    SMB 连接错误
    """
    pass


class SMB(StorageBase, metaclass=WeakSingleton):
    """
    SMB网络挂载存储相关操作 - 使用 smbclient 高级接口
    """

    # 存储类型
    schema = StorageSchema.SMB

    # 支持的整理方式
    transtype = {
        "move": "移动",
        "copy": "复制",
    }

    # 文件块大小，默认100MB
    chunk_size = 100 * 1024 * 1024

    def __init__(self):
        super().__init__()
        self._connected = False
        self._server_path = None
        self._host = None
        self._username = None
        self._password = None
        self._init_connection()

    def _init_connection(self):
        """
        初始化SMB连接配置
        """
        try:
            conf = self.get_conf()
            if not conf:
                return

            self._host = conf.get("host")
            self._username = conf.get("username")
            self._password = conf.get("password")
            domain = conf.get("domain", "")
            share = conf.get("share", "")
            port = conf.get("port", 445)

            if not all([self._host, share]):
                logger.error("【SMB】缺少必要的连接参数：host 和 share")
                return

            # 构建服务器路径
            self._server_path = f"\\\\{self._host}\\{share}"

            # 配置全局客户端设置
            ClientConfig(
                username=self._username,
                password=self._password,
                domain=domain if domain else None,
                connection_timeout=60,
                port=port,
                auth_protocol="negotiate",  # 使用协商认证
                require_secure_negotiate=False  # 匿名访问时可能需要关闭安全协商
            )

            # 注册会话以启用连接池
            register_session(
                self._host,
                username=self._username,
                password=self._password,
                port=port,
                encrypt=False,  # 根据需要启用加密
                connection_timeout=60
            )

            # 测试连接
            self._test_connection()

            self._connected = True
            # 判断是否为匿名访问
            if self._is_anonymous_access():
                logger.info(f"【SMB】匿名连接成功：{self._server_path}")
            else:
                logger.info(f"【SMB】认证连接成功：{self._server_path} (用户：{self._username})")

        except Exception as e:
            logger.error(f"【SMB】连接初始化失败：{e}")
            self._connected = False

    def _test_connection(self):
        """
        测试SMB连接
        """
        try:
            # 尝试列出根目录来测试连接
            smbclient.listdir(self._server_path)
        except SMBAuthenticationError as e:
            raise SMBConnectionError(f"SMB认证失败：{e}")
        except SMBResponseException as e:
            raise SMBConnectionError(f"SMB响应错误：{e}")
        except SMBException as e:
            raise SMBConnectionError(f"SMB连接错误：{e}")
        except Exception as e:
            raise SMBConnectionError(f"连接测试失败：{e}")

    def _is_anonymous_access(self) -> bool:
        """
        检查是否为匿名访问
        """
        return not self._username and not self._password

    def _check_connection(self):
        """
        检查SMB连接状态
        """
        if not self._connected or not self._server_path:
            raise SMBConnectionError("【SMB】连接未建立或已断开，请检查配置！")

    def _normalize_path(self, path: Union[str, Path]) -> str:
        """
        标准化路径格式为SMB路径
        """
        path_str = str(path)

        # 处理根路径
        if path_str in ["/", "\\"]:
            return self._server_path

        # 去除前导斜杠
        if path_str.startswith("/"):
            path_str = path_str[1:]

        # 构建完整的SMB路径
        if path_str:
            return f"{self._server_path}\\{path_str.replace('/', '\\')}"
        else:
            return self._server_path

    def _create_fileitem(self, stat_result, file_path: str, name: str) -> schemas.FileItem:
        """
        创建文件项
        """
        try:
            # 检查是否为目录
            is_directory = smbclient.path.isdir(file_path)

            # 处理路径
            relative_path = file_path.replace(self._server_path, "").replace("\\", "/")
            if not relative_path.startswith("/"):
                relative_path = "/" + relative_path

            if is_directory and not relative_path.endswith("/"):
                relative_path += "/"

            # 获取时间戳
            try:
                modify_time = int(stat_result.st_mtime)
            except (AttributeError, TypeError):
                modify_time = int(time.time())

            if is_directory:
                return schemas.FileItem(
                    storage=self.schema.value,
                    type="dir",
                    path=relative_path,
                    name=name,
                    basename=name,
                    modify_time=modify_time
                )
            else:
                return schemas.FileItem(
                    storage=self.schema.value,
                    type="file",
                    path=relative_path,
                    name=name,
                    basename=Path(name).stem,
                    extension=Path(name).suffix[1:] if Path(name).suffix else None,
                    size=getattr(stat_result, 'st_size', 0),
                    modify_time=modify_time
                )
        except Exception as e:
            logger.error(f"【SMB】创建文件项失败：{e}")
            # 返回基本的文件项信息
            return schemas.FileItem(
                storage=self.schema.value,
                type="file",
                path=file_path.replace(self._server_path, "").replace("\\", "/"),
                name=name,
                basename=Path(name).stem,
                modify_time=int(time.time())
            )

    def init_storage(self):
        """
        初始化存储
        """
        # 重置连接缓存
        reset_connection_cache()
        self._init_connection()

    def check(self) -> bool:
        """
        检查存储是否可用
        """
        if not self._connected:
            return False

        try:
            self._test_connection()
            return True
        except Exception as e:
            logger.debug(f"【SMB】连接检查失败：{e}")
            self._connected = False
            return False

    def list(self, fileitem: schemas.FileItem) -> List[schemas.FileItem]:
        """
        浏览文件
        """
        try:
            self._check_connection()

            if fileitem.type == "file":
                item = self.detail(fileitem)
                if item:
                    return [item]
                return []

            # 构建SMB路径
            smb_path = self._normalize_path(fileitem.path.rstrip("/"))

            # 列出目录内容
            try:
                entries = smbclient.listdir(smb_path)
            except SMBResponseException as e:
                logger.error(f"【SMB】列出目录失败: {smb_path} - {e}")
                return []
            except SMBException as e:
                logger.error(f"【SMB】列出目录失败: {smb_path} - {e}")
                return []

            items = []
            for entry in entries:
                if entry in [".", ".."]:
                    continue

                entry_path = f"{smb_path}\\{entry}"
                try:
                    stat_result = smbclient.stat(entry_path)
                    item = self._create_fileitem(stat_result, entry_path, entry)
                    items.append(item)
                except Exception as e:
                    logger.debug(f"【SMB】获取文件信息失败: {entry_path} - {e}")
                    continue

            return items
        except Exception as e:
            logger.error(f"【SMB】列出文件失败: {e}")
            return []

    def create_folder(self, fileitem: schemas.FileItem, name: str) -> Optional[schemas.FileItem]:
        """
        创建目录
        """
        try:
            self._check_connection()

            parent_path = self._normalize_path(fileitem.path.rstrip("/"))
            new_path = f"{parent_path}\\{name}"

            # 创建目录
            smbclient.mkdir(new_path)

            # 返回创建的目录信息
            return schemas.FileItem(
                storage=self.schema.value,
                type="dir",
                path=f"{fileitem.path.rstrip('/')}/{name}/",
                name=name,
                basename=name,
                modify_time=int(time.time())
            )
        except Exception as e:
            logger.error(f"【SMB】创建目录失败: {e}")
            return None

    def get_folder(self, path: Path) -> Optional[schemas.FileItem]:
        """
        获取目录，如目录不存在则创建
        """
        # 检查目录是否存在
        folder = self.get_item(path)
        if folder:
            return folder

        # 逐级创建目录
        parts = path.parts
        current_path = Path("/")

        for part in parts[1:]:  # 跳过根目录
            current_path = current_path / part
            folder = self.get_item(current_path)
            if not folder:
                parent_folder = self.get_item(current_path.parent)
                if not parent_folder:
                    logger.error(f"【SMB】父目录不存在: {current_path.parent}")
                    return None
                folder = self.create_folder(parent_folder, part)
                if not folder:
                    return None

        return folder

    def get_item(self, path: Path) -> Optional[schemas.FileItem]:
        """
        获取文件或目录，不存在返回None
        """
        try:
            self._check_connection()

            # 处理根目录
            if str(path) == "/":
                return schemas.FileItem(
                    storage=self.schema.value,
                    type="dir",
                    path="/",
                    name="",
                    basename="",
                    modify_time=int(time.time())
                )

            smb_path = self._normalize_path(str(path).rstrip("/"))

            # 检查路径是否存在
            if not smbclient.path.exists(smb_path):
                return None

            stat_result = smbclient.stat(smb_path)
            file_name = Path(path).name

            return self._create_fileitem(stat_result, smb_path, file_name)

        except Exception as e:
            logger.debug(f"【SMB】获取文件项失败: {e}")
            return None

    def detail(self, fileitem: schemas.FileItem) -> Optional[schemas.FileItem]:
        """
        获取文件详情
        """
        return self.get_item(Path(fileitem.path))

    def delete(self, fileitem: schemas.FileItem) -> bool:
        """
        删除文件或目录
        """
        try:
            self._check_connection()

            smb_path = self._normalize_path(fileitem.path.rstrip("/"))

            if fileitem.type == "dir":
                # 删除目录
                smbclient.rmdir(smb_path)
            else:
                # 删除文件
                smbclient.remove(smb_path)

            logger.info(f"【SMB】删除成功: {fileitem.path}")
            return True
        except Exception as e:
            logger.error(f"【SMB】删除失败: {e}")
            return False

    def rename(self, fileitem: schemas.FileItem, name: str) -> bool:
        """
        重命名文件
        """
        try:
            self._check_connection()

            old_path = self._normalize_path(fileitem.path.rstrip("/"))
            parent_path = Path(fileitem.path).parent
            new_path = self._normalize_path(str(parent_path / name))

            # 重命名
            smbclient.rename(old_path, new_path)

            logger.info(f"【SMB】重命名成功: {fileitem.path} -> {name}")
            return True
        except Exception as e:
            logger.error(f"【SMB】重命名失败: {e}")
            return False

    def download(self, fileitem: schemas.FileItem, path: Path = None) -> Optional[Path]:
        """
        带实时进度显示的下载
        """
        local_path = path or settings.TEMP_PATH / fileitem.name
        smb_path = self._normalize_path(fileitem.path)
        try:
            self._check_connection()

            # 确保本地目录存在
            local_path.parent.mkdir(parents=True, exist_ok=True)

            # 获取文件大小
            file_size = fileitem.size

            # 初始化进度条
            logger.info(f"【SMB】开始下载: {fileitem.name} -> {local_path}")
            progress_callback = transfer_process(Path(fileitem.path).as_posix())

            # 使用更高效的文件传输方式
            with smbclient.open_file(smb_path, mode="rb") as src_file:
                with open(local_path, "wb") as dst_file:
                    downloaded_size = 0
                    while True:
                        if global_vars.is_transfer_stopped(fileitem.path):
                            logger.info(f"【SMB】{fileitem.path} 下载已取消！")
                            return None
                        chunk = src_file.read(self.chunk_size)
                        if not chunk:
                            break
                        dst_file.write(chunk)
                        downloaded_size += len(chunk)
                        # 更新进度
                        if file_size:
                            progress = (downloaded_size * 100) / file_size
                            progress_callback(progress)

            # 完成下载
            progress_callback(100)
            logger.info(f"【SMB】下载完成: {fileitem.name}")
            return local_path

        except Exception as e:
            logger.error(f"【SMB】下载失败: {fileitem.name} - {e}")
            # 删除可能部分下载的文件
            if local_path.exists():
                local_path.unlink()
            return None

    def upload(self, fileitem: schemas.FileItem, path: Path,
               new_name: Optional[str] = None) -> Optional[schemas.FileItem]:
        """
        带实时进度显示的上传
        """
        target_name = new_name or path.name
        target_path = Path(fileitem.path) / target_name
        smb_path = self._normalize_path(str(target_path))

        try:
            self._check_connection()

            # 获取文件大小
            file_size = path.stat().st_size

            # 初始化进度条
            logger.info(f"【SMB】开始上传: {path} -> {target_path}")
            progress_callback = transfer_process(path.as_posix())

            # 使用更高效的文件传输方式
            with open(path, "rb") as src_file:
                with smbclient.open_file(smb_path, mode="wb") as dst_file:
                    uploaded_size = 0
                    while True:
                        if global_vars.is_transfer_stopped(path.as_posix()):
                            logger.info(f"【SMB】{path} 上传已取消！")
                            return None
                        chunk = src_file.read(self.chunk_size)
                        if not chunk:
                            break
                        dst_file.write(chunk)
                        uploaded_size += len(chunk)
                        # 更新进度
                        if file_size:
                            progress = (uploaded_size * 100) / file_size
                            progress_callback(progress)

            # 完成上传
            progress_callback(100)
            logger.info(f"【SMB】上传完成: {target_name}")

            # 返回上传后的文件信息
            return self.get_item(target_path)

        except Exception as e:
            logger.error(f"【SMB】上传失败: {target_name} - {e}")
            return None

    def copy(self, fileitem: schemas.FileItem, path: Path, new_name: str) -> bool:
        """
        复制文件
        """
        try:
            # 下载到临时文件
            temp_file = self.download(fileitem)
            if not temp_file:
                return False

            # 获取目标目录
            target_folder = self.get_item(path)
            if not target_folder:
                return False

            # 上传到目标位置
            result = self.upload(target_folder, temp_file, new_name)

            # 删除临时文件
            if temp_file.exists():
                temp_file.unlink()

            return result is not None
        except Exception as e:
            logger.error(f"【SMB】复制失败: {e}")
            return False

    def move(self, fileitem: schemas.FileItem, path: Path, new_name: str) -> bool:
        """
        移动文件
        """
        try:
            # 先复制
            if not self.copy(fileitem, path, new_name):
                return False

            # 再删除原文件
            if not self.delete(fileitem):
                logger.warn(f"【SMB】删除原文件失败: {fileitem.path}")
                return False

            return True
        except Exception as e:
            logger.error(f"【SMB】移动失败: {e}")
            return False

    def link(self, fileitem: schemas.FileItem, target_file: Path) -> bool:
        pass

    def softlink(self, fileitem: schemas.FileItem, target_file: Path) -> bool:
        pass

    def usage(self) -> Optional[schemas.StorageUsage]:
        """
        存储使用情况
        """
        try:
            self._check_connection()
            volume_stat = smbclient.stat_volume(self._server_path)
            return schemas.StorageUsage(
                total=volume_stat.total_size,
                available=volume_stat.caller_available_size
            )

        except Exception as e:
            logger.error(f"【SMB】获取存储使用情况失败: {e}")
            return None

    def __del__(self):
        """
        析构函数，清理连接
        """
        try:
            # smbclient 自动管理连接池，但我们可以重置缓存
            if hasattr(self, '_connected') and self._connected:
                reset_connection_cache()
        except Exception as e:
            logger.debug(f"【SMB】清理连接失败: {e}")
