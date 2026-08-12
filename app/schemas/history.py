from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import JsonData
from app.schemas.media import OptionalMediaIdentityMixin
from app.schemas.types import MediaSource


class DownloadHistory(OptionalMediaIdentityMixin, BaseModel):
    """
    下载历史记录
    """

    # ID
    id: int
    # 保存路程
    path: Optional[str] = None
    # 类型：电影、电视剧、音乐
    type: Optional[str] = None
    # 标题
    title: Optional[str] = None
    # 年份
    year: Optional[str] = None
    # 媒体数据源
    media_source: Optional[MediaSource] = None
    # 数据源原生ID
    media_id: Optional[str] = None
    # 音乐实体类型：recording 单曲、album 专辑
    music_type: Optional[str] = None
    # 季Sxx
    seasons: Optional[str] = None
    # 集Exx
    episodes: Optional[str] = None
    # 背景图
    image: Optional[str] = None
    # 海报
    poster: Optional[str] = None
    # 下载器Hash
    download_hash: Optional[str] = None
    # 种子名称
    torrent_name: Optional[str] = None
    # 种子描述
    torrent_description: Optional[str] = None
    # 站点
    torrent_site: Optional[str] = None
    # 下载用户
    userid: Optional[str] = None
    # 下载用户名
    username: Optional[str] = None
    # 下载渠道
    channel: Optional[str] = None
    # 创建时间
    date: Optional[str] = None
    # 备注
    note: Optional[JsonData] = None
    # 自定义媒体类别
    media_category: Optional[str] = None
    # 自定义剧集组
    episode_group: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class TransferHistory(OptionalMediaIdentityMixin, BaseModel):
    """
    文件整理历史记录
    """

    # ID
    id: int
    # 源存储类型
    src_storage: Optional[str] = None
    # 目标存储类型
    dest_storage: Optional[str] = None
    # 源目录
    src: Optional[str] = None
    # 目的目录
    dest: Optional[str] = None
    # 转移模式
    mode: Optional[str] = None
    # 类型：电影、电视剧、音乐
    type: Optional[str] = None
    # 二级分类
    category: Optional[str] = None
    # 标题
    title: Optional[str] = None
    # 年份
    year: Optional[str] = None
    # 媒体数据源
    media_source: Optional[MediaSource] = None
    # 数据源原生ID
    media_id: Optional[str] = None
    # 音乐实体类型：recording 单曲、album 专辑
    music_type: Optional[str] = None
    # 专辑预期总曲目数
    total_tracks: Optional[int] = None
    # 实际音频格式
    audio_format: Optional[str] = None
    # 是否无损音频
    audio_lossless: Optional[bool] = None
    # 实际位深（bit）
    bit_depth: Optional[int] = None
    # 实际采样率（Hz）
    sample_rate: Optional[int] = None
    # 实际码率（bps）
    bitrate: Optional[int] = None
    # 季Sxx
    seasons: Optional[str] = None
    # 集Exx
    episodes: Optional[str] = None
    # 海报
    image: Optional[str] = None
    # 下载器Hash
    download_hash: Optional[str] = None
    # 自定义剧集组
    episode_group: Optional[str] = None
    # 状态 1-成功，0-失败
    status: bool = True
    # 失败原因
    errmsg: Optional[str] = None
    # 日期
    date: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class BatchTransferHistoryRedoRequest(BaseModel):
    """批量重新整理历史请求。"""

    history_ids: list[int] = Field(default_factory=list)


class TransferHistoryPage(BaseModel):
    """整理历史分页数据。"""

    list: List[TransferHistory] = Field(default_factory=list, description="整理历史列表")
    total: int = Field(default=0, description="记录总数")
