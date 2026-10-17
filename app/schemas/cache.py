"""
种子缓存 API 输出模型。
"""

from typing import Optional

from pydantic import BaseModel, Field

from app.schemas.types import MediaSource


class TorrentCacheItem(BaseModel):
    """
    单条站点种子缓存。
    """

    hash: str = Field(description="种子缓存的唯一摘要键")
    domain: str = Field(description="站点域名")
    title: Optional[str] = Field(default=None, description="标题")
    description: Optional[str] = Field(default=None, description="描述")
    size: Optional[int] = Field(default=None, description="文件大小，单位为字节")
    pubdate: Optional[str] = Field(default=None, description="发布时间")
    site_name: Optional[str] = Field(default=None, description="站点名称")
    media_name: Optional[str] = Field(default=None, description="识别出的媒体名称")
    media_year: Optional[str | int] = Field(default=None, description="识别出的发行年份")
    media_type: Optional[str] = Field(default=None, description="适用媒体类型")
    media_source: Optional[MediaSource] = Field(default=None, description="媒体数据源")
    media_id: Optional[str] = Field(default=None, description="数据源原生 ID")
    music_type: Optional[str] = Field(default=None, description="音乐实体类型：recording 单曲、album 专辑")
    season_episode: Optional[str] = Field(default=None, description="季集信息的展示文本")
    resource_term: Optional[str] = Field(default=None, description="资源规格的展示文本")
    enclosure: Optional[str] = Field(default=None, description="种子链接")
    page_url: Optional[str] = Field(default=None, description="详情页面")
    poster_path: Optional[str] = Field(default=None, description="海报地址")
    backdrop_path: Optional[str] = Field(default=None, description="背景图片")


class TorrentCacheData(BaseModel):
    """
    种子缓存统计及明细。
    """

    count: int = Field(default=0, description="条目数量")
    sites: int = Field(default=0, description="包含缓存条目的站点数量")
    data: list[TorrentCacheItem] = Field(default_factory=list, description="缓存种子明细列表")


class TorrentReidentifyData(BaseModel):
    """
    种子重新识别后的媒体身份。
    """

    media_name: Optional[str] = Field(default=None, description="识别出的媒体名称")
    media_year: Optional[str | int] = Field(default=None, description="识别出的发行年份")
    media_type: Optional[str] = Field(default=None, description="适用媒体类型")
    media_source: Optional[MediaSource] = Field(default=None, description="媒体数据源")
    media_id: Optional[str] = Field(default=None, description="数据源原生 ID")
    music_type: Optional[str] = Field(default=None, description="音乐实体类型：recording 单曲、album 专辑")
