import json
import subprocess
from pathlib import Path
from typing import Optional, List

from app import schemas
from app.core.config import settings
from app.log import logger
from app.modules.filemanager.storages import StorageBase, transfer_process
from app.schemas.types import StorageSchema
from app.utils.string import StringUtils
from app.utils.system import SystemUtils


class Rclone(StorageBase):
    """
    rclone相关操作
    """

    # 存储类型
    schema = StorageSchema.Rclone

    # 支持的整理方式
    transtype = {
        "move": "移动",
        "copy": "复制"
    }

    snapshot_check_folder_modtime = settings.RCLONE_SNAPSHOT_CHECK_FOLDER_MODTIME

    def init_storage(self):
        """
        初始化
        """
        pass

    def set_config(self, conf: dict):
        """
        设置配置
        """
        super().set_config(conf)
        filepath = conf.get("filepath")
        if not filepath:
            logger.warn("【rclone】保存配置失败：未设置配置文件路径")
        logger.info(f"【rclone】配置写入文件：{filepath}")
        path = Path(filepath)
        if not path.parent.exists():
            path.parent.mkdir(parents=True)
        path.write_text(conf.get('content'), encoding='utf-8')

    @staticmethod
    def __get_hidden_shell():
        if SystemUtils.is_windows():
            st = subprocess.STARTUPINFO()
            st.dwFlags = subprocess.STARTF_USESHOWWINDOW
            st.wShowWindow = subprocess.SW_HIDE
            return st
        else:
            return None

    @staticmethod
    def __parse_rclone_progress(line: str) -> Optional[float]:
        """
        解析rclone进度输出
        """
        if not line:
            return None
        
        line = line.strip()
        
        # 检查是否包含百分比
        if '%' not in line:
            return None
            
        try:
            # 尝试多种进度输出格式
            if 'ETA' in line:
                # 格式: "Transferred: 1.234M / 5.678M, 22%, 1.234MB/s, ETA 2m3s"
                percent_str = line.split('%')[0].split()[-1]
                return float(percent_str)
            elif 'Transferred:' in line and '100%' in line:
                # 传输完成
                return 100.0
            else:
                # 其他包含百分比的格式
                parts = line.split()
                for part in parts:
                    if '%' in part:
                        percent_str = part.replace('%', '')
                        return float(percent_str)
        except (ValueError, IndexError):
            pass
            
        return None

    def __get_rcloneitem(self, item: dict, parent: Optional[str] = "/") -> schemas.FileItem:
        """
        获取rclone文件项
        """
        if not item:
            return schemas.FileItem()
        if item.get("IsDir"):
            return schemas.FileItem(
                storage=self.schema.value,
                type="dir",
                path=f"{parent}{item.get('Name')}" + "/",
                name=item.get("Name"),
                basename=item.get("Name"),
                modify_time=StringUtils.str_to_timestamp(item.get("ModTime"))
            )
        else:
            return schemas.FileItem(
                storage=self.schema.value,
                type="file",
                path=f"{parent}{item.get('Name')}",
                name=item.get("Name"),
                basename=Path(item.get("Name")).stem,
                extension=Path(item.get("Name")).suffix[1:],
                size=item.get("Size"),
                modify_time=StringUtils.str_to_timestamp(item.get("ModTime"))
            )

    def check(self) -> bool:
        """
        检查存储是否可用
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

    def list(self, fileitem: schemas.FileItem) -> List[schemas.FileItem]:
        """
        浏览文件
        """
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

    def create_folder(self, fileitem: schemas.FileItem, name: str) -> Optional[schemas.FileItem]:
        """
        创建目录
        :param fileitem: 父目录
        :param name: 目录名
        """
        try:
            retcode = subprocess.run(
                [
                    'rclone', 'mkdir',
                    f'MP:{Path(fileitem.path) / name}'
                ],
                startupinfo=self.__get_hidden_shell()
            ).returncode
            if retcode == 0:
                return self.get_item(Path(fileitem.path) / name)
        except Exception as err:
            logger.error(f"【rclone】创建目录失败：{err}")
        return None

    def get_folder(self, path: Path) -> Optional[schemas.FileItem]:
        """
        根据文件路程获取目录，不存在则创建
        """

        def __find_dir(_fileitem: schemas.FileItem, _name: str) -> Optional[schemas.FileItem]:
            """
            查找下级目录中匹配名称的目录
            """
            for sub_folder in self.list(_fileitem):
                if sub_folder.type != "dir":
                    continue
                if sub_folder.name == _name:
                    return sub_folder
            return None

        # 是否已存在
        folder = self.get_item(path)
        if folder:
            return folder
        # 逐级查找和创建目录
        fileitem = schemas.FileItem(storage=self.schema.value, path="/")
        for part in path.parts[1:]:
            dir_file = __find_dir(fileitem, part)
            if dir_file:
                fileitem = dir_file
            else:
                dir_file = self.create_folder(fileitem, part)
                if not dir_file:
                    logger.warn(f"【rclone】创建目录 {fileitem.path}{part} 失败！")
                    return None
                fileitem = dir_file
        return fileitem

    def get_item(self, path: Path) -> Optional[schemas.FileItem]:
        """
        获取文件或目录，不存在返回None
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

    def delete(self, fileitem: schemas.FileItem) -> bool:
        """
        删除文件
        """
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

    def rename(self, fileitem: schemas.FileItem, name: str) -> bool:
        """
        重命名文件
        """
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

    def download(self, fileitem: schemas.FileItem, path: Path = None) -> Optional[Path]:
        """
        带实时进度显示的下载
        """
        local_path = (path or settings.TEMP_PATH) / fileitem.name
        
        # 初始化进度条
        logger.info(f"【rclone】开始下载: {fileitem.name} -> {local_path}")
        progress_callback = transfer_process(Path(fileitem.path).as_posix())
        
        try:
            # 使用rclone的进度显示功能
            process = subprocess.Popen(
                [
                    'rclone', 'copyto',
                    '--progress',  # 启用进度显示
                    '--stats', '1s',  # 每秒更新一次统计信息
                    f'MP:{fileitem.path}',
                    f'{local_path}'
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                startupinfo=self.__get_hidden_shell(),
                universal_newlines=True,
                bufsize=1
            )
            
            # 监控进度输出
            last_progress = 0
            for line in process.stdout:
                if line:
                    # 解析rclone的进度输出
                    progress = self.__parse_rclone_progress(line)
                    if progress is not None and progress > last_progress:
                        progress_callback(progress)
                        last_progress = progress
                        if progress >= 100:
                            break
            
            # 等待进程完成
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

    def upload(self, fileitem: schemas.FileItem, path: Path,
               new_name: Optional[str] = None) -> Optional[schemas.FileItem]:
        """
        带实时进度显示的上传
        :param fileitem: 上传目录项
        :param path: 本地文件路径
        :param new_name: 上传后文件名
        """
        target_name = new_name or path.name
        new_path = Path(fileitem.path) / target_name
        
        # 初始化进度条
        logger.info(f"【rclone】开始上传: {path} -> {new_path}")
        progress_callback = transfer_process(path.as_posix())
        
        try:
            # 使用rclone的进度显示功能
            process = subprocess.Popen(
                [
                    'rclone', 'copyto',
                    '--progress',  # 启用进度显示
                    '--stats', '1s',  # 每秒更新一次统计信息
                    path.as_posix(),
                    f'MP:{new_path}'
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                startupinfo=self.__get_hidden_shell(),
                universal_newlines=True,
                bufsize=1
            )
            
            # 监控进度输出
            last_progress = 0
            for line in process.stdout:
                if line:
                    # 解析rclone的进度输出
                    progress = self.__parse_rclone_progress(line)
                    if progress is not None and progress > last_progress:
                        progress_callback(progress)
                        last_progress = progress
                        if progress >= 100:
                            break
            
            # 等待进程完成
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

    def detail(self, fileitem: schemas.FileItem) -> Optional[schemas.FileItem]:
        """
        获取文件详情
        """
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

    def move(self, fileitem: schemas.FileItem, path: Path, new_name: str) -> bool:
        """
        移动文件
        :param fileitem: 文件项
        :param path: 目标目录
        :param new_name: 新文件名
        """
        target_path = path / new_name
        
        # 初始化进度条
        logger.info(f"【rclone】开始移动: {fileitem.path} -> {target_path}")
        progress_callback = transfer_process(Path(fileitem.path).as_posix())
        
        try:
            # 使用rclone的进度显示功能
            process = subprocess.Popen(
                [
                    'rclone', 'moveto',
                    '--progress',  # 启用进度显示
                    '--stats', '1s',  # 每秒更新一次统计信息
                    f'MP:{fileitem.path}',
                    f'MP:{target_path}'
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                startupinfo=self.__get_hidden_shell(),
                universal_newlines=True,
                bufsize=1
            )
            
            # 监控进度输出
            last_progress = 0
            for line in process.stdout:
                if line:
                    # 解析rclone的进度输出
                    progress = self.__parse_rclone_progress(line)
                    if progress is not None and progress > last_progress:
                        progress_callback(progress)
                        last_progress = progress
                        if progress >= 100:
                            break
            
            # 等待进程完成
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

    def copy(self, fileitem: schemas.FileItem, path: Path, new_name: str) -> bool:
        """
        复制文件
        :param fileitem: 文件项
        :param path: 目标目录
        :param new_name: 新文件名
        """
        target_path = path / new_name
        
        # 初始化进度条
        logger.info(f"【rclone】开始复制: {fileitem.path} -> {target_path}")
        progress_callback = transfer_process(Path(fileitem.path).as_posix())
        
        try:
            # 使用rclone的进度显示功能
            process = subprocess.Popen(
                [
                    'rclone', 'copyto',
                    '--progress',  # 启用进度显示
                    '--stats', '1s',  # 每秒更新一次统计信息
                    f'MP:{fileitem.path}',
                    f'MP:{target_path}'
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                startupinfo=self.__get_hidden_shell(),
                universal_newlines=True,
                bufsize=1
            )
            
            # 监控进度输出
            last_progress = 0
            for line in process.stdout:
                if line:
                    # 解析rclone的进度输出
                    progress = self.__parse_rclone_progress(line)
                    if progress is not None and progress > last_progress:
                        progress_callback(progress)
                        last_progress = progress
                        if progress >= 100:
                            break
            
            # 等待进程完成
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

    def link(self, fileitem: schemas.FileItem, target_file: Path) -> bool:
        pass

    def softlink(self, fileitem: schemas.FileItem, target_file: Path) -> bool:
        pass

    def usage(self) -> Optional[schemas.StorageUsage]:
        """
        存储使用情况
        """
        conf = self.get_config()
        if not conf:
            return None
        file_path = conf.config.get("filepath")
        if not file_path or not Path(file_path).exists():
            return None
        # 读取rclone文件，检查是否有[MP]节点配置
        with open(file_path, "r", encoding="utf-8") as f:
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
                return schemas.StorageUsage(
                    total=items.get("total"),
                    available=items.get("free")
                )
        except Exception as err:
            logger.error(f"【rclone】获取存储使用情况失败：{err}")
        return None
