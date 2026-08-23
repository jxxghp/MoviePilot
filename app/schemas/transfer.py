from pathlib import Path
from typing import List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.media import OptionalMediaIdentityMixin
from app.schemas.types import MediaSource, MusicTargetEntityType

from app.schemas.context import MetaInfo, MediaInfo
from app.schemas.music import MusicInfo, MusicMeta
from app.schemas.file import FileItem


class DownloaderTorrent(BaseModel):
    """
    下载器任务信息
    """
    downloader: Optional[str] = None
    hash: Optional[str] = None
    title: Optional[str] = None
    site_name: Optional[str] = None
    name: Optional[str] = None
    year: Optional[str] = None
    season_episode: Optional[str] = None
    path: Optional[Path] = None
    size: Optional[float] = 0.0
    progress: Optional[float] = 0.0
    state: Optional[str] = 'downloading'
    upspeed: Optional[str] = None
    dlspeed: Optional[str] = None
    tags: Optional[str] = None
    save_path: Optional[str] = None
    content_path: Optional[str] = None
    category: Optional[str] = None
    download_limit: Optional[float] = None
    upload_limit: Optional[float] = None
    ratio_limit: Optional[float] = None
    seeding_time_limit: Optional[int] = None
    trackers: Optional[List[str]] = Field(default_factory=list)
    media: Optional["DownloadTaskMedia"] = None
    userid: Optional[str] = None
    username: Optional[str] = None
    left_time: Optional[str] = None


class DownloaderFile(BaseModel):
    """下载器文件项的宿主投影，隔离各 provider SDK 的对象差异。"""

    model_config = ConfigDict(from_attributes=True)

    id: Optional[Union[int, str]] = None
    name: str
    size: Optional[int] = None
    priority: Optional[int] = None
    progress: Optional[float] = None


class DownloadTaskMedia(OptionalMediaIdentityMixin, BaseModel):
    """下载任务关联的影视或音乐媒体摘要。"""

    type: Optional[str] = None
    title: Optional[str] = None
    season: Optional[list[int] | int | str] = None
    episode: Optional[list[int] | int | str] = None
    image: Optional[str] = None
    poster: Optional[str] = None
    backdrop: Optional[str] = None
    media_source: Optional[MediaSource] = None
    media_id: Optional[str] = None
    music_type: Optional[str] = None
    artists: list[str] = Field(default_factory=list)
    album: Optional[str] = None
    album_id: Optional[str] = None
    total_tracks: Optional[int] = None
    track_number: Optional[int] = None


class TransferTorrent(DownloaderTorrent):
    """
    待转移任务信息
    """


class DownloadingTorrent(DownloaderTorrent):
    """
    下载中任务信息
    """


# TransferTask 已迁至 app/application/transfer.py：它是整理链的进程内工作项，装的是
# 领域对象而非 DTO，留在这里只能把两个字段标成 Any——app.schemas 命名领域类型会让
# app.schemas -> app.schemas.transfer -> app.domain.* -> app.schemas.types -> app.schemas
# 闭环。下面的 TransferJob / TransferJobTask 才是它面向前端的投影，用本包的同名 DTO。


class TransferJobTask(BaseModel):
    """
    文件整理作业任务
    """
    fileitem: Optional[FileItem] = None
    meta: Optional[Union[MusicMeta, MetaInfo]] = None
    state: Optional[str] = None
    downloader: Optional[str] = None
    download_hash: Optional[str] = None


class TransferJob(BaseModel):
    """
    文件整理作业
    """
    media: Optional[Union[MusicInfo, MediaInfo]] = None
    season: Optional[int] = None
    tasks: Optional[List[TransferJobTask]] = Field(default_factory=list)


class TransferInfo(BaseModel):
    """
    文件整理结果
    """
    # 是否成功标志
    success: bool = True
    # 整理⼁路径
    fileitem: Optional[FileItem] = None
    # 转移后的目录项，媒体的根目录
    target_diritem: Optional[FileItem] = None
    # 转移后路径
    target_item: Optional[FileItem] = None
    # 整理方式
    transfer_type: Optional[str] = None
    # 处理文件数
    file_count: Optional[int] = Field(default=0)
    # 处理文件清单
    file_list: Optional[list] = Field(default_factory=list)
    # 目标文件清单
    file_list_new: Optional[list] = Field(default_factory=list)
    # 总文件大小
    total_size: Optional[int] = Field(default=0)
    # 失败清单
    fail_list: Optional[list] = Field(default_factory=list)
    # 错误信息
    message: Optional[str] = None
    # 是否需要刮削
    need_scrape: Optional[bool] = False
    # 是否需要通知
    need_notify: Optional[bool] = False
    # 是否因覆盖模式判定「不覆盖」而放弃整理。
    # 这是一次正常的策略裁决而非整理故障，调用方据此决定是否写失败历史与推送失败通知
    overwrite_skipped: Optional[bool] = False

    def to_dict(self):
        """
        返回字典
        """
        dicts = vars(self).copy()
        dicts["fileitem"] = self.fileitem.model_dump() if self.fileitem else None
        dicts["target_item"] = self.target_item.model_dump() if self.target_item else None
        return dicts


class EpisodeFormat(BaseModel):
    """
    剧集自定义识别格式
    """
    format: Optional[str] = None
    detail: Optional[str] = None
    part: Optional[str] = None
    offset: Optional[str] = None


class EpisodeFormatRule(BaseModel):
    """
    集数定位规则
    """
    name: str
    enabled: bool = True
    order: int = 0
    pattern: str
    min_file_size_mb: int = 0


class EpisodeFormatRecommendItem(BaseModel):
    """
    集数定位推荐请求
    """
    fileitem: Optional[FileItem] = None
    fileitems: Optional[List[FileItem]] = None


class ManualTransferItem(OptionalMediaIdentityMixin, BaseModel):
    """手动整理请求，媒体身份接受内置或插件来源与原生 ID。"""

    # 文件项
    fileitem: FileItem = None
    # 文件项列表（前端多选时传入）
    fileitems: Optional[List[FileItem]] = None
    # 日志ID
    logid: Optional[int] = None
    # 日志ID列表（前端多选历史记录时传入）
    logids: Optional[List[int]] = None
    # 目标存储
    target_storage: Optional[str] = None
    # 目标路径
    target_path: Optional[str] = None
    # 媒体数据源
    media_source: Optional[MediaSource] = None
    # 数据源原生ID
    media_id: Optional[str] = None
    # 音乐实体类型
    music_type: Optional[MusicTargetEntityType] = None
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
    scrape: Optional[bool] = False
    # 媒体库类型子目录
    library_type_folder: Optional[bool] = None
    # 媒体库类别子目录
    library_category_folder: Optional[bool] = None
    # 复用历史识别信息
    from_history: Optional[bool] = False
    # 剧集组
    episode_group: Optional[str] = None
    # 仅预览，不执行整理
    preview: Optional[bool] = False
    # 重新整理，清理命中的成功历史及其旧目标
    reorganize: Optional[bool] = False


class ManualTransferHistoryInfo(BaseModel):
    """
    手动整理命中的成功历史摘要
    """

    # 是否应显示重新整理操作
    reorganize: bool = False
    # 命中的成功历史数量
    history_count: int = 0


class ManualTransferTargetPath(BaseModel):
    """
    手动整理目的路径匹配结果
    """

    # 目标存储
    target_storage: Optional[str] = None
    # 目标路径
    target_path: Optional[str] = None
    # 整理方式
    transfer_type: Optional[str] = None
    # 刮削
    scrape: Optional[bool] = False
    # 媒体库类型子目录
    library_type_folder: Optional[bool] = False
    # 媒体库类别子目录
    library_category_folder: Optional[bool] = False


class ManualTransferPreviewSummary(BaseModel):
    """手动整理预览数量统计。"""

    total: int = 0
    success: int = 0
    failed: int = 0


class ManualTransferPreviewItem(BaseModel):
    """单个文件的手动整理预览。"""

    source: Optional[str] = None
    target: Optional[str] = None
    target_dir: Optional[str] = None
    success: bool = False
    message: Optional[str] = None
    type: Optional[str] = None
    title: Optional[str] = None
    season: Optional[int] = None
    episode: Optional[int] = None
    episode_end: Optional[int] = None
    part: Optional[int | str] = None
    org_string: Optional[str] = None
    apply_words: list[str] = Field(default_factory=list)
    resource_team: Optional[str] = None
    customization: Optional[str] = None


class ManualTransferResultData(BaseModel):
    """手动整理预览或执行结果数据。"""

    summary: Optional[ManualTransferPreviewSummary] = None
    items: list[ManualTransferPreviewItem] = Field(default_factory=list)
    message: Optional[str] = None


class EpisodeFormatRecommendData(BaseModel):
    """集数定位模板推荐结果。"""

    rule_name: str
    episode_format: str
    sample_file: str
    pattern: Optional[str] = None
    rule_index: Optional[int] = None
    min_file_size_mb: Optional[int] = None
    sample_count: Optional[int] = None
    majority_count: Optional[int] = None
    confidence: Optional[str] = None
    size_filter_relaxed: Optional[bool] = None
    native_verified_count: Optional[int] = None
    native_fallback_count: Optional[int] = None
    native_conflict_count: Optional[int] = None
    reason: Optional[str] = None
    reasons: list[str] = Field(default_factory=list)
    message: Optional[str] = None
