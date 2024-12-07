from pathlib import Path
from typing import Optional

from pydantic import BaseModel

from app.schemas.file import FileItem
from app.schemas.system import TransferDirectoryConf


class TransferTorrent(BaseModel):
    """
    待转移任务信息
    """
    downloader: Optional[str] = None
    title: Optional[str] = None
    path: Optional[Path] = None
    hash: Optional[str] = None
    tags: Optional[str] = None
    size: Optional[int] = 0
    userid: Optional[str] = None


class DownloadingTorrent(BaseModel):
    """
    下载中任务信息
    """
    downloader: Optional[str] = None
    hash: Optional[str] = None
    title: Optional[str] = None
    name: Optional[str] = None
    year: Optional[str] = None
    season_episode: Optional[str] = None
    size: Optional[float] = 0
    progress: Optional[float] = 0
    state: Optional[str] = 'downloading'
    upspeed: Optional[str] = None
    dlspeed: Optional[str] = None
    media: Optional[dict] = {}
    userid: Optional[str] = None
    username: Optional[str] = None
    left_time: Optional[str] = None


class TransferInfo(BaseModel):
    """
    文件转移结果信息
    """
    # 是否成功标志
    success: bool = True
    # 整理⼁路径
    fileitem: Optional[FileItem] = None
    # 转移后的目录项
    target_diritem: Optional[FileItem] = None
    # 转移后路径
    target_item: Optional[FileItem] = None
    # 整理方式
    transfer_type: Optional[str] = None
    # 处理文件数
    file_count: Optional[int] = 0
    # 处理文件清单
    file_list: Optional[list] = []
    # 目标文件清单
    file_list_new: Optional[list] = []
    # 总文件大小
    total_size: Optional[float] = 0
    # 失败清单
    fail_list: Optional[list] = []
    # 错误信息
    message: Optional[str] = None
    # 是否需要刮削
    need_scrape: Optional[bool] = False
    # 是否需要通知
    need_notify: Optional[bool] = False

    def to_dict(self):
        """
        返回字典
        """
        dicts = vars(self).copy()
        dicts["fileitem"] = self.fileitem.dict() if self.fileitem else None
        dicts["target_item"] = self.target_item.dict() if self.target_item else None
        return dicts


class EpisodeFormat(BaseModel):
    """
    剧集自定义识别格式
    """
    format: Optional[str] = None
    detail: Optional[str] = None
    part: Optional[str] = None
    offset: Optional[str] = None


class TransferItem(BaseModel):
    # 文件项
    fileitem: FileItem = None
    # 日志ID
    logid: Optional[int] = None
    # 目标存储
    target_storage: Optional[str] = None
    # 目标路径
    target_path: Optional[str] = None
    # TMDB ID
    tmdbid: Optional[int] = None
    # 豆瓣ID
    doubanid: Optional[str] = None
    # 类型
    type_name: Optional[str] = None
    # 季号
    season: Optional[int] = None
    # 整理方式
    transfer_type: Optional[str] = None
    # 自定义格式
    episode_format: Optional[str] = None
    # 指定集数
    episode_detail: Optional[str] = None
    # 指定PART
    episode_part: Optional[str] = None
    # 集数偏移
    episode_offset: Optional[str] = None
    # 最小文件大小
    min_filesize: Optional[int] = 0
    # 刮削
    scrape: Optional[bool] = None
    # 媒体库类型子目录
    library_type_folder: Optional[bool] = None
    # 媒体库类别子目录
    library_category_folder: Optional[bool] = None
    # 复用历史识别信息
    from_history: Optional[bool] = False

    def epformat(self) -> Optional[EpisodeFormat]:
          if self.episode_format or self.episode_detail \
                or self.episode_part or self.episode_offset:
            return EpisodeFormat(
                format = self.episode_format,
                detail = self.episode_detail,
                part = self.episode_part,
                offset = self.episode_offset,
                )
          return None

    def to_transfer_directory_conf(self, dir_info: TransferDirectoryConf = None) -> TransferDirectoryConf:
        """
        转换为`TransferDirectoryConf`对象
        """
        # 属性映射
        _attribute_mapping = {
            "target_storage": "library_storage",
            "target_path": "library_path",
            "transfer_type": "transfer_type",
            "scrape": "scraping",
            "library_type_folder": "library_type_folder",
            "library_category_folder": "library_category_folder",
        }

        # 初始化目录配置
        _dir_info = dir_info or TransferDirectoryConf(
            renaming=True,
            scraping=False,
            notify=False,
            overwrite_mode="never",
        )

        # 更新目录配置对象
        for source_key, target_key in _attribute_mapping.items():
            current_value = getattr(self, source_key)
            # 更新声明的属性
            if current_value or isinstance(current_value, bool) and current_value is not None:
                setattr(_dir_info, target_key, current_value)
        return _dir_info
