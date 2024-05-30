from pathlib import Path

from pydantic import BaseModel


class TransferTorrent(BaseModel):
    """
    待转移任务信息
    """
    title: str | None = None
    path: Path | None = None
    hash: str | None = None
    tags: str | None = None
    userid: str | None = None


class DownloadingTorrent(BaseModel):
    """
    下载中任务信息
    """
    hash: str | None = None
    title: str | None = None
    name: str | None = None
    year: str | None = None
    season_episode: str | None = None
    size: float | None = 0
    progress: float | None = 0
    state: str | None = 'downloading'
    upspeed: str | None = None
    dlspeed: str | None = None
    media: dict | None = {}
    userid: str | None = None
    username: str | None = None
    left_time: str | None = None


class TransferInfo(BaseModel):
    """
    文件转移结果信息
    """
    # 是否成功标志
    success: bool = True
    # 转移⼁路径
    path: Path | None = None
    # 转移后路径
    target_path: Path | None = None
    # 是否蓝光原盘
    is_bluray: bool | None = False
    # 处理文件数
    file_count: int | None = 0
    # 处理文件清单
    file_list: list | None = []
    # 目标文件清单
    file_list_new: list | None = []
    # 总文件大小
    total_size: float | None = 0
    # 失败清单
    fail_list: list | None = []
    # 错误信息
    message: str | None = None
    # 是否需要刮削
    need_scrape: bool | None = False

    def to_dict(self):
        """
        返回字典
        """
        dicts = vars(self).copy()  # 创建一个字典的副本以避免修改原始数据
        dicts["path"] = str(self.path) if self.path else None
        dicts["target_path"] = str(self.target_path) if self.target_path else None
        return dicts


class EpisodeFormat(BaseModel):
    """
    剧集自定义识别格式
    """
    format: str | None = None
    detail: str | None = None
    part: str | None = None
    offset: int | None = None
