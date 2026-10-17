"""
TMDB 季集信息、演职人员与识别缓存的数据结构。
"""

from typing import Optional

from pydantic import BaseModel, Field


class TmdbSeason(BaseModel):
    """
    TMDB季信息。
    """
    air_date: Optional[str] = Field(default=None, description="首次播出日期")
    episode_count: Optional[int] = Field(default=None, description="集数量")
    name: Optional[str] = Field(default=None, description="显示名称")
    overview: Optional[str] = Field(default=None, description="简介")
    poster_path: Optional[str] = Field(default=None, description="海报地址")
    season_number: Optional[int] = Field(default=None, description="季号")
    vote_average: Optional[float] = Field(default=None, description="评分")


class TmdbEpisodeCredit(BaseModel):
    """
    TMDB 剧集演职人员的公共信息。
    """

    adult: Optional[bool] = Field(default=None, description="是否成人内容")
    gender: Optional[int] = Field(default=None, description="人物或艺人的性别信息")
    id: Optional[int] = Field(default=None, description="记录标识")
    known_for_department: Optional[str] = Field(default=None, description="人物主要从事的部门")
    name: Optional[str] = Field(default=None, description="显示名称")
    original_name: Optional[str] = Field(default=None, description="原名")
    popularity: Optional[float] = Field(default=None, description="流行度")
    profile_path: Optional[str] = Field(default=None, description="人物头像路径")
    credit_id: Optional[str] = Field(default=None, description="演职人员与作品之间的关联标识")


class TmdbEpisodeCrew(TmdbEpisodeCredit):
    """
    TMDB 剧集幕后人员信息。
    """

    department: Optional[str] = Field(default=None, description="演职人员所属部门")
    job: Optional[str] = Field(default=None, description="演职人员在作品中的具体职务")


class TmdbEpisodeGuestStar(TmdbEpisodeCredit):
    """
    TMDB 剧集客串演员信息。
    """

    character: Optional[str] = Field(default=None, description="饰演的角色名称")
    order: Optional[int] = Field(default=None, description="展示顺序")


class TmdbEpisode(BaseModel):
    """
    TMDB集信息。
    """
    air_date: Optional[str] = Field(default=None, description="首次播出日期")
    episode_number: Optional[int] = Field(default=None, description="集号")
    episode_type: Optional[str] = Field(default=None, description="单集类型")
    name: Optional[str] = Field(default=None, description="显示名称")
    overview: Optional[str] = Field(default=None, description="简介")
    runtime: Optional[int] = Field(default=None, description="单集时长，单位为分钟")
    season_number: Optional[int] = Field(default=None, description="季号")
    still_path: Optional[str] = Field(default=None, description="单集剧照路径")
    vote_average: Optional[float] = Field(default=None, description="评分")
    crew: Optional[list[TmdbEpisodeCrew]] = Field(default_factory=list, description="幕后演职人员列表")
    guest_stars: Optional[list[TmdbEpisodeGuestStar]] = Field(default_factory=list, description="本集客串演员列表")


class TmdbRecognitionCacheItem(BaseModel):
    """
    单条 TMDB 识别缓存。
    """

    key: str = Field(description="缓存条目的键")
    tmdb_id: int = Field(default=0, description="数据源返回的辅助 ID，仅作为元数据输出")
    title: str = Field(default="", description="标题")
    year: str | int = Field(default="", description="媒体发行年份")
    media_type: str = Field(default="unknown", description="适用媒体类型")
    poster_path: str = Field(default="", description="海报地址")
    backdrop_path: str = Field(default="", description="背景图片")


class TmdbRecognitionCacheData(BaseModel):
    """
    TMDB 识别缓存统计及明细。
    """

    count: int = Field(default=0, description="识别缓存条目总数")
    recognized: int = Field(default=0, description="已成功识别的缓存条目数量")
    unrecognized: int = Field(default=0, description="尚未识别的缓存条目数量")
    shared_recognized: int = Field(default=0, description="通过共享缓存识别的条目数量")
    shared_recognize_enabled: bool = Field(default=False, description="是否启用共享识别缓存")
    data: list[TmdbRecognitionCacheItem] = Field(default_factory=list, description="识别缓存条目列表")
