import os
import shutil
import time
from pathlib import Path
from typing import Optional, List

from app.schemas.file import StorageUsage as _SchemaStorageUsage
from app.schemas.workflow import FileItem as _SchemaFileItem
from app.runtime.config import global_vars, settings
from app.runtime.hostports.directories import directory_config_port
from app.runtime.log import logger
from app.adapters.system.fsproxy import fsproxy
from app.modules._base.storage import StorageBase, transfer_process
from app.schemas.exception import StorageQueryError
from app.schemas.types import StorageSchema
from app.adapters.system.host import SystemUtils


class LocalStorage(StorageBase):
    """
    本地文件操作
    """

    # 存储类型
    schema = StorageSchema.Local
    # 支持的整理方式
    transtype = {
        "copy": "复制",
        "move": "移动",
        "link": "硬链接",
        "softlink": "软链接"
    }

    # 文件块大小，默认10MB
    chunk_size = 10 * 1024 * 1024

    def init_storage(self):
        """
        初始化
        """
        pass

    def check(self) -> bool:
        """
        检查存储是否可用
        """
        return True

    def __get_fileitem(self, path: Path) -> _SchemaFileItem:
        """
        获取文件项
        """
        # 走代理读取：挂载挂死时这一步会在超时后抛 OSError，而不是永久悬挂线程。
        # 顺带只 stat 一次——原先 size 与 modify_time 各 stat 一次，在网络挂载上
        # 等于把这个热点路径的开销翻倍
        info = fsproxy.stat(path)
        return _SchemaFileItem(
            storage=self.storage_token,
            type="file",
            path=path.as_posix(),
            name=path.name,
            basename=path.stem,
            extension=path.suffix[1:],
            size=info["size"],
            modify_time=info["mtime"],
        )

    def __get_diritem(self, path: Path) -> _SchemaFileItem:
        """
        获取目录项
        """
        return _SchemaFileItem(
            storage=self.storage_token,
            type="dir",
            path=path.as_posix() + "/",
            name=path.name,
            basename=path.stem,
            modify_time=fsproxy.stat(path)["mtime"],
        )

    def list(self, fileitem: _SchemaFileItem) -> List[_SchemaFileItem]:
        """
        浏览文件
        """
        # 返回结果
        ret_items = []
        path = fileitem.path
        if not fileitem.path or fileitem.path == "/":
            if SystemUtils.is_windows():
                partitions = SystemUtils.get_windows_drives() or ["C:/"]
                for partition in partitions:
                    ret_items.append(_SchemaFileItem(
                        storage=self.storage_token,
                        type="dir",
                        path=partition + "/",
                        name=partition,
                        basename=partition
                    ))
                return ret_items
            else:
                path = "/"
        else:
            if SystemUtils.is_windows():
                path = path.lstrip("/")
            elif not path.startswith("/"):
                path = "/" + path

        # 遍历目录
        path_obj = Path(path)
        try:
            info = fsproxy.stat(path_obj)
        except (FileNotFoundError, NotADirectoryError):
            logger.warn(f"【本地】目录不存在：{path}")
            return []

        # 如果是文件
        if info["is_file"]:
            ret_items.append(self.__get_fileitem(path_obj))
            return ret_items

        # 扁历所有目录
        for item in SystemUtils.list_sub_directory(path_obj):
            ret_items.append(self.__get_diritem(item))

        # 遍历所有文件，不含子目录
        for item in SystemUtils.list_sub_file(path_obj):
            ret_items.append(self.__get_fileitem(item))
        return ret_items

    def create_folder(self, fileitem: _SchemaFileItem, name: str) -> Optional[_SchemaFileItem]:
        """
        创建目录
        :param fileitem: 父目录
        :param name: 目录名
        """
        if not fileitem.path:
            return None
        path_obj = Path(fileitem.path) / name
        if not path_obj.exists():
            path_obj.mkdir(parents=True, exist_ok=True)
        return self.__get_diritem(path_obj)

    def get_folder(self, path: Path) -> Optional[_SchemaFileItem]:
        """
        获取目录
        """
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
        return self.__get_diritem(path)

    def get_item(self, path: Path) -> Optional[_SchemaFileItem]:
        """
        获取文件或目录，不存在返回None
        """
        try:
            info = fsproxy.stat(path)
        except (FileNotFoundError, NotADirectoryError):
            return None
        if info["is_file"]:
            return self.__get_fileitem(path)
        return self.__get_diritem(path)

    def get_item_strict(self, path: Path) -> Optional[_SchemaFileItem]:
        """
        获取文件或目录，无法确认状态时抛出 StorageQueryError。
        Path.exists() 会把部分 errno（如 EBADF/ELOOP）归入「不存在」，
        网络/FUSE 挂载抖动时会误判，这里用 stat 显式区分。
        挂载完全无响应时代理会超时并抛 FileSystemTimeout（OSError 子类），
        同样落入下面的分支，转化成调用方能处理的查询失败。
        """
        try:
            fsproxy.stat(path)
        except (FileNotFoundError, NotADirectoryError):
            return None
        except OSError as e:
            raise StorageQueryError(f"【本地】读取文件状态失败: {path} - {e}") from e
        try:
            return self.get_item(path)
        except OSError as e:
            raise StorageQueryError(f"【本地】读取文件信息失败: {path} - {e}") from e

    def detail(self, fileitem: _SchemaFileItem) -> Optional[_SchemaFileItem]:
        """
        获取文件详情
        """
        path_obj = Path(fileitem.path)
        if not path_obj.exists():
            return None
        return self.__get_fileitem(path_obj)

    def delete(self, fileitem: _SchemaFileItem) -> bool:
        """
        删除文件
        """
        if not fileitem.path:
            return False
        path_obj = Path(fileitem.path)
        try:
            info = fsproxy.stat(path_obj)
        except (FileNotFoundError, NotADirectoryError):
            return True
        except OSError as e:
            logger.error(f"【本地】读取待删除文件状态失败：{e}")
            return False
        try:
            if info["is_file"]:
                fsproxy.unlink(path_obj)
            else:
                fsproxy.rmtree(path_obj)
        except Exception as e:
            logger.error(f"【本地】删除文件失败：{e}")
            return False
        return True

    def rename(self, fileitem: _SchemaFileItem, name: str) -> bool:
        """
        重命名文件
        """
        path_obj = Path(fileitem.path)
        try:
            fsproxy.rename(path_obj, path_obj.parent / name)
        except (FileNotFoundError, NotADirectoryError):
            return False
        except Exception as e:
            logger.error(f"【本地】重命名文件失败：{e}")
            return False
        return True

    def download(self, fileitem: _SchemaFileItem, path: Path = None) -> Optional[Path]:
        """
        下载文件
        """
        return Path(fileitem.path)

    # 写入中的临时文件后缀。点开头（隐藏）+ 专用后缀双重保证：即使进程被
    # SIGKILL、临时文件残留，媒体库也不会把半成品当成媒体收录
    PARTIAL_SUFFIX = ".mp-partial"
    # 临时文件被认定为中断残留的时长（秒）。正常失败路径会自行清理，只有被
    # 强杀才会残留；阈值取得宽松，避免误删仍在写入的大文件
    PARTIAL_STALE_SECONDS = 24 * 3600

    @classmethod
    def _partial_path(cls, dest: Path) -> Path:
        """
        生成写入中的临时文件路径。

        必须与目标同目录：os.replace 只有在同一文件系统内才是原子的，放到
        /tmp 之类的地方会退化成一次完整拷贝，原子性荡然无存。带 PID 是为了
        避免多进程同时写同一目标时互相踩踏。
        :param dest: 目标文件路径
        :return: 临时文件路径
        """
        return dest.parent / f".{dest.name}.{os.getpid()}{cls.PARTIAL_SUFFIX}"

    @classmethod
    def _cleanup_stale_partials(cls, directory: Path):
        """
        清理目录下中断残留的临时文件。

        只做局部清理而不是全库扫描：在网络挂载上遍历整个媒体库代价不可接受，
        而残留只可能出现在曾经写入过的目录里，因此每次写入时顺带清理即可。
        本方法是尽力而为的旁路操作，任何失败都不影响主流程。
        :param directory: 目标目录
        """
        try:
            threshold = time.time() - cls.PARTIAL_STALE_SECONDS
            for item in directory.glob(f"*{cls.PARTIAL_SUFFIX}"):
                try:
                    if item.stat().st_mtime < threshold:
                        item.unlink()
                        logger.info(f"【本地】已清理中断残留的临时文件：{item}")
                except OSError:
                    continue
        except Exception as err:
            logger.debug(f"【本地】清理临时文件失败：{directory} - {err}")

    def _write_atomically(self, src: Path, dest: Path) -> bool:
        """
        以「写临时名 → os.replace」的方式把源文件内容落到目标。

        直接写目标路径的话，进程被杀（OOM、重启、宿主断电、SIGKILL）会在媒体库
        里留下一个**叫最终文件名的半截文件**：媒体库会把它扫进去，后续的
        「目标已存在」判断也会把它当成完成品。os.replace 在同目录内由内核保证
        原子性，因此目标要么完整存在，要么根本不存在。
        :param src: 源文件路径
        :param dest: 目标文件路径
        :return: 是否成功
        """
        self._cleanup_stale_partials(dest.parent)
        partial = self._partial_path(dest)
        # 进度只在需要展示时才回调 UI，但代理内部始终按固定间隔上报——那是判定
        # 「传输是否还在推进」的心跳，不能因为不展示进度就关掉
        progress_callback = (
            transfer_process(src.as_posix())
            if self.__should_show_progress(src, dest) else None
        )
        try:
            copied = fsproxy.copy(
                src, partial,
                progress_cb=progress_callback,
                cancel_cb=lambda: global_vars.is_transfer_stopped(src.as_posix()),
                chunk_size=self.chunk_size,
            )
            if not copied:
                logger.info(f"【本地】{src} 复制未完成")
                return False
            os.replace(partial, dest)
            return True
        except Exception as err:
            logger.error(f"【本地】复制文件失败：{err}")
            return False
        finally:
            if progress_callback:
                progress_callback(100)
            # 失败路径留下的临时文件就地清掉；成功时 replace 已经把它移走
            try:
                if partial.exists():
                    partial.unlink()
            except OSError:
                pass

    @staticmethod
    def _copy_with_target_permissions(src: Path, dest: Path) -> Path:
        """
        复制文件内容和时间戳，并保留目标目录赋予新文件的权限。

        目标目录的默认权限或继承 ACL 应作为媒体库的访问策略，复制完成后不能再用
        源文件权限覆盖，否则部分文件系统会清除已继承的 ACL。

        :param src: 源文件路径
        :param dest: 目标文件路径
        :return: 目标文件路径
        """
        src = Path(src)
        dest = Path(dest)
        src_stat = src.stat()
        shutil.copyfile(src, dest)
        os.utime(dest, ns=(src_stat.st_atime_ns, src_stat.st_mtime_ns))
        return dest

    def _copy_with_progress(self, src: Path, dest: Path) -> bool:
        """
        分块复制文件并回调进度
        """
        src_stat = src.stat()
        total_size = src_stat.st_size
        copied_size = 0
        progress_callback = transfer_process(src.as_posix())
        try:
            with open(src, "rb") as fsrc, open(dest, "wb") as fdst:
                while True:
                    if global_vars.is_transfer_stopped(src.as_posix()):
                        logger.info(f"【本地】{src} 复制已取消！")
                        return False
                    buf = fsrc.read(self.chunk_size)
                    if not buf:
                        break
                    fdst.write(buf)
                    copied_size += len(buf)
                    # 更新进度
                    if progress_callback:
                        percent = copied_size / total_size * 100
                        progress_callback(percent)
            os.utime(dest, ns=(src_stat.st_atime_ns, src_stat.st_mtime_ns))
            return True
        except Exception as e:
            logger.error(f"【本地】复制文件 {src} 失败：{e}")
            return False
        finally:
            progress_callback(100)

    def upload(
            self,
            fileitem: _SchemaFileItem,
            path: Path,
            new_name: Optional[str] = None
    ) -> Optional[_SchemaFileItem]:
        """
        上传文件（带进度）
        """
        try:
            dir_path = Path(fileitem.path)
            target_path = dir_path / (new_name or path.name)
            # 先原子地把内容落到目标，确认完整之后才删源
            if self._write_atomically(path, target_path):
                # 上传删除源文件
                path.unlink()
                return self.get_item(target_path)
        except Exception as err:
            logger.error(f"【本地】上传文件失败：{err}")
        return None

    @staticmethod
    def __should_show_progress(src: Path, dest: Path):
        """
        是否显示进度条
        """
        src_isnetwork = SystemUtils.is_network_filesystem(src)
        dest_isnetwork = SystemUtils.is_network_filesystem(dest)
        if src_isnetwork and dest_isnetwork and SystemUtils.is_same_disk(src, dest):
            return True
        return False

    def copy(
            self,
            fileitem: _SchemaFileItem,
            path: Path,
            new_name: str
    ) -> bool:
        """
        复制文件（带进度）
        """
        return self._write_atomically(Path(fileitem.path), path / new_name)

    def move(
            self,
            fileitem: _SchemaFileItem,
            path: Path,
            new_name: str
    ) -> bool:
        """
        移动文件（带进度）
        """
        src = Path(fileitem.path)
        dest = path / new_name
        if src == dest:
            # 目标和源文件相同，直接返回成功，不做任何操作
            return True
        try:
            # 同一文件系统内 rename 是原子操作：中断后要么完全成功、要么完全
            # 没发生，既不需要临时文件也不会留下半成品。直接尝试而不预先比较
            # st_dev，省掉挂载上的两次 stat——跨设备会以 EXDEV 失败并落到下面
            os.replace(src, dest)
            return True
        except OSError as err:
            logger.debug(f"【本地】直接移动未成功，降级为复制：{src} -> {dest} - {err}")
        # 跨文件系统：先原子地把内容落到目标，确认完整之后才删源。
        # 顺序不能反——先删源再失败就是永久丢件
        if not self._write_atomically(src, dest):
            return False
        try:
            src.unlink()
        except OSError as err:
            logger.warn(f"【本地】移动已完成但删除源文件失败：{src} - {err}")
        return True

    def link(self, fileitem: _SchemaFileItem, target_file: Path) -> bool:
        """
        硬链接文件
        """
        file_path = Path(fileitem.path)
        code, message = SystemUtils.link(file_path, target_file)
        if code != 0:
            logger.error(f"【本地】硬链接文件失败：{message}")
            return False
        return True

    def softlink(self, fileitem: _SchemaFileItem, target_file: Path) -> bool:
        """
        软链接文件
        """
        file_path = Path(fileitem.path)
        code, message = SystemUtils.softlink(file_path, target_file)
        if code != 0:
            logger.error(f"【本地】软链接文件失败：{message}")
            return False
        return True

    def usage(self) -> Optional[_SchemaStorageUsage]:
        """
        存储使用情况
        """
        directory_helper = directory_config_port.resolve()
        total_storage, free_storage = SystemUtils.space_usage(
            [Path(d.download_path) for d in directory_helper.get_local_download_dirs() if d.download_path] +
            [Path(d.library_path) for d in directory_helper.get_local_library_dirs() if d.library_path],
            btrfs_fsid_dedup=settings.BTRFS_FSID_DEDUP,
        )
        return _SchemaStorageUsage(
            total=total_storage,
            available=free_storage
        )
