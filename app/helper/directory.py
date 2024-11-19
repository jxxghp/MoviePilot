from pathlib import Path
from typing import List, Optional

from app import schemas
from app.core.context import MediaInfo
from app.db.systemconfig_oper import SystemConfigOper
from app.schemas.types import SystemConfigKey


class DirectoryHelper:
    """
    下载目录/媒体库目录帮助类
    """

    def __init__(self):
        self.systemconfig = SystemConfigOper()

    def get_dirs(self) -> List[schemas.TransferDirectoryConf]:
        """
        获取所有下载目录
        """
        dir_confs: List[dict] = self.systemconfig.get(SystemConfigKey.Directories)
        if not dir_confs:
            return []
        return [schemas.TransferDirectoryConf(**d) for d in dir_confs]

    def get_download_dirs(self) -> List[schemas.TransferDirectoryConf]:
        """
        获取所有下载目录
        """
        return sorted([d for d in self.get_dirs() if d.download_path], key=lambda x: x.priority)

    def get_local_download_dirs(self) -> List[schemas.TransferDirectoryConf]:
        """
        获取所有本地的可下载目录
        """
        return [d for d in self.get_download_dirs() if d.storage == "local"]

    def get_library_dirs(self) -> List[schemas.TransferDirectoryConf]:
        """
        获取所有媒体库目录
        """
        return sorted([d for d in self.get_dirs() if d.library_path], key=lambda x: x.priority)

    def get_local_library_dirs(self) -> List[schemas.TransferDirectoryConf]:
        """
        获取所有本地的媒体库目录
        """
        return [d for d in self.get_library_dirs() if d.library_storage == "local"]

    def get_dir(self, media: MediaInfo, storage: str = "local",
                src_path: Path = None, dest_path: Path = None) -> Optional[schemas.TransferDirectoryConf]:
        """
        根据媒体信息获取下载目录、媒体库目录配置
        :param media: 媒体信息
        :param storage: 存储类型
        :param src_path: 源目录，有值时直接匹配
        :param dest_path: 目标目录，有值时直接匹配
        """
        # 处理类型
        if not media:
            return None
        # 电影/电视剧
        media_type = media.type.value
        dirs = self.get_dirs()
        # 按照配置顺序查找
        for d in dirs:
            # 没有启用整理的目录
            if not d.monitor_type:
                continue
            # 存储类型不匹配
            if storage and d.storage != storage:
                continue
            # 下载目录
            download_path = Path(d.download_path)
            # 媒体库目录
            library_path = Path(d.library_path)
            # 有源目录时，源目录不匹配下载目录
            if src_path and not src_path.is_relative_to(download_path):
                continue
            # 有目标目录时，目标目录不匹配媒体库目录
            if dest_path and library_path != dest_path:
                continue
            # 目录类型为全部的，符合条件
            if not d.media_type:
                return d
            # 目录类型相等，目录类别为全部，符合条件
            if d.media_type == media_type and not d.media_category:
                return d
            # 目录类型相等，目录类别相等，符合条件
            if d.media_type == media_type and d.media_category == media.category:
                return d

        return None
