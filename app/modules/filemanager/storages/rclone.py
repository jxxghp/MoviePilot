"""
基于 rclone MP 远端的文件存储实现与命令进度适配。
"""

import json
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, List, Optional, Union

from app.foundation import temporal as time_tools
from app.modules.filemanager.storages import StorageBase, transfer_process
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.schemas.exception import StorageQueryError
from app.schemas.file import FileItem as _SchemaFileItem
from app.schemas.file import StorageUsage as _SchemaStorageUsage
from app.schemas.types import StorageSchema

# 路径锁缓存的软容量上限；全部锁都被占用时允许暂时超过
_MAX_FOLDER_LOCKS = 4096
_folder_locks: OrderedDict[str, threading.Lock] = OrderedDict()
_folder_locks_guard = threading.Lock()


def _evict_unused_folder_locks_locked() -> None:
    """
    在持有全局锁表互斥锁时淘汰旧路径锁，避免大量不同目录导致锁表无限增长。
    """
    while len(_folder_locks) >= _MAX_FOLDER_LOCKS:
        for key, lock in list(_folder_locks.items()):
            if not lock.locked():
                _folder_locks.pop(key, None)
                break
        else:
            break


class Rclone(StorageBase):
    """
    通过 rclone 命令行操作 MP 远端存储并解析传输进度。
    """

    # 存储类型
    schema = StorageSchema.Rclone

    # 支持的整理方式
    transtype = {
        "move": "移动",
        "copy": "复制"
    }

    @property
    def snapshot_check_folder_modtime(self) -> bool:
        """
        读取当前目录修改时间检查开关，配置保存后立即生效。

        :return: RCLONE_SNAPSHOT_CHECK_FOLDER_MODTIME 的布尔值
        """
        return bool(get_runtime_setting('RCLONE_SNAPSHOT_CHECK_FOLDER_MODTIME'))

    def init_storage(self) -> None:
        """
        保留初始化钩子；rclone 进程按每次操作启动。
        """
        pass

    def set_config(self, conf: dict[str, Any]) -> None:
        """
        先持久化存储配置，再将配置内容写入指定本地文件。

        写入异常向上传播，此时存储配置可能已经保存。

        :param conf: 配置字典，filepath 指定文件路径，content 指定完整文本；父目录不存在时创建
        """
        super().set_config(conf)
        filepath = conf.get("filepath")
        if not filepath:
            logger.warn("【rclone】保存配置失败：未设置配置文件路径")
            return
        logger.info(f"【rclone】配置写入文件：{filepath}")
        path = Path(filepath)
        if not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(conf.get('content') or ''), encoding='utf-8')

    @staticmethod
    def __get_hidden_shell() -> Optional[Any]:
        """
        在 Windows 下隐藏命令窗口，其他平台无需启动窗口配置。
        """
        if sys.platform == "win32":
            st = subprocess.STARTUPINFO()
            st.dwFlags = subprocess.STARTF_USESHOWWINDOW
            st.wShowWindow = subprocess.SW_HIDE
            return st
        else:
            return None

    @staticmethod
    def __parse_rclone_progress(line: str) -> Optional[float]:
        """
        解析rclone进度输出。
        """
        if not line:
            return None

        line = line.strip()

        if '%' not in line:
            return None

        try:
            if 'ETA' in line:
                # 格式: "Transferred: 1.234M / 5.678M, 22%, 1.234MB/s, ETA 2m3s"
                percent_str = line.split('%')[0].split()[-1]
                return float(percent_str)
            elif 'Transferred:' in line and '100%' in line:
                return 100.0
            else:
                parts = line.split()
                for part in parts:
                    if '%' in part:
                        percent_str = part.replace('%', '')
                        return float(percent_str)
        except (ValueError, IndexError):
            pass

        return None

    def __get_rcloneitem(self, item: dict[str, Any], parent: Optional[str] = "/") -> _SchemaFileItem:
        """
        获取rclone文件项。
        """
        name = item.get("Name")
        if not isinstance(name, str) or not name:
            raise ValueError("rclone 文件项缺少名称")
        if item.get("IsDir"):
            return _SchemaFileItem(
                storage=self.schema.value,
                type="dir",
                path=f"{parent}{item.get('Name')}" + "/",
                name=item.get("Name"),
                basename=item.get("Name"),
                modify_time=time_tools.parse_timestamp(item.get("ModTime") or "")
            )
        else:
            return _SchemaFileItem(
                storage=self.schema.value,
                type="file",
                path=f"{parent}{item.get('Name')}",
                name=item.get("Name"),
                basename=Path(name).stem,
                extension=Path(name).suffix[1:],
                size=item.get("Size"),
                modify_time=time_tools.parse_timestamp(item.get("ModTime") or "")
            )

    @staticmethod
    def __normalize_remote_path(path: Union[Path, str]) -> str:
        """
        规范化远端路径，统一目录锁键值。
        """
        path_str = Path(str(path or "/")).as_posix()
        if not path_str.startswith("/"):
            path_str = f"/{path_str}"
        if path_str != "/":
            path_str = path_str.rstrip("/")
        return path_str or "/"

    @staticmethod
    def __get_path_lock(path: Union[Path, str]) -> threading.Lock:
        """
        获取指定远端路径的模块级锁。
        """
        normalized = Rclone.__normalize_remote_path(path)
        with _folder_locks_guard:
            lock = _folder_locks.get(normalized)
            if lock:
                _folder_locks.move_to_end(normalized)
                return lock
            _evict_unused_folder_locks_locked()
            lock = threading.Lock()
            _folder_locks[normalized] = lock
            return lock

    def __wait_for_item(
        self, path: Path, retries: int = 3, delay: float = 0.2
    ) -> Optional[_SchemaFileItem]:
        """
        等待目录或文件在远端可见，兼容云盘最终一致性延迟。
        """
        for attempt in range(retries):
            item = self.get_item(path)
            if item:
                return item
            if attempt < retries - 1:
                time.sleep(delay)
        return None

    def check(self) -> bool:
        """
        执行 rclone lsf MP: 检查远端是否可访问。

        :return: 命令退出码为 0 时返回 True，执行异常或非零退出返回 False
        """
        try:
            retcode = subprocess.run(
                ['rclone', 'lsf', 'MP:'],
                startupinfo=self.__get_hidden_shell()
            ).returncode
            if retcode == 0:
                return True
        except Exception as err:
            logger.error(f"【rclone】存储检查失败：{err}")
        return False

    def list(self, fileitem: _SchemaFileItem) -> List[_SchemaFileItem]:
        """
        通过 lsjson 列举远端直接子项，文件项则直接原样返回。

        :param fileitem: MP 远端目录或文件项；目录路径应保留末尾斜杠以便拼接子项
        :return: 直接子项列表；命令或解析失败返回空列表，不能据此区分空目录
        """
        if not fileitem.path:
            return []
        if fileitem.type == "file":
            return [fileitem]
        try:
            ret = subprocess.run(
                [
                    'rclone', 'lsjson',
                    f'MP:{fileitem.path}'
                ],
                capture_output=True,
                startupinfo=self.__get_hidden_shell()
            )
            if ret.returncode == 0:
                items = json.loads(ret.stdout)
                return [self.__get_rcloneitem(item, parent=fileitem.path) for item in items]
        except Exception as err:
            logger.error(f"【rclone】浏览文件失败：{err}")
        return []

    def create_folder(self, fileitem: _SchemaFileItem, name: str) -> Optional[_SchemaFileItem]:
        """
        请求创建远端目录，并短暂重查以兼容可见性延迟。

        :param fileitem: 父目录项
        :param name: 目标子目录名称
        :return: 查询到的目标项；创建或确认失败返回 None，即使创建命令已成功但目标暂不可见
        """
        if not fileitem.path:
            return None
        path = Path(self.__normalize_remote_path(Path(fileitem.path) / name))
        try:
            retcode = subprocess.run(
                [
                    'rclone', 'mkdir',
                    f'MP:{path}'
                ],
                startupinfo=self.__get_hidden_shell()
            ).returncode
            if retcode == 0:
                folder = self.__wait_for_item(path)
                if folder:
                    return folder
                logger.warn(f"【rclone】目录 {path} 创建成功后暂未可见")
                return None
            folder = self.__wait_for_item(path, retries=2)
            if folder:
                logger.info(f"【rclone】目录 {path} 已存在，忽略重复创建")
                return folder
        except Exception as err:
            logger.error(f"【rclone】创建目录失败：{err}")
            folder = self.__wait_for_item(path, retries=2)
            if folder:
                logger.info(f"【rclone】目录 {path} 已存在，忽略创建异常")
                return folder
        return None

    def get_folder(self, path: Path) -> Optional[_SchemaFileItem]:
        """
        逐级取得或创建远端目录，在进程内按路径串行处理。

        :param path: 目标远端目录路径，自动规范化为以斜杠开头的路径
        :return: 最终路径项；任一级创建或确认失败返回 None
        """
        normalized = Path(self.__normalize_remote_path(path))

        folder = self.get_item(normalized)
        if folder:
            return folder
        fileitem = _SchemaFileItem(storage=self.schema.value, type="dir", path="/")
        for part in normalized.parts[1:]:
            if not fileitem.path:
                return None
            current_path = Path(self.__normalize_remote_path(Path(fileitem.path) / part))
            with self.__get_path_lock(current_path):
                dir_file = self.get_item(current_path)
                if not dir_file:
                    dir_file = self.create_folder(fileitem, part)
                if not dir_file:
                    logger.warn(f"【rclone】创建目录 {current_path} 失败！")
                    return None
                fileitem = dir_file
        return fileitem

    def get_item(self, path: Path) -> Optional[_SchemaFileItem]:
        """
        列举父目录并按文件名查找远端项。

        :param path: MP 远端的目标路径
        :return: 匹配的文件或目录项；未找到、命令失败或解析失败返回 None
        """
        try:
            ret = subprocess.run(
                [
                    'rclone', 'lsjson',
                    f'MP:{path.parent}'
                ],
                capture_output=True,
                startupinfo=self.__get_hidden_shell()
            )
            if ret.returncode == 0:
                items = json.loads(ret.stdout)
                for item in items:
                    if item.get("Name") == path.name:
                        return self.__get_rcloneitem(item, parent=str(path.parent) + "/")
            return None
        except Exception as err:
            logger.debug(f"【rclone】获取文件项失败：{err}")
        return None

    def get_item_strict(self, path: Path) -> Optional[_SchemaFileItem]:
        """
        列举父目录并区分远端项缺失与查询故障。

        :param path: 需要确认状态的 MP 远端路径
        :return: 匹配的文件或目录项；列表无匹配项或退出码为 3、4 时返回 None
        :raises StorageQueryError: 命令无法执行、其他非零退出或响应 JSON 无法解析
        """
        try:
            ret = subprocess.run(
                [
                    'rclone', 'lsjson',
                    f'MP:{path.parent}'
                ],
                capture_output=True,
                startupinfo=self.__get_hidden_shell()
            )
        except Exception as err:
            raise StorageQueryError(f"【rclone】查询文件项失败: {path} - {err}") from err
        if ret.returncode in (3, 4):
            # 目录或文件不存在，是确定结果
            return None
        if ret.returncode != 0:
            errmsg = (ret.stderr or b"").decode(errors="ignore").strip()
            raise StorageQueryError(f"【rclone】查询文件项失败: {path} - {errmsg}")
        try:
            items = json.loads(ret.stdout)
        except Exception as err:
            raise StorageQueryError(f"【rclone】解析查询结果失败: {path} - {err}") from err
        for item in items:
            if item.get("Name") == path.name:
                return self.__get_rcloneitem(item, parent=str(path.parent) + "/")
        return None

    def delete(self, fileitem: _SchemaFileItem) -> bool:
        """
        调用 deletefile 删除远端文件。

        :param fileitem: 需要删除的文件项；此方法不递归删除目录
        :return: 命令退出码为 0 时返回 True，否则返回 False
        """
        if not fileitem.path:
            return False
        try:
            retcode = subprocess.run(
                [
                    'rclone', 'deletefile',
                    f'MP:{fileitem.path}'
                ],
                startupinfo=self.__get_hidden_shell()
            ).returncode
            if retcode == 0:
                return True
        except Exception as err:
            logger.error(f"【rclone】删除文件失败：{err}")
        return False

    def rename(self, fileitem: _SchemaFileItem, name: str) -> bool:
        """
        调用 moveto 在原目录内改名。

        :param fileitem: 源存储项
        :param name: 目标名称，已存在目标的处理由 rclone 决定
        :return: 命令退出码为 0 时返回 True，否则返回 False
        """
        if not fileitem.path:
            return False
        try:
            retcode = subprocess.run(
                [
                    'rclone', 'moveto',
                    f'MP:{fileitem.path}',
                    f'MP:{Path(fileitem.path).parent / name}'
                ],
                startupinfo=self.__get_hidden_shell()
            ).returncode
            if retcode == 0:
                return True
        except Exception as err:
            logger.error(f"【rclone】重命名文件失败：{err}")
        return False

    def download(self, fileitem: _SchemaFileItem, path: Optional[Path] = None) -> Optional[Path]:
        """
        调用 copyto 下载远端文件并回调进度。

        捕获异常时尝试删除部分文件；命令正常退出但退出码非零时不清理残留文件。

        :param fileitem: 源远端文件，文件名会提取为安全的本地名称
        :param path: 本地保存目录；为空时使用 TEMP_PATH
        :return: 成功下载的本地路径；路径无效或传输失败返回 None
        """
        if not fileitem.path:
            return None
        local_path = self._build_download_path(fileitem, path or get_runtime_setting('TEMP_PATH'))
        if not local_path:
            return None

        logger.info(f"【rclone】开始下载: {fileitem.name} -> {local_path}")
        progress_callback = transfer_process(Path(fileitem.path).as_posix())

        try:
            process = subprocess.Popen(
                [
                    'rclone', 'copyto',
                    '--progress',
                    '--stats', '1s',
                    f'MP:{fileitem.path}',
                    f'{local_path}'
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                startupinfo=self.__get_hidden_shell(),
                universal_newlines=True,
                bufsize=1
            )

            assert process.stdout is not None
            last_progress = 0.0
            for line in process.stdout:
                if line:
                    progress = self.__parse_rclone_progress(line)
                    if progress is not None and progress > last_progress:
                        progress_callback(progress)
                        last_progress = progress
                        if progress >= 100:
                            break

            retcode = process.wait()
            if retcode == 0:
                logger.info(f"【rclone】下载完成: {fileitem.name}")
                return local_path
            else:
                logger.error(f"【rclone】下载失败: {fileitem.name}")
                return None

        except Exception as err:
            logger.error(f"【rclone】下载失败: {fileitem.name} - {err}")
            # 删除可能部分下载的文件
            if local_path.exists():
                local_path.unlink()
            return None

    def upload(self, fileitem: _SchemaFileItem, path: Path,
               new_name: Optional[str] = None) -> Optional[_SchemaFileItem]:
        """
        调用 copyto 上传本地文件，完成后查询目标项。

        :param fileitem: 远端目标目录
        :param path: 本地源文件路径；成功后仍保留
        :param new_name: 远端目标名称；为空时使用源文件名
        :return: 上传后查询到的目标项；传输或后续查询失败返回 None
        """
        if not fileitem.path:
            return None
        target_name = new_name or path.name
        new_path = Path(fileitem.path) / target_name

        logger.info(f"【rclone】开始上传: {path} -> {new_path}")
        progress_callback = transfer_process(path.as_posix())

        try:
            process = subprocess.Popen(
                [
                    'rclone', 'copyto',
                    '--progress',
                    '--stats', '1s',
                    path.as_posix(),
                    f'MP:{new_path}'
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                startupinfo=self.__get_hidden_shell(),
                universal_newlines=True,
                bufsize=1
            )

            assert process.stdout is not None
            last_progress = 0.0
            for line in process.stdout:
                if line:
                    progress = self.__parse_rclone_progress(line)
                    if progress is not None and progress > last_progress:
                        progress_callback(progress)
                        last_progress = progress
                        if progress >= 100:
                            break

            retcode = process.wait()
            if retcode == 0:
                logger.info(f"【rclone】上传完成: {target_name}")
                return self.get_item(new_path)
            else:
                logger.error(f"【rclone】上传失败: {target_name}")
                return None

        except Exception as err:
            logger.error(f"【rclone】上传失败: {target_name} - {err}")
            return None

    def detail(self, fileitem: _SchemaFileItem) -> Optional[_SchemaFileItem]:
        """
        调用 lsjson 查询给定路径并取首个结果。

        目录输入会取其首个子项，返回路径按根目录拼接，调用方不应据此假设保留原路径。

        :param fileitem: 待查询的远端项
        :return: 首个结果构造的文件项；执行、解析或无结果时返回 None
        """
        if not fileitem.path:
            return None
        try:
            ret = subprocess.run(
                [
                    'rclone', 'lsjson',
                    f'MP:{fileitem.path}'
                ],
                capture_output=True,
                startupinfo=self.__get_hidden_shell()
            )
            if ret.returncode == 0:
                items = json.loads(ret.stdout)
                return self.__get_rcloneitem(items[0])
        except Exception as err:
            logger.error(f"【rclone】获取文件详情失败：{err}")
        return None

    def move(self, fileitem: _SchemaFileItem, path: Path, new_name: str) -> bool:
        """
        调用 moveto 在 MP 远端内移动文件并回调进度。

        :param fileitem: 源远端文件项
        :param path: 远端目标目录
        :param new_name: 目标文件名
        :return: 命令退出码为 0 时返回 True，否则返回 False；具体移动语义由 rclone 后端决定
        """
        if not fileitem.path:
            return False
        target_path = path / new_name

        logger.info(f"【rclone】开始移动: {fileitem.path} -> {target_path}")
        progress_callback = transfer_process(Path(fileitem.path).as_posix())

        try:
            process = subprocess.Popen(
                [
                    'rclone', 'moveto',
                    '--progress',
                    '--stats', '1s',
                    f'MP:{fileitem.path}',
                    f'MP:{target_path}'
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                startupinfo=self.__get_hidden_shell(),
                universal_newlines=True,
                bufsize=1
            )

            assert process.stdout is not None
            last_progress = 0.0
            for line in process.stdout:
                if line:
                    progress = self.__parse_rclone_progress(line)
                    if progress is not None and progress > last_progress:
                        progress_callback(progress)
                        last_progress = progress
                        if progress >= 100:
                            break

            retcode = process.wait()
            if retcode == 0:
                logger.info(f"【rclone】移动完成: {fileitem.name}")
                return True
            else:
                logger.error(f"【rclone】移动失败: {fileitem.name}")
                return False

        except Exception as err:
            logger.error(f"【rclone】移动失败: {fileitem.name} - {err}")
            return False

    def copy(self, fileitem: _SchemaFileItem, path: Path, new_name: str, **_kwargs: Any) -> bool:
        """
        调用 copyto 在 MP 远端内复制文件并回调进度。

        :param fileitem: 源远端文件项
        :param path: 远端目标目录
        :param new_name: 目标文件名
        :return: 命令退出码为 0 时返回 True，否则返回 False；失败时不撤销已写入内容
        """
        if not fileitem.path:
            return False
        target_path = path / new_name

        logger.info(f"【rclone】开始复制: {fileitem.path} -> {target_path}")
        progress_callback = transfer_process(Path(fileitem.path).as_posix())

        try:
            process = subprocess.Popen(
                [
                    'rclone', 'copyto',
                    '--progress',
                    '--stats', '1s',
                    f'MP:{fileitem.path}',
                    f'MP:{target_path}'
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                startupinfo=self.__get_hidden_shell(),
                universal_newlines=True,
                bufsize=1
            )

            assert process.stdout is not None
            last_progress = 0.0
            for line in process.stdout:
                if line:
                    progress = self.__parse_rclone_progress(line)
                    if progress is not None and progress > last_progress:
                        progress_callback(progress)
                        last_progress = progress
                        if progress >= 100:
                            break

            retcode = process.wait()
            if retcode == 0:
                logger.info(f"【rclone】复制完成: {fileitem.name}")
                return True
            else:
                logger.error(f"【rclone】复制失败: {fileitem.name}")
                return False

        except Exception as err:
            logger.error(f"【rclone】复制失败: {fileitem.name} - {err}")
            return False

    def link(self, fileitem: _SchemaFileItem, target_file: Path) -> bool:
        """
        保留硬链接接口；rclone 存储当前不支持此操作。

        :param fileitem: 兼容接口的源文件项，当前忽略
        :param target_file: 兼容接口的目标路径，当前忽略
        :return: 固定返回 False，不创建链接
        """
        return False

    def softlink(self, fileitem: _SchemaFileItem, target_file: Path) -> bool:
        """
        保留符号链接接口；rclone 存储当前不支持此操作。

        :param fileitem: 兼容接口的源文件项，当前忽略
        :param target_file: 兼容接口的目标路径，当前忽略
        :return: 固定返回 False，不创建链接
        """
        return False

    def usage(self) -> Optional[_SchemaStorageUsage]:
        """
        确认配置含 MP 远端后，通过 about 查询容量。

        :return: 以字节为单位的总容量与可用容量；配置缺失、没有 MP 节或查询失败返回 None
        """
        conf = self.get_config()
        if not conf:
            return None
        file_path = (conf.config or {}).get("filepath")
        if not file_path or not Path(file_path).exists():
            return None
        # 读取rclone文件，检查是否有[MP]节点配置
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
            if not lines:
                return None
            if not any("[MP]" in line.strip() for line in lines):
                return None
        try:
            ret = subprocess.run(
                [
                    'rclone', 'about',
                    'MP:/', '--json'
                ],
                capture_output=True,
                startupinfo=self.__get_hidden_shell()
            )
            if ret.returncode == 0:
                items = json.loads(ret.stdout)
                return _SchemaStorageUsage(
                    total=items.get("total"),
                    available=items.get("free")
                )
        except Exception as err:
            logger.error(f"【rclone】获取存储使用情况失败：{err}")
        return None
