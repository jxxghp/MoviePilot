from typing import List, Optional

from app import schemas
from app.core.config import settings
from app.core.context import MediaInfo
from app.db.systemconfig_oper import SystemConfigOper
from app.schemas.types import SystemConfigKey, MediaType


class DirectoryHelper:
    """
    下载目录/媒体库目录帮助类
    """

    def __init__(self):
        self.systemconfig = SystemConfigOper()

    def get_download_dirs(self) -> List[schemas.MediaDirectory]:
        """
        获取下载目录
        """
        dir_conf: List[dict] = self.systemconfig.get(SystemConfigKey.DownloadDirectories)
        if not dir_conf:
            return []
        return [schemas.MediaDirectory(**d) for d in dir_conf]

    def get_library_dirs(self) -> List[schemas.MediaDirectory]:
        """
        获取媒体库目录
        """
        dir_conf: List[dict] = self.systemconfig.get(SystemConfigKey.LibraryDirectories)
        if not dir_conf:
            return []
        return [schemas.MediaDirectory(**d) for d in dir_conf]

    def get_download_dir(self, media: MediaInfo = None) -> Optional[schemas.MediaDirectory]:
        """
        根据媒体信息获取下载目录
        :param media: 媒体信息
        """
        media_dirs = self.get_download_dirs()
        # 按照配置顺序查找（保存后的数据已经排序）
        for media_dir in media_dirs:
            # 没有媒体信息时，返回第一个类型为全部的目录
            if (not media or media.type == MediaType.UNKNOWN) and not media_dir.media_type:
                return media_dir
            # 目录类型为全部的，符合条件
            if not media_dir.media_type:
                return media_dir
            # 处理类型
            if media.genre_ids \
                    and set(media.genre_ids).intersection(set(settings.ANIME_GENREIDS)):
                media_type = "动漫"
            else:
                media_type = media.type.value
            # 目录类型相等，目录类别为全部，符合条件
            if media_dir.media_type == media_type and not media_dir.category:
                return media_dir
            # 目录类型相等，目录类别相等，符合条件
            if media_dir.media_type == media_type and media_dir.category == media.category:
                return media_dir

        return None

    def get_library_dir(self, media: MediaInfo = None) -> Optional[schemas.MediaDirectory]:
        """
        根据媒体信息获取媒体库目录
        :param media: 媒体信息
        """
        library_dirs = self.get_library_dirs()
        # 按照配置顺序查找（保存后的数据已经排序）
        for library_dir in library_dirs:
            # 没有媒体信息时，返回第一个类型为全部的目录
            if (not media or media.type == MediaType.UNKNOWN) and not library_dir.media_type:
                return library_dir
            # 目录类型为全部的，符合条件
            if not library_dir.media_type:
                return library_dir
            # 处理类型
            if media.genre_ids \
                    and set(media.genre_ids).intersection(set(settings.ANIME_GENREIDS)):
                media_type = "动漫"
            else:
                media_type = media.type.value
            # 目录类型相等，目录类别为全部，符合条件
            if library_dir.media_type == media_type and not library_dir.category:
                return library_dir
            # 目录类型相等，目录类别相等，符合条件
            if library_dir.media_type == media_type and library_dir.category == media.category:
                return library_dir

        return None
