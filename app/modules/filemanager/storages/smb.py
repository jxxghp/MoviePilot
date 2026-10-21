"""
SMB 单共享与多共享存储的路径映射、严格查询和服务端文件操作。
"""

import errno
import ntpath
import threading
import time
from pathlib import Path
from typing import Any, List, Optional, Union

from app.foundation.singleton import WeakSingleton
from app.modules.filemanager.storages import StorageBase, transfer_process
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.runtime.stop import runtime_stop_state
from app.schemas.exception import StorageQueryError
from app.schemas.file import FileItem as _SchemaFileItem
from app.schemas.file import StorageUsage as _SchemaStorageUsage
from app.schemas.types import StorageSchema

# SMB SDK（smbclient/smbprotocol/spnego）约 12MB，存储发现会导入全部存储实现，
# 因此各方法内按需导入，未配置 SMB 时不加载

lock = threading.Lock()


class SMBConnectionError(Exception):
    """
    SMB 连接错误。
    """

    pass


class SMB(StorageBase, metaclass=WeakSingleton):
    """
    通过 smbclient 操作已配置的 SMB 共享及其文件。

    单共享模式使用共享内路径；多共享模式在虚拟根下暴露各共享名称。
    客户端采用弱单例，配置初始化和析构会影响 smbclient 的进程级连接缓存。
    """

    # 存储类型
    schema = StorageSchema.SMB

    # 支持的整理方式
    transtype = {
        "move": "移动",
        "copy": "复制",
        "link": "硬链接",
    }

    # 文件读写块大小，默认 10 MiB
    chunk_size = 10 * 1024 * 1024

    @property
    def snapshot_check_folder_modtime(self) -> bool:
        """
        禁用目录修改时间剪枝，避免漏掉后代目录变化。

        :return: 固定为 False，快照遍历到配置深度上限
        """
        return False

    @property
    def snapshot_strict_query(self) -> bool:
        """
        要求快照使用严格查询，避免故障被记作空目录。

        :return: 固定为 True
        """
        return True

    def __init__(self) -> None:
        """
        加载当前 SMB 配置并尝试建立连接。
        """
        super().__init__()
        self._connected = False
        self._server_path: Optional[str] = None
        self._shares: dict[str, str] = {}
        self._multi_share = False
        self._host = None
        self._username = None
        self._password = None

        self._init_connection()

    def _init_connection(self) -> None:
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

            ClientConfig(
                username=self._username,
                password=self._password,
                domain=domain if domain else None,
                connection_timeout=60,
                port=port,
                # 使用协商认证，并兼容需要关闭安全协商要求的匿名访问。
                auth_protocol="negotiate",
                require_secure_negotiate=False,
            )

            register_session(
                self._host,
                username=self._username,
                password=self._password,
                port=port,
                # 当前会话不要求 SMB 消息加密。
                encrypt=False,
                connection_timeout=60,
            )

            self._test_connection()

            self._connected = True
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
        """
        显式 shares 配置启用共享根命名空间，旧 share 保持共享内路径。
        """
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

    def _test_connection(self) -> None:
        """
        测试SMB连接。
        """
        import smbclient
        from smbprotocol.exceptions import SMBAuthenticationError, SMBException, SMBResponseException
        try:
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
        检查是否为匿名访问。
        """
        return not self._username and not self._password

    def _check_connection(self) -> None:
        """
        检查SMB连接状态。
        """
        if not self._connected or not self._server_path:
            raise SMBConnectionError("【SMB】连接未建立或已断开，请检查配置！")

    def _normalize_path(self, path: Union[str, Path], *, writable: bool = False) -> str:
        """
        映射存储路径到已配置共享，禁止越界以及修改虚拟根或共享根。
        """
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
        """
        去掉当前命名空间的 UNC 前缀，保留多共享模式的共享名称。
        """
        prefix = f"{self._server_path}\\"
        if not file_path.casefold().startswith(prefix.casefold()):
            raise ValueError("文件不属于当前 SMB 存储")
        return "/" + file_path[len(prefix):].replace("\\", "/")

    def _create_fileitem(
        self, stat_result: Any, file_path: str, name: str, *, strict: bool = False
    ) -> _SchemaFileItem:
        """
        创建文件项；严格查询不能把元数据读取失败回退成普通文件。
        """
        import smbclient
        try:
            is_directory = smbclient.path.isdir(file_path)

            relative_path = self._relative_path(file_path)
            if not relative_path.startswith("/"):
                relative_path = "/" + relative_path

            if is_directory and not relative_path.endswith("/"):
                relative_path += "/"

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
            return _SchemaFileItem(
                storage=self.schema.value,
                type="file",
                path=self._relative_path(file_path),
                name=name,
                basename=Path(name).stem,
                modify_time=int(time.time()),
            )

    def init_storage(self) -> None:
        """
        保存或重置配置后清理旧会话并重新连接，即使配置未变或上次连接失败。
        """
        from smbclient import reset_connection_cache
        reset_connection_cache()
        self._init_connection()

    def check(self) -> bool:
        """
        列举各已配置共享以确认连接可用。

        :return: 已连接且全部共享探测成功返回 True；失败时返回 False 并清除连接标记
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
        """
        完整列举目录，任一子项读取失败都拒绝返回部分结果。

        :param fileitem: 目标目录项；文件项会执行严格详情查询
        :return: 完整直接子项列表；文件不存在时为空列表
        :raises StorageQueryError: 连接、目录或任一子项查询失败
        """
        try:
            return self.list(fileitem, strict=True)
        except StorageQueryError:
            raise
        except Exception as err:
            raise StorageQueryError(f"【SMB】查询目录失败: {fileitem.path} - {err}") from err

    def list(self, fileitem: _SchemaFileItem, *, strict: bool = False) -> List[_SchemaFileItem]:
        """
        浏览共享中的文件或目录，支持严格与普通容错模式。

        :param fileitem: 待浏览项；多共享模式的根目录返回已配置共享列表
        :param strict: 是否传播查询错误；默认 False，普通浏览会跳过失败子项
        :return: 文件自身或直接子项列表；普通模式失败可返回部分结果或空列表
        """
        import smbclient
        from smbprotocol.exceptions import SMBException, SMBResponseException
        try:
            self._check_connection()

            file_path = fileitem.path
            if not file_path:
                raise StorageQueryError("【SMB】文件查询缺少路径")

            if fileitem.type == "file":
                item = self.get_item_strict(Path(file_path)) if strict else self.detail(fileitem)
                if item:
                    return [item]
                return []

            if self._multi_share and not file_path.rstrip("/\\"):
                return [
                    _SchemaFileItem(storage=self.schema.value, type="dir", path=f"/{share}/",
                                    name=share, basename=share)
                    for share in self._shares.values()
                ]

            smb_path = self._normalize_path(file_path.rstrip("/"))

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
        创建共享内子目录，不允许修改虚拟根或共享根。

        :param fileitem: 已有父目录项
        :param name: 新子目录名称
        :return: 创建后的目录项；路径、连接或创建失败返回 None
        """
        import smbclient
        try:
            self._check_connection()
            file_path = fileitem.path
            if not file_path:
                raise ValueError("父目录路径不能为空")

            new_path = self._normalize_path(Path(file_path) / name, writable=True)

            smbclient.mkdir(new_path)

            return _SchemaFileItem(
                storage=self.schema.value,
                type="dir",
                path=f"{file_path.rstrip('/')}/{name}/",
                name=name,
                basename=name,
                modify_time=int(time.time()),
            )
        except Exception as e:
            logger.error(f"【SMB】创建目录失败: {e}")
            return None

    def get_folder(self, path: Path) -> Optional[_SchemaFileItem]:
        """
        取得共享内目录，必要时逐级创建缺失目录。

        :param path: 以根目录开头的存储路径，多共享模式包含共享名
        :return: 目标目录项；父项查询或创建失败返回 None
        """
        folder = self.get_item(path)
        if folder:
            return folder

        parts = path.parts
        current_path = Path("/")

        for part in parts[1:]:
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
        查询存储路径，根路径返回虚拟目录信息。

        :param path: 存储内路径，多共享模式包含共享名
        :return: 文件或目录项；不存在、路径不合法或查询失败返回 None
        """
        import smbclient
        try:
            self._check_connection()

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
        严格查询 SMB 路径，区分缺失和连接或认证故障。

        :param path: 需要确认状态的存储内路径
        :return: 文件或目录项；只有 ENOENT 或 ENOTDIR 表示确认缺失并返回 None
        :raises StorageQueryError: 连接、路径或元数据状态无法确认
        """
        import smbclient
        try:
            self._check_connection()

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
        按存储项路径重新查询元数据。

        :param fileitem: 待查询的文件或目录项
        :return: 最新存储项；不存在或普通查询失败返回 None
        """
        return self.get_item(Path(fileitem.path)) if fileitem.path else None

    def delete(self, fileitem: _SchemaFileItem) -> bool:
        """
        删除文件或递归删除目录，保护虚拟根和共享根。

        :param fileitem: 待删除的存储项；type 决定文件或目录删除方式
        :return: 删除完成或普通存在性检查显示路径缺失返回 True；其他失败返回 False
        """
        import smbclient
        from smbprotocol.exceptions import SMBException, SMBResponseException
        try:
            self._check_connection()
            file_path = fileitem.path
            if not file_path:
                raise ValueError("文件路径不能为空")

            smb_path = self._normalize_path(file_path.rstrip("/"), writable=True)
            logger.info(f"【SMB】开始删除: {file_path} (类型: {fileitem.type})")

            if not smbclient.path.exists(smb_path):
                logger.warn(f"【SMB】路径不存在，跳过删除: {file_path}")
                return True

            if fileitem.type == "dir":
                logger.debug(f"【SMB】递归删除目录: {smb_path}")
                self._recursive_delete(smb_path)
            else:
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

    def _recursive_delete(self, smb_path: str) -> None:
        """
        递归删除目录及其所有内容。
        """
        import smbclient
        from smbprotocol.exceptions import SMBException, SMBResponseException
        try:
            if not smbclient.path.exists(smb_path):
                logger.debug(f"【SMB】路径不存在，跳过删除: {smb_path}")
                return

            if smbclient.path.isfile(smb_path):
                logger.debug(f"【SMB】删除文件: {smb_path}")
                smbclient.remove(smb_path)
                return

            if smbclient.path.isdir(smb_path):
                logger.debug(f"【SMB】开始删除目录内容: {smb_path}")
                try:
                    entries = smbclient.listdir(smb_path)
                    logger.debug(f"【SMB】目录 {smb_path} 包含 {len(entries)} 个项目")

                    for entry in entries:
                        if entry in [".", ".."]:
                            continue
                        entry_path = f"{smb_path}\\{entry}"
                        logger.debug(f"【SMB】递归删除子项: {entry_path}")
                        self._recursive_delete(entry_path)

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
            raise
        except Exception as e:
            logger.error(f"【SMB】递归删除失败: {smb_path} - {e}")
            raise SMBConnectionError(f"递归删除失败 {smb_path}: {e}")

    def rename(self, fileitem: _SchemaFileItem, name: str) -> bool:
        """
        在原父目录内重命名存储项，禁止修改共享根。

        :param fileitem: 源文件或目录项
        :param name: 目标名称；已有目标由服务器重命名语义决定
        :return: 操作完成返回 True，路径、连接或服务器失败返回 False
        """
        import smbclient
        try:
            self._check_connection()
            file_path = fileitem.path
            if not file_path:
                raise ValueError("文件路径不能为空")

            old_path = self._normalize_path(file_path.rstrip("/"), writable=True)
            parent_path = Path(file_path).parent
            new_path = self._normalize_path(parent_path / name, writable=True)

            smbclient.rename(old_path, new_path)

            logger.info(f"【SMB】重命名成功: {file_path} -> {name}")
            return True
        except Exception as e:
            logger.error(f"【SMB】重命名失败: {e}")
            return False

    def download(self, fileitem: _SchemaFileItem, path: Optional[Path] = None) -> Optional[Path]:
        """
        分块读取 SMB 文件到本地路径并报告进度。

        取消时可能保留部分本地文件；捕获传输异常时尝试删除残留。路径规范化错误可直接传播。

        :param fileitem: 源文件项，size 用于进度计算
        :param path: 本地保存目录；为空时使用 TEMP_PATH，缺失目录会创建
        :return: 下载完成的本地路径；取消或传输失败返回 None
        """
        import smbclient
        file_path = fileitem.path
        if not file_path:
            return None
        temp_path = path or get_runtime_setting('TEMP_PATH')
        if not isinstance(temp_path, Path):
            temp_path = Path(str(temp_path or "."))
        local_path = self._build_download_path(fileitem, temp_path)
        if not local_path:
            return None
        smb_path = self._normalize_path(file_path)
        try:
            self._check_connection()

            local_path.parent.mkdir(parents=True, exist_ok=True)

            file_size = fileitem.size

            logger.info(f"【SMB】开始下载: {fileitem.name} -> {local_path}")
            progress_callback = transfer_process(Path(file_path).as_posix())

            with smbclient.open_file(smb_path, mode="rb") as src_file:
                with open(local_path, "wb") as dst_file:
                    downloaded_size = 0
                    while True:
                        if runtime_stop_state.consume_transfer_stop(file_path):
                            logger.info(f"【SMB】{file_path} 下载已取消！")
                            return None
                        chunk = src_file.read(self.chunk_size)
                        if not chunk:
                            break
                        dst_file.write(chunk)
                        downloaded_size += len(chunk)
                        if file_size:
                            progress = (downloaded_size * 100) / file_size
                            progress_callback(progress)

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
        分块写入远端文件并报告进度，保留本地源文件。

        :param fileitem: 远端已有目标目录
        :param path: 本地源文件路径
        :param new_name: 可选目标文件名；为空时使用本地文件名
        :return: 上传后查询到的文件项；取消、传输或查询失败返回 None，远端可能保留部分内容
        """
        import smbclient
        if not fileitem.path:
            return None
        target_name = new_name or path.name
        target_path = Path(fileitem.path) / target_name
        try:
            self._check_connection()
            smb_path = self._normalize_path(target_path, writable=True)

            file_size = path.stat().st_size

            logger.info(f"【SMB】开始上传: {path} -> {target_path}")
            progress_callback = transfer_process(path.as_posix())

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
                        if file_size:
                            progress = (uploaded_size * 100) / file_size
                            progress_callback(progress)

            progress_callback(100)
            logger.info(f"【SMB】上传完成: {target_name}")

            return self.get_item(target_path)

        except Exception as e:
            logger.error(f"【SMB】上传失败: {target_name} - {e}")
            return None

    def copy(self, fileitem: _SchemaFileItem, path: Path, new_name: str, **_kwargs: Any) -> bool:
        """
        在同一服务器的已配置共享间执行 CopyChunk 服务端复制。

        不回退到本地下载上传；失败后的目标状态由整理恢复流程核验。

        :param fileitem: 源文件项，不得与目标表示同一文件
        :param path: 目标共享中的目录路径
        :param new_name: 目标文件名
        :return: 复制完成返回 True，失败返回 False 且保留已产生的目标内容
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
        """
        共享内直接改名，跨共享先服务端复制再删除源文件。

        跨共享只接受不存在的新目标，移动不具备整体原子性；删除源失败时保留两份。

        :param fileitem: 源文件项
        :param path: 已配置共享中的目标目录
        :param new_name: 目标文件名
        :return: 全部步骤完成返回 True；复制或源文件删除失败返回 False
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
        """
        跨共享移动仅接受新目标，CopyChunk 完整成功后才允许删除源文件。
        """
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
        在同一共享内创建服务端硬链接并按需创建父目录。

        :param fileitem: 源文件项，需存在且与目标在同一共享
        :param target_file: 完整目标链接路径
        :return: 创建成功返回 True；跨共享、路径或服务器不支持等失败返回 False
        """
        import smbclient
        from smbprotocol.exceptions import SMBResponseException
        try:
            self._check_connection()
            if not fileitem.path:
                raise ValueError("源文件路径不能为空")
            src_path = self._normalize_path(fileitem.path, writable=True)
            dst_path = self._normalize_path(target_file, writable=True)

            if ntpath.splitdrive(src_path)[0].casefold() != ntpath.splitdrive(dst_path)[0].casefold():
                raise ValueError("SMB 不支持跨共享硬链接，请改用复制或移动，或使用包含两者的共同共享")

            if not smbclient.path.exists(src_path):
                raise FileNotFoundError(f"源文件不存在: {src_path}")

            dst_parent = "\\".join(dst_path.rsplit("\\", 1)[:-1])
            if dst_parent and not smbclient.path.exists(dst_parent):
                logger.info(f"【SMB】创建目标目录: {dst_parent}")
                smbclient.makedirs(dst_parent, exist_ok=True)

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
        """
        保留符号链接接口；当前 SMB 存储不实现此操作。

        :param fileitem: 兼容接口的源文件项，当前忽略
        :param target_file: 兼容接口的目标路径，当前忽略
        :return: 固定返回 None，不创建链接
        """
        return False

    def usage(self) -> Optional[_SchemaStorageUsage]:
        """
        通过配置中的首个共享查询卷容量，避免累加重叠共享。

        :return: 以字节为单位的卷总容量和当前账号可用容量；连接或查询失败返回 None
        """
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

    def __del__(self) -> None:
        """
        已连接实例回收时尽力清空 smbclient 的进程级连接缓存。
        """
        try:
            if self._connected:
                from smbclient import reset_connection_cache

                reset_connection_cache()
        except Exception as e:
            logger.debug(f"【SMB】清理连接失败: {e}")
