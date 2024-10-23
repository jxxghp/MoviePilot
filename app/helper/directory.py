from pathlib import Path
from typing import List, Optional

from app import schemas
from app.core.context import MediaInfo
from app.db.systemconfig_oper import SystemConfigOper
from app.schemas.types import SystemConfigKey, MediaType


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

    def get_dir(self, media: MediaInfo, src_path: Path = None, dest_path: Path = None,
                local: bool = False) -> Optional[schemas.TransferDirectoryConf]:
        """
        根据媒体信息获取下载目录、媒体库目录配置
        :param media: 媒体信息
        :param src_path: 源目录，有值时直接匹配
        :param dest_path: 目标目录，有值时直接匹配
        :param local: 是否本地目录
        """
        # 处理类型
        if media:
            media_type = media.type.value
        else:
            media_type = MediaType.UNKNOWN.value
        dirs = self.get_dirs()
        # 按照配置顺序查找
        for d in dirs:
            if not d.download_path or not d.library_path:
                continue
            # 下载目录
            download_path = Path(d.download_path)
            # 媒体库目录
            library_path = Path(d.library_path)
            # 媒体类型
            # 有目录时直接匹配
            if src_path and download_path != src_path:
                continue
            if dest_path and library_path != dest_path:
                continue
            # 本地目录
            if local and d.storage != "local":
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
