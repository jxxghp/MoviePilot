from pathlib import Path
from typing import List, Optional

from app import schemas
from app.core.config import settings
from app.core.context import MediaInfo
from app.db.systemconfig_oper import SystemConfigOper
from app.schemas.types import SystemConfigKey, MediaType
from app.utils.system import SystemUtils


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
            if not media_dir.path:
                continue
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

    def get_library_dir(self, media: MediaInfo = None, in_path: Path = None) -> Optional[schemas.MediaDirectory]:
        """
        根据媒体信息获取媒体库目录，需判断是否同盘优先
        :param media: 媒体信息
        :param in_path: 源目录
        """
        matched_dirs = []
        library_dirs = self.get_library_dirs()
        # 按照配置顺序查找（保存后的数据已经排序）
        for library_dir in library_dirs:
            if not library_dir.path:
                continue
            # 没有媒体信息时，返回第一个类型为全部的目录
            if (not media or media.type == MediaType.UNKNOWN) and not library_dir.media_type:
                matched_dirs.append(library_dir)
            # 目录类型为全部的，符合条件
            if not library_dir.media_type:
                matched_dirs.append(library_dir)
            # 处理类型
            if media.genre_ids \
                    and set(media.genre_ids).intersection(set(settings.ANIME_GENREIDS)):
                media_type = "动漫"
            else:
                media_type = media.type.value
            # 目录类型相等，目录类别为全部，符合条件
            if library_dir.media_type == media_type and not library_dir.category:
                matched_dirs.append(library_dir)
            # 目录类型相等，目录类别相等，符合条件
            if library_dir.media_type == media_type and library_dir.category == media.category:
                matched_dirs.append(library_dir)

        # 未匹配到
        if not matched_dirs:
            return None

        # 优先同盘
        if in_path and settings.TRANSFER_SAME_DISK:
            for matched_dir in matched_dirs:
                matched_path = Path(matched_dir.path)
                if not matched_path.exists():
                    matched_path.mkdir(parents=True, exist_ok=True)
                if SystemUtils.is_same_disk(matched_path, in_path):
                    return matched_dir

        return matched_dirs[0]
