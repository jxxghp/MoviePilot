"""
本地存储的文件查询、原子复制、移动、链接及容量统计。
"""

import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, List, Optional

from app.adapters.system.fsproxy import fsproxy
from app.adapters.system.host import SystemUtils
from app.application.directory import DirectoryHelper
from app.modules.filemanager.storages import StorageBase, transfer_process
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.runtime.stop import runtime_stop_state
from app.schemas.exception import StorageQueryError
from app.schemas.file import FileItem as _SchemaFileItem
from app.schemas.file import StorageUsage as _SchemaStorageUsage
from app.schemas.types import StorageSchema


class LocalStorage(StorageBase):
    """
    通过本地文件系统和受管文件代理实现存储操作。
    """

    # 存储类型
    schema = StorageSchema.Local
    # 支持的整理方式
    transtype = {
        "copy": "复制",
        "move": "移动",
        "link": "硬链接",
        "softlink": "软链接",
        "reflink": "引用链接",
    }

    # 文件复制块大小，默认 10 MiB
    chunk_size = 10 * 1024 * 1024

    def init_storage(self) -> None:
        """
        保留存储初始化钩子；本地文件系统不需要建立客户端。
        """
        pass

    def check(self) -> bool:
        """
        报告本地存储能力可用，不额外探测具体挂载路径。

        :return: 固定返回 True
        """
        return True

    def __get_fileitem(self, path: Path) -> _SchemaFileItem:
        """
        获取文件项，同时保留设备和 inode 身份用于恢复后的安全清理。
        """
        # 走代理读取：挂载挂死时这一步会在超时后抛 OSError，而不是永久悬挂线程。
        # 文件大小、修改时间和身份信息共用一次 stat，减少网络挂载查询。
        info = fsproxy.stat(path)
        return _SchemaFileItem(
            storage=self.schema.value,
            type="file",
            path=path.as_posix(),
            name=path.name,
            basename=path.stem,
            extension=path.suffix[1:],
            size=info["size"],
            modify_time=info["mtime"],
            modify_time_ns=info.get("mtime_ns"),
            change_time_ns=info.get("ctime_ns"),
            fileid=f"{info['dev']}:{info['ino']}" if info.get("ino") else None,
        )

    def __get_diritem(self, path: Path) -> _SchemaFileItem:
        """
        获取目录项。
        """
        return _SchemaFileItem(
            storage=self.schema.value,
            type="dir",
            path=path.as_posix() + "/",
            name=path.name,
            basename=path.stem,
            modify_time=fsproxy.stat(path)["mtime"],
        )

    def list(self, fileitem: _SchemaFileItem) -> List[_SchemaFileItem]:
        """
        列举直接子目录和文件，文件路径则返回该文件自身。

        :param fileitem: 待浏览项；空路径或根路径在 Windows 下返回磁盘列表
        :return: 目录项在前、文件项在后的列表；路径确认不存在时返回空列表，
                 列举过程中已消失的子项被跳过
        """
        ret_items = []
        path = fileitem.path
        if not path or path == "/":
            if SystemUtils.is_windows():
                partitions = SystemUtils.get_windows_drives() or ["C:/"]
                for partition in partitions:
                    ret_items.append(_SchemaFileItem(
                        storage=self.schema.value,
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

        path_obj = Path(path)
        try:
            info = fsproxy.stat(path_obj)
        except (FileNotFoundError, NotADirectoryError):
            logger.warn(f"【本地】目录不存在：{path}")
            return []

        if info["is_file"]:
            ret_items.append(self.__get_fileitem(path_obj))
            return ret_items

        for item in SystemUtils.list_sub_directory(path_obj):
            if sub_item := self.__stat_subitem(item):
                ret_items.append(sub_item)

        for item in SystemUtils.list_sub_file(path_obj):
            if sub_item := self.__stat_subitem(item, is_file=True):
                ret_items.append(sub_item)
        return ret_items

    def __stat_subitem(self, path: Path, is_file: bool = False) -> Optional[_SchemaFileItem]:
        """
        读取子项信息，条目在枚举后消失时返回 None 而不是中断整次列举。

        :param path: 子项路径
        :param is_file: 是否按文件项读取，否则按目录项读取
        :return: 文件项或目录项；条目已被移动、删除或成为悬空链接时为 None
        """
        try:
            return self.__get_fileitem(path) if is_file else self.__get_diritem(path)
        except (FileNotFoundError, NotADirectoryError):
            logger.debug(f"【本地】列举期间子项已不存在，跳过：{path}")
            return None

    def create_folder(self, fileitem: _SchemaFileItem, name: str) -> Optional[_SchemaFileItem]:
        """
        在父路径下创建目录及缺失的中间目录。

        :param fileitem: 父目录项，必须具有路径
        :param name: 需要创建的子目录名称
        :return: 目录项；父路径为空返回 None，文件系统错误向上传播
        """
        if not fileitem.path:
            return None
        path_obj = Path(fileitem.path) / name
        if not path_obj.exists():
            path_obj.mkdir(parents=True, exist_ok=True)
        return self.__get_diritem(path_obj)

    def get_folder(self, path: Path) -> Optional[_SchemaFileItem]:
        """
        取得目录项，路径不存在时创建全部中间目录。

        :param path: 本地目标目录路径
        :return: 目录信息；文件系统错误向上传播
        """
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
        return self.__get_diritem(path)

    def get_item(self, path: Path) -> Optional[_SchemaFileItem]:
        """
        通过文件代理查询本地文件或目录元数据。

        :param path: 本地路径
        :return: 文件或目录项；确认不存在时返回 None，其他查询错误向上传播
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
        通过 stat 区分路径不存在与文件系统故障。

        代理超时与其他 OSError 转为 StorageQueryError，避免把挂载故障当作可以覆盖的空目标。

        :param path: 需要确认存在性的本地路径
        :return: 文件或目录项；确认不存在时返回 None
        :raises StorageQueryError: 无法确认路径状态或读取元数据失败
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
        按本地路径重新读取文件元数据。

        :param fileitem: 目标文件项；调用方应传入普通文件
        :return: 最新文件信息；路径不存在时返回 None
        """
        if not fileitem.path:
            return None
        path_obj = Path(fileitem.path)
        if not path_obj.exists():
            return None
        return self.__get_fileitem(path_obj)

    def delete(self, fileitem: _SchemaFileItem) -> bool:
        """
        删除文件或递归删除目录。

        :param fileitem: 目标存储项；空路径不执行删除
        :return: 删除成功或路径已经不存在返回 True；空路径、查询或删除失败返回 False
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
        在原父目录中重命名存储项。

        :param fileitem: 需要改名的文件或目录
        :param name: 目标名称，目标已存在时遵循操作系统重命名语义
        :return: 操作完成返回 True，源项缺失或其他失败返回 False
        """
        if not fileitem.path:
            return False
        path_obj = Path(fileitem.path)
        try:
            fsproxy.rename(path_obj, path_obj.parent / name)
        except (FileNotFoundError, NotADirectoryError):
            return False
        except Exception as e:
            logger.error(f"【本地】重命名文件失败：{e}")
            return False
        return True

    def download(self, fileitem: _SchemaFileItem, path: Optional[Path] = None) -> Path:
        """
        直接提供本地源路径，不复制文件。

        :param fileitem: 本地源文件项
        :param path: 兼容远端存储接口的保存目录，当前忽略
        :return: 源文件 Path 对象，不在此方法检查是否存在
        """
        if not fileitem.path:
            raise ValueError("本地文件项缺少路径")
        return Path(fileitem.path)

    # 中间文件使用隐藏名称及专用后缀，降低媒体库把未完成内容当作成品的风险。
    PARTIAL_SUFFIX = ".mp-partial"
    # 中间文件按修改时间清理的阈值，单位为秒；正常失败路径会先尝试自行清理。
    PARTIAL_STALE_SECONDS = 24 * 3600

    @classmethod
    def _partial_path(cls, dest: Path) -> Path:
        """
        生成同目录、每次调用独立的中间写入路径。

        同目录便于完成后原子替换；随机标识隔离同进程和跨进程的同目标任务。

        :param dest: 最终文件路径
        :return: 隐藏的 .mp-partial 文件路径
        """
        return dest.parent / f".{dest.name}.{uuid.uuid4().hex}{cls.PARTIAL_SUFFIX}"

    @classmethod
    def _cleanup_stale_partials(cls, directory: Path) -> None:
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

    @staticmethod
    def _try_reflink_copy(src: Path, dest: Path) -> bool:
        """
        若源与目标位于同一磁盘且支持写时复制，优先尝试通过引用链接克隆。

        :param src: 源文件路径
        :param dest: 目标文件路径
        :return: 成功完成克隆并同步时间戳与权限策略返回 True，否则返回 False
        """
        if not SystemUtils.is_same_disk(src, dest):
            return False
        try:
            code, _ = SystemUtils.reflink(src, dest)
            if code != 0:
                logger.debug(f"【本地】尝试写时复制克隆返回非零状态码，回退常规复制：code={code}")
                if dest.exists():
                    try:
                        dest.unlink()
                    except OSError:
                        pass
                return False
            src_stat = src.stat()
            os.utime(dest, ns=(src_stat.st_atime_ns, src_stat.st_mtime_ns))
            sentinel = dest.parent / f".mode-probe-{uuid.uuid4().hex}"
            try:
                sentinel.write_bytes(b"")
                dest_mode = sentinel.stat().st_mode & 0o777
                dest.chmod(dest_mode)
            finally:
                sentinel.unlink(missing_ok=True)
            return True
        except Exception as err:
            logger.debug(f"【本地】尝试写时复制克隆失败，回退常规复制：{err}")
            if dest.exists():
                try:
                    dest.unlink()
                except OSError:
                    pass
            return False

    def _write_atomically(
        self,
        src: Path,
        dest: Path,
        *,
        try_reflink: Optional[bool] = None,
    ) -> bool:
        """
        先复制到同目录中间文件，完成后原子替换目标名称。

        目标已有内容在替换前保留；此处保证名称替换的原子性，不额外执行断电持久化同步。
        结束时尽力删除中间文件，进度展示会收尾到 100，不能仅据此判断复制成功。
        同一文件系统且支持写时复制时优先使用 reflink，失败或不支持时自动回退为常规复制。

        :param src: 源文件路径，成功后仍保留
        :param dest: 完整目标文件路径；父目录需已存在
        :param try_reflink: 是否优先尝试写时复制引用链接克隆；None 时遵循系统运行期配置
        :return: 复制和替换均完成返回 True；取消或失败返回 False
        """
        self._cleanup_stale_partials(dest.parent)
        partial = self._partial_path(dest)
        if runtime_stop_state.consume_transfer_stop(src.as_posix()):
            logger.info(f"【本地】{src} 复制已取消")
            return False

        should_try = (
            get_runtime_setting("TRANSFER_COPY_TRY_REFLINK", True)
            if try_reflink is None
            else try_reflink
        )
        # 同一文件系统优先尝试写时复制克隆，成功即完成原子替换
        if should_try and self._try_reflink_copy(src, partial):
            os.replace(partial, dest)
            return True
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
                cancel_cb=lambda: runtime_stop_state.consume_transfer_stop(src.as_posix()),
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
        直接分块写入目标并报告进度，支持消费传输停止信号。

        :param src: 源文件路径
        :param dest: 完整目标路径，写入时覆盖原内容
        :return: 复制完成并保留时间戳返回 True；取消或失败返回 False，可能留下部分目标内容
        """
        src_stat = src.stat()
        total_size = src_stat.st_size
        copied_size = 0
        progress_callback = transfer_process(src.as_posix())
        try:
            with open(src, "rb") as fsrc, open(dest, "wb") as fdst:
                while True:
                    if runtime_stop_state.consume_transfer_stop(src.as_posix()):
                        logger.info(f"【本地】{src} 复制已取消！")
                        return False
                    buf = fsrc.read(self.chunk_size)
                    if not buf:
                        break
                    fdst.write(buf)
                    copied_size += len(buf)
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
        原子复制本地上传文件到目标目录，并删除上传源。

        :param fileitem: 必须已经存在的目标目录项
        :param path: 上传源文件，复制成功后会删除
        :param new_name: 可选目标文件名，默认使用源文件名
        :return: 目标文件项；复制、删除源文件或后续查询失败返回 None，目标文件可能已经写入
        """
        try:
            if not fileitem.path:
                return None
            dir_path = Path(fileitem.path)
            target_path = dir_path / (new_name or path.name)
            # 先原子地把内容落到目标，确认完整之后才删源
            if self._write_atomically(path, target_path):
                path.unlink()
                return self.get_item(target_path)
        except Exception as err:
            logger.error(f"【本地】上传文件失败：{err}")
        return None

    @staticmethod
    def __should_show_progress(src: Path, dest: Path) -> bool:
        """
        是否显示进度条。
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
        new_name: str,
        *,
        try_reflink: Optional[bool] = None,
        **_kwargs: Any,
    ) -> bool:
        """
        通过同目录中间文件复制并替换目标。

        :param fileitem: 源文件项
        :param path: 已存在的目标目录
        :param new_name: 目标文件名
        :param try_reflink: 是否优先尝试写时复制引用链接克隆；None 时遵循系统运行期配置
        :param _kwargs: 忽略多余的扩展参数
        :return: 目标写入完成返回 True，取消或失败返回 False
        """
        if not fileitem.path:
            return False
        return self._write_atomically(
            Path(fileitem.path),
            path / new_name,
            try_reflink=try_reflink,
        )

    def move(
            self,
            fileitem: _SchemaFileItem,
            path: Path,
            new_name: str
    ) -> bool:
        """
        先尝试直接替换目标，失败后复制完成再删除源文件。

        复制成功但删除源失败时只记录警告，仍返回 True，此时源和目标可能同时存在。

        :param fileitem: 源文件项
        :param path: 已存在的目标目录
        :param new_name: 目标文件名
        :return: 目标完成或源目标路径相同返回 True；复制失败返回 False
        """
        if not fileitem.path:
            return False
        src = Path(fileitem.path)
        dest = path / new_name
        if src == dest:
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
        创建本地硬链接。

        :param fileitem: 源文件项
        :param target_file: 完整目标链接路径
        :return: 系统操作返回成功时为 True，否则为 False
        """
        if not fileitem.path:
            return False
        file_path = Path(fileitem.path)
        code, message = SystemUtils.link(file_path, target_file)
        if code != 0:
            logger.error(f"【本地】硬链接文件失败：{message}")
            return False
        return True

    def softlink(self, fileitem: _SchemaFileItem, target_file: Path) -> bool:
        """
        创建本地符号链接。

        :param fileitem: 源文件项
        :param target_file: 完整目标链接路径
        :return: 系统操作返回成功时为 True，否则为 False
        """
        if not fileitem.path:
            return False
        file_path = Path(fileitem.path)
        code, message = SystemUtils.softlink(file_path, target_file)
        if code != 0:
            logger.error(f"【本地】软链接文件失败：{message}")
            return False
        return True

    def reflink(self, fileitem: _SchemaFileItem, target_file: Path) -> bool:
        """
        创建本地引用链接。

        :param fileitem: 源文件项
        :param target_file: 完整目标链接路径
        :return: 系统操作返回成功时为 True，否则为 False
        """
        if not fileitem.path:
            logger.error("【本地】引用链接文件失败：源文件路径为空")
            return False
        code, message = SystemUtils.reflink(Path(fileitem.path), target_file)
        if code != 0:
            logger.error(f"【本地】引用链接文件失败：{message}")
            return False
        return True

    def usage(self) -> Optional[_SchemaStorageUsage]:
        """
        汇总已配置下载目录和媒体库目录所在文件系统的容量。

        :return: 以字节为单位的总容量与可用容量；按目录服务和 Btrfs 配置去重
        """
        directory_helper = DirectoryHelper()
        total_storage, free_storage = SystemUtils.space_usage(
            [Path(d.download_path) for d in directory_helper.get_local_download_dirs() if d.download_path] +
            [Path(d.library_path) for d in directory_helper.get_local_library_dirs() if d.library_path],
            btrfs_fsid_dedup=get_runtime_setting('BTRFS_FSID_DEDUP'),
        )
        return _SchemaStorageUsage(
            total=total_storage,
            available=free_storage
        )
