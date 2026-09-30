import errno
import ntpath
import threading
import time
from pathlib import Path
from typing import List, Optional, Union

from app.foundation.singleton import WeakSingleton
from app.modules.filemanager.storages import StorageBase, transfer_process
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.runtime.stop import runtime_stop_state
from app.schemas.exception import StorageQueryError
from app.schemas.file import StorageUsage as _SchemaStorageUsage
from app.schemas.types import StorageSchema
from app.schemas.workflow import FileItem as _SchemaFileItem

# SMB SDK（smbclient/smbprotocol/spnego）约 12MB，存储发现会导入全部存储实现，
# 因此各方法内按需导入，未配置 SMB 时不加载

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
        "link": "硬链接",
    }

    # 文件块大小，默认10MB
    chunk_size = 10 * 1024 * 1024

    @property
    def snapshot_check_folder_modtime(self) -> bool:
        """SMB 目录时间不保证传播后代变化，快照必须遍历到配置的深度上限。"""
        return False

    @property
    def snapshot_strict_query(self) -> bool:
        """SMB 快照必须区分真实空目录与读取失败，防止错误更新监控基线。"""
        return True

    def __init__(self):
        """加载当前 SMB 配置并尝试建立连接。"""
        super().__init__()
        self._connected = False
        self._server_path: Optional[str] = None
        self._shares: dict[str, str] = {}
        self._multi_share = False
        self._host = None
        self._username = None
        self._password = None

        self._init_connection()

    def _init_connection(self):
        """
        按当前配置建立连接，清除上次状态以免配置无效时继续使用旧共享。
        """
        self._connected = False
        self._server_path = None
        self._shares = {}
        self._multi_share = False
        from smbclient import ClientConfig, register_session
        try:
            conf = self.get_conf()
            if not conf:
                return

            self._host = conf.get("host")
            self._username = conf.get("username")
            self._password = conf.get("password")
            domain = conf.get("domain", "")
            port = conf.get("port", 445)

            if not self._host:
                return
            self._configure_shares(conf)

            # 配置全局客户端设置
            ClientConfig(
                username=self._username,
                password=self._password,
                domain=domain if domain else None,
                connection_timeout=60,
                port=port,
                auth_protocol="negotiate",  # 使用协商认证
                require_secure_negotiate=False,  # 匿名访问时可能需要关闭安全协商
            )

            # 注册会话以启用连接池
            register_session(
                self._host,
                username=self._username,
                password=self._password,
                port=port,
                encrypt=False,  # 根据需要启用加密
                connection_timeout=60,
            )

            # 测试连接
            self._test_connection()

            self._connected = True
            # 判断是否为匿名访问
            if self._is_anonymous_access():
                logger.info(f"【SMB】匿名连接成功：{self._server_path}")
            else:
                logger.info(
                    f"【SMB】认证连接成功：{self._server_path} (用户：{self._username})"
                )

        except Exception as e:
            logger.error(f"【SMB】连接初始化失败：{e}")
            self._connected = False

    def _configure_shares(self, conf: dict[str, object]) -> None:
        """显式 shares 配置启用共享根命名空间，旧 share 保持共享内路径。"""
        self._multi_share = "shares" in conf
        names = conf.get("shares") if self._multi_share else [conf.get("share", "")]
        if not isinstance(names, list) or not names:
            raise ValueError("至少配置一个 SMB 共享名称")
        shares = {}
        for name in names:
            if not isinstance(name, str):
                raise ValueError("SMB 共享名称必须为字符串")
            name = name.strip()
            if not name or name in (".", "..") or any(char in name for char in "/\\:\x00"):
                raise ValueError("SMB 共享名称不能包含路径分隔符或越界目录")
            if name.casefold() in shares:
                raise ValueError("SMB 共享名称不能重复")
            shares[name.casefold()] = name
        self._shares = shares
        server = f"\\\\{self._host}"
        self._server_path = server if self._multi_share else f"{server}\\{names[0].strip()}"

    def _test_connection(self):
        """
        测试SMB连接
        """
        import smbclient
        from smbprotocol.exceptions import SMBAuthenticationError, SMBException, SMBResponseException
        try:
            # 尝试列出根目录来测试连接
            for share in self._shares.values():
                smbclient.listdir(f"\\\\{self._host}\\{share}")
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

    def _check_connection(self) -> None:
        """
        检查SMB连接状态
        """
        if not self._connected or not self._server_path:
            raise SMBConnectionError("【SMB】连接未建立或已断开，请检查配置！")

    def _normalize_path(self, path: Union[str, Path], *, writable: bool = False) -> str:
        """映射存储路径到已配置共享，禁止越界以及修改虚拟根或共享根。"""
        path_str = str(path).replace("\\", "/")
        parts = [part for part in path_str.split("/") if part and part != "."]
        if ".." in parts or any(":" in part or "\x00" in part for part in parts):
            raise ValueError("SMB 路径不能越界或包含协议地址")
        if self._multi_share:
            if not parts:
                raise ValueError("SMB 虚拟根目录不能执行文件操作")
            share = self._shares.get(parts[0].casefold())
            if not share:
                raise ValueError(f"SMB 共享未配置：{parts[0]}")
            if writable and len(parts) == 1:
                raise ValueError("不能修改 SMB 共享根目录")
            parts[0] = share
        elif writable and not parts:
            raise ValueError("不能修改 SMB 共享根目录")
        if not self._server_path:
            raise SMBConnectionError("SMB 连接尚未初始化")
        return self._server_path + ("\\" + "\\".join(parts) if parts else "")

    def _relative_path(self, file_path: str) -> str:
        """去掉当前命名空间的 UNC 前缀，保留多共享模式的共享名称。"""
        prefix = f"{self._server_path}\\"
        if not file_path.casefold().startswith(prefix.casefold()):
            raise ValueError("文件不属于当前 SMB 存储")
        return "/" + file_path[len(prefix):].replace("\\", "/")

    def _create_fileitem(
        self, stat_result, file_path: str, name: str, *, strict: bool = False
    ) -> _SchemaFileItem:
        """
        创建文件项；严格查询不能把元数据读取失败回退成普通文件。
        """
        import smbclient
        try:
            # 检查是否为目录
            is_directory = smbclient.path.isdir(file_path)

            # 处理路径
            relative_path = self._relative_path(file_path)
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
                return _SchemaFileItem(
                    storage=self.schema.value,
                    type="dir",
                    path=relative_path,
                    name=name,
                    basename=name,
                    modify_time=modify_time,
                )
            else:
                return _SchemaFileItem(
                    storage=self.schema.value,
                    type="file",
                    path=relative_path,
                    name=name,
                    basename=Path(name).stem,
                    extension=Path(name).suffix[1:] if Path(name).suffix else None,
                    size=getattr(stat_result, "st_size", 0),
                    modify_time=modify_time,
                )
        except Exception as e:
            if strict:
                raise StorageQueryError(f"【SMB】创建文件项失败: {file_path} - {e}") from e
            logger.error(f"【SMB】创建文件项失败：{e}")
            # 返回基本的文件项信息
            return _SchemaFileItem(
                storage=self.schema.value,
                type="file",
                path=self._relative_path(file_path),
                name=name,
                basename=Path(name).stem,
                modify_time=int(time.time()),
            )

    def init_storage(self):
        """
        保存或重置配置后清理旧会话并重新连接，即使配置未变或上次连接失败。
        """
        from smbclient import reset_connection_cache
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

    def list_strict(self, fileitem: _SchemaFileItem) -> List[_SchemaFileItem]:
        """用于快照的完整目录查询，任一子项读取失败都拒绝返回部分结果。"""
        try:
            return self.list(fileitem, strict=True)
        except StorageQueryError:
            raise
        except Exception as err:
            raise StorageQueryError(f"【SMB】查询目录失败: {fileitem.path} - {err}") from err

    def list(self, fileitem: _SchemaFileItem, *, strict: bool = False) -> List[_SchemaFileItem]:
        """
        浏览文件；strict 供快照查询传播异常，普通浏览沿用容错回退。
        """
        import smbclient
        from smbprotocol.exceptions import SMBException, SMBResponseException
        try:
            self._check_connection()

            if fileitem.type == "file":
                if not fileitem.path:
                    raise StorageQueryError("【SMB】文件查询缺少路径")
                item = self.get_item_strict(Path(fileitem.path)) if strict else self.detail(fileitem)
                if item:
                    return [item]
                return []

            if self._multi_share and not fileitem.path.rstrip("/\\"):
                return [
                    _SchemaFileItem(storage=self.schema.value, type="dir", path=f"/{share}/",
                                    name=share, basename=share)
                    for share in self._shares.values()
                ]

            # 构建SMB路径
            smb_path = self._normalize_path(fileitem.path.rstrip("/"))

            # 列出目录内容
            try:
                entries = smbclient.listdir(smb_path)
            except (SMBResponseException, SMBException) as e:
                logger.error(f"【SMB】列出目录失败: {smb_path} - {e}")
                if strict:
                    raise
                return []

            items = []
            for entry in entries:
                if entry in [".", ".."]:
                    continue

                entry_path = f"{smb_path}\\{entry}"
                try:
                    stat_result = smbclient.stat(entry_path)
                    item = self._create_fileitem(stat_result, entry_path, entry, strict=strict)
                    items.append(item)
                except Exception as e:
                    if strict:
                        raise
                    logger.debug(f"【SMB】获取文件信息失败: {entry_path} - {e}")
                    continue

            return items
        except Exception as e:
            if strict:
                raise
            logger.error(f"【SMB】列出文件失败: {e}")
            return []

    def create_folder(
        self, fileitem: _SchemaFileItem, name: str
    ) -> Optional[_SchemaFileItem]:
        """
        创建目录
        """
        import smbclient
        try:
            self._check_connection()

            new_path = self._normalize_path(Path(fileitem.path) / name, writable=True)

            # 创建目录
            smbclient.mkdir(new_path)

            # 返回创建的目录信息
            return _SchemaFileItem(
                storage=self.schema.value,
                type="dir",
                path=f"{fileitem.path.rstrip('/')}/{name}/",
                name=name,
                basename=name,
                modify_time=int(time.time()),
            )
        except Exception as e:
            logger.error(f"【SMB】创建目录失败: {e}")
            return None

    def get_folder(self, path: Path) -> Optional[_SchemaFileItem]:
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

    def get_item(self, path: Path) -> Optional[_SchemaFileItem]:
        """
        获取文件或目录，不存在返回None
        """
        import smbclient
        try:
            self._check_connection()

            # 处理根目录
            if str(path) == "/":
                return _SchemaFileItem(
                    storage=self.schema.value,
                    type="dir",
                    path="/",
                    name="",
                    basename="",
                    modify_time=int(time.time()),
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

    def get_item_strict(self, path: Path) -> Optional[_SchemaFileItem]:
        """
        获取文件或目录，确认不存在返回None；无法确认状态时抛出 StorageQueryError。
        只有 ENOENT/ENOTDIR 才是「确认不存在」，连接中断、认证失败等都无法确认
        目标状态，必须保守失败以免覆盖保护被绕过。
        """
        import smbclient
        try:
            self._check_connection()

            # 处理根目录
            if str(path) == "/":
                return _SchemaFileItem(
                    storage=self.schema.value,
                    type="dir",
                    path="/",
                    name="",
                    basename="",
                    modify_time=int(time.time()),
                )

            smb_path = self._normalize_path(str(path).rstrip("/"))
            try:
                stat_result = smbclient.stat(smb_path)
            except OSError as err:
                if err.errno in (errno.ENOENT, errno.ENOTDIR):
                    return None
                raise StorageQueryError(f"【SMB】查询文件项失败: {path} - {err}") from err
            return self._create_fileitem(stat_result, smb_path, Path(path).name, strict=True)
        except StorageQueryError:
            raise
        except Exception as e:
            raise StorageQueryError(f"【SMB】查询文件项失败: {path} - {e}") from e

    def detail(self, fileitem: _SchemaFileItem) -> Optional[_SchemaFileItem]:
        """
        获取文件详情
        """
        return self.get_item(Path(fileitem.path))

    def delete(self, fileitem: _SchemaFileItem) -> bool:
        """
        删除文件或目录
        """
        import smbclient
        from smbprotocol.exceptions import SMBException, SMBResponseException
        try:
            self._check_connection()

            smb_path = self._normalize_path(fileitem.path.rstrip("/"), writable=True)
            logger.info(f"【SMB】开始删除: {fileitem.path} (类型: {fileitem.type})")

            # 先检查路径是否存在
            if not smbclient.path.exists(smb_path):
                logger.warn(f"【SMB】路径不存在，跳过删除: {fileitem.path}")
                return True

            if fileitem.type == "dir":
                # 递归删除目录及其内容
                logger.debug(f"【SMB】递归删除目录: {smb_path}")
                self._recursive_delete(smb_path)
            else:
                # 删除文件
                logger.debug(f"【SMB】删除文件: {smb_path}")
                smbclient.remove(smb_path)

            logger.info(f"【SMB】删除成功: {fileitem.path}")
            return True
        except SMBConnectionError as e:
            logger.error(f"【SMB】删除失败 - 连接错误: {fileitem.path} - {e}")
            return False
        except SMBResponseException as e:
            logger.error(f"【SMB】删除失败 - SMB响应错误: {fileitem.path} - {e}")
            return False
        except SMBException as e:
            logger.error(f"【SMB】删除失败 - SMB错误: {fileitem.path} - {e}")
            return False
        except Exception as e:
            logger.error(f"【SMB】删除失败 - 未知错误: {fileitem.path} - {e}")
            return False

    def _recursive_delete(self, smb_path: str):
        """
        递归删除目录及其所有内容
        """
        import smbclient
        from smbprotocol.exceptions import SMBException, SMBResponseException
        try:
            # 检查路径是否存在
            if not smbclient.path.exists(smb_path):
                logger.debug(f"【SMB】路径不存在，跳过删除: {smb_path}")
                return

            # 如果是文件，直接删除
            if smbclient.path.isfile(smb_path):
                logger.debug(f"【SMB】删除文件: {smb_path}")
                smbclient.remove(smb_path)
                return

            # 如果是目录，先删除其内容
            if smbclient.path.isdir(smb_path):
                logger.debug(f"【SMB】开始删除目录内容: {smb_path}")
                try:
                    # 列出目录内容
                    entries = smbclient.listdir(smb_path)
                    logger.debug(f"【SMB】目录 {smb_path} 包含 {len(entries)} 个项目")

                    for entry in entries:
                        if entry in [".", ".."]:
                            continue
                        entry_path = f"{smb_path}\\{entry}"
                        logger.debug(f"【SMB】递归删除子项: {entry_path}")
                        # 递归删除子项
                        self._recursive_delete(entry_path)

                    # 删除空目录
                    logger.debug(f"【SMB】删除空目录: {smb_path}")
                    smbclient.rmdir(smb_path)
                    logger.debug(f"【SMB】目录删除成功: {smb_path}")

                except SMBResponseException as e:
                    # 如果目录不为空，尝试强制删除
                    logger.warn(f"【SMB】目录不为空，尝试强制删除: {smb_path} - {e}")
                    # 使用remove方法尝试删除（某些SMB服务器支持）
                    try:
                        smbclient.remove(smb_path)
                        logger.info(f"【SMB】强制删除目录成功: {smb_path}")
                    except Exception as remove_error:
                        # 如果还是失败，记录错误并抛出异常
                        logger.error(
                            f"【SMB】无法删除非空目录: {smb_path} - {remove_error}"
                        )
                        raise SMBConnectionError(
                            f"无法删除非空目录 {smb_path}: {remove_error}"
                        )
                except SMBException as e:
                    logger.error(f"【SMB】SMB操作失败: {smb_path} - {e}")
                    raise SMBConnectionError(f"SMB操作失败 {smb_path}: {e}")

        except SMBConnectionError:
            # 重新抛出SMB连接错误
            raise
        except Exception as e:
            logger.error(f"【SMB】递归删除失败: {smb_path} - {e}")
            raise SMBConnectionError(f"递归删除失败 {smb_path}: {e}")

    def rename(self, fileitem: _SchemaFileItem, name: str) -> bool:
        """
        重命名文件
        """
        import smbclient
        try:
            self._check_connection()

            old_path = self._normalize_path(fileitem.path.rstrip("/"), writable=True)
            parent_path = Path(fileitem.path).parent
            new_path = self._normalize_path(parent_path / name, writable=True)

            # 重命名
            smbclient.rename(old_path, new_path)

            logger.info(f"【SMB】重命名成功: {fileitem.path} -> {name}")
            return True
        except Exception as e:
            logger.error(f"【SMB】重命名失败: {e}")
            return False

    def download(self, fileitem: _SchemaFileItem, path: Path = None) -> Optional[Path]:
        """
        带实时进度显示的下载
        """
        import smbclient
        local_path = self._build_download_path(fileitem, path or get_runtime_setting('TEMP_PATH'))
        if not local_path:
            return None
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
                        if runtime_stop_state.consume_transfer_stop(fileitem.path):
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

    def upload(
        self, fileitem: _SchemaFileItem, path: Path, new_name: Optional[str] = None
    ) -> Optional[_SchemaFileItem]:
        """
        带实时进度显示的上传
        """
        import smbclient
        target_name = new_name or path.name
        target_path = Path(fileitem.path) / target_name
        try:
            self._check_connection()
            smb_path = self._normalize_path(target_path, writable=True)

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
                        if runtime_stop_state.consume_transfer_stop(path.as_posix()):
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

    def copy(self, fileitem: _SchemaFileItem, path: Path, new_name: str) -> bool:
        """在同一服务的已配置共享间执行服务端复制，失败时不回退到本地下载和上传。

        使用 SMB CopyChunk；不能使用会自动回退客户端流式复制的 shutil 接口。
        失败后的目标状态由整理步骤恢复机制核验，避免擅自删除已产生的结果。
        """
        import smbclient
        try:
            self._check_connection()
            if not fileitem.path:
                raise ValueError("源文件路径不能为空")
            src_path = self._normalize_path(fileitem.path, writable=True)
            dst_path = self._normalize_path(path / new_name, writable=True)
            try:
                if smbclient.path.samefile(src_path, dst_path):
                    raise ValueError("源文件和目标是同一文件，不能执行服务端复制")
            except OSError as err:
                if err.errno != errno.ENOENT:
                    raise
            smbclient.copyfile(src_path, dst_path)
            logger.info(f"【SMB】服务端复制成功: {src_path} -> {dst_path}")
            return True
        except Exception as e:
            logger.error(f"【SMB】服务端复制失败，不进行本地中转: {e}")
            return False

    def move(self, fileitem: _SchemaFileItem, path: Path, new_name: str) -> bool:
        """共享内重命名；跨共享先完成服务端复制，再删除源文件。

        跨共享移动不是原子操作，复制失败不删除源文件，删除失败保留两份并报错。
        不确定结果交由持久步骤恢复，不执行客户端中转或无条件覆盖已有目标。
        """
        import smbclient
        try:
            self._check_connection()
            if not fileitem.path:
                raise ValueError("源文件路径不能为空")
            src_path = self._normalize_path(fileitem.path, writable=True)
            dst_path = self._normalize_path(path / new_name, writable=True)
            if ntpath.splitdrive(src_path)[0].casefold() != ntpath.splitdrive(dst_path)[0].casefold():
                self._move_between_shares(fileitem, path, new_name, src_path, dst_path)
            else:
                smbclient.rename(src_path, dst_path)
            logger.info(f"【SMB】服务端移动成功: {src_path} -> {dst_path}")
            return True
        except Exception as e:
            logger.error(f"【SMB】服务端移动失败，不进行本地中转: {e}")
            return False

    def _move_between_shares(
            self, fileitem: _SchemaFileItem, path: Path, new_name: str, src_path: str, dst_path: str,
    ) -> None:
        """跨共享移动仅接受新目标，CopyChunk 完整成功后才允许删除源文件。"""
        import smbclient
        try:
            smbclient.stat(dst_path)
        except OSError as err:
            if err.errno != errno.ENOENT:
                raise
        else:
            raise FileExistsError("跨共享移动的目标已存在，请先由整理覆盖策略处理")
        if not self.copy(fileitem, path, new_name):
            raise OSError("跨共享服务端复制失败，保留源文件")
        smbclient.remove(src_path)

    def link(self, fileitem: _SchemaFileItem, target_file: Path) -> bool:
        """
        在当前共享内创建服务端硬链接，要求同一文件系统且服务器支持硬链接。
        """
        import smbclient
        from smbprotocol.exceptions import SMBResponseException
        try:
            self._check_connection()
            src_path = self._normalize_path(fileitem.path, writable=True)
            dst_path = self._normalize_path(target_file, writable=True)

            if ntpath.splitdrive(src_path)[0].casefold() != ntpath.splitdrive(dst_path)[0].casefold():
                raise ValueError("SMB 不支持跨共享硬链接，请改用复制或移动，或使用包含两者的共同共享")

            # 检查源文件是否存在
            if not smbclient.path.exists(src_path):
                raise FileNotFoundError(f"源文件不存在: {src_path}")

            # 确保目标路径的父目录存在
            dst_parent = "\\".join(dst_path.rsplit("\\", 1)[:-1])
            if dst_parent and not smbclient.path.exists(dst_parent):
                logger.info(f"【SMB】创建目标目录: {dst_parent}")
                smbclient.makedirs(dst_parent, exist_ok=True)

            # 尝试创建硬链接
            smbclient.link(src_path, dst_path)
            logger.info(f"【SMB】硬链接创建成功: {src_path} -> {dst_path}")
            return True

        except SMBResponseException as e:
            # SMB协议错误，可能不支持硬链接
            logger.error(f"【SMB】创建硬链接失败(当前Samba服务器可能不支持硬链接): {e}")
            return False
        except Exception as e:
            logger.error(f"【SMB】创建硬链接失败: {e}")
            return False

    def softlink(self, fileitem: _SchemaFileItem, target_file: Path) -> bool:
        """当前 SMB 存储未提供软链接整理能力。"""
        pass

    def usage(self) -> Optional[_SchemaStorageUsage]:
        """返回首个共享报告的卷容量和当前账号可用空间，不累计可能重叠的共享。"""
        import smbclient
        try:
            self._check_connection()
            # 多个共享可能共用存储池；SMB 的卷序列号也可能按共享生成，不能据此累加。
            # 沿用单卷用量合同，以配置首项作为容量查询入口，保持卡片和仪表板可用。
            share = next(iter(self._shares.values()))
            volume_stat = smbclient.stat_volume(f"\\\\{self._host}\\{share}")
            return _SchemaStorageUsage(
                total=volume_stat.total_size,
                available=volume_stat.caller_available_size,
            )

        except Exception as e:
            logger.error(f"【SMB】获取存储使用情况失败: {e}")
            return None

    def __del__(self):
        """
        析构函数，清理连接
        """
        try:
            if self._connected:
                from smbclient import reset_connection_cache

                reset_connection_cache()
        except Exception as e:
            logger.debug(f"【SMB】清理连接失败: {e}")
