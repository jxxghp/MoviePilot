from pathlib import Path
from typing import List, Optional

from app import schemas
from app.core.config import settings
from app.core.context import MediaInfo
from app.db.systemconfig_oper import SystemConfigOper
from app.log import logger
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

    def get_download_dir(self, media: MediaInfo = None, to_path: Path = None) -> Optional[schemas.MediaDirectory]:
        """
        根据媒体信息获取下载目录
        :param media: 媒体信息
        :param to_path: 目标目录
        """
        # 处理类型
        if media:
            media_type = media.type.value
        else:
            media_type = MediaType.UNKNOWN.value
        download_dirs = self.get_download_dirs()
        # 按照配置顺序查找（保存后的数据已经排序）
        for download_dir in download_dirs:
            if not download_dir.path:
                continue
            download_path = Path(download_dir.path)
            # 有目标目录，但目标目录与当前目录不相等时不要
            if to_path and download_path != to_path:
                continue
            # 目录类型为全部的，符合条件
            if not download_dir.media_type:
                return download_dir
            # 目录类型相等，目录类别为全部，符合条件
            if download_dir.media_type == media_type and not download_dir.category:
                return download_dir
            # 目录类型相等，目录类别相等，符合条件
            if download_dir.media_type == media_type and download_dir.category == media.category:
                return download_dir

        return None

    def get_library_dir(self, media: MediaInfo = None, in_path: Path = None,
                        to_path: Path = None) -> Optional[schemas.MediaDirectory]:
        """
        根据媒体信息获取媒体库目录，需判断是否同盘优先
        :param media: 媒体信息
        :param in_path: 源目录
        :param to_path: 目标目录
        """

        def __comman_parts(path1: Path, path2: Path) -> int:
            """
            计算两个路径的公共路径长度
            """
            parts1 = path1.parts
            parts2 = path2.parts
            root_flag = parts1[0] == '/' and parts2[0] == '/'
            length = min(len(parts1), len(parts2))
            for i in range(length):
                if parts1[i] == '/' and parts2[i] == '/':
                    continue
                if parts1[i] != parts2[i]:
                    return i - 1 if root_flag else i
            return length - 1 if root_flag else length

        # 处理类型
        if media:
            media_type = media.type.value
        else:
            media_type = MediaType.UNKNOWN.value

        # 匹配的目录
        matched_dirs = []
        library_dirs = self.get_library_dirs()
        # 按照配置顺序查找（保存后的数据已经排序）
        for library_dir in library_dirs:
            if not library_dir.path:
                continue
            # 有目标目录，但目标目录与当前目录不相等时不要
            if to_path and Path(library_dir.path) != to_path:
                continue
            # 目录类型为全部的，符合条件
            if not library_dir.media_type:
                matched_dirs.append(library_dir)
            # 目录类型相等，目录类别为全部，符合条件
            if library_dir.media_type == media_type and not library_dir.category:
                matched_dirs.append(library_dir)
            # 目录类型相等，目录类别相等，符合条件
            if library_dir.media_type == media_type and library_dir.category == media.category:
                matched_dirs.append(library_dir)

        # 未匹配到
        if not matched_dirs:
            return None

        # 没有目录则创建
        for matched_dir in matched_dirs:
            matched_path = Path(matched_dir.path)
            if not matched_path.exists():
                matched_path.mkdir(parents=True, exist_ok=True)

        # 只匹配到一项
        if len(matched_dirs) == 1:
            return matched_dirs[0]

        # 有源路径，且开启同盘/同目录优先时
        if in_path and settings.TRANSFER_SAME_DISK:
            # 优先同根路径
            max_length = 0
            target_dirs = []
            for matched_dir in matched_dirs:
                try:
                    # 计算in_path和path的公共路径长度
                    relative_len = __comman_parts(in_path, Path(matched_dir.path))
                    if relative_len and relative_len >= max_length:
                        max_length = relative_len
                        target_dirs.append({
                            'path': matched_dir,
                            'relative_len': relative_len
                        })
                except Exception as e:
                    logger.debug(f"计算目标路径时出错：{str(e)}")
                    continue
            if target_dirs:
                target_dirs.sort(key=lambda x: x['relative_len'], reverse=True)
                matched_dirs = [x['path'] for x in target_dirs]

            # 优先同盘
            for matched_dir in matched_dirs:
                matched_path = Path(matched_dir.path)
                if SystemUtils.is_same_disk(matched_path, in_path):
                    return matched_dir

        # 返回最优先的匹配
        return matched_dirs[0]
