"""Agent 推荐接口的稳定结果模型。"""

from typing import Optional

from pydantic import BaseModel, Field


class AgentRecommendationItem(BaseModel):
    """影视、动画与音乐推荐共用的有界字段投影。"""

    title: Optional[str] = Field(default=None, description="标题")
    en_title: Optional[str] = Field(default=None, description="英文标题")
    year: Optional[int | str] = Field(default=None, description="年份")
    type: Optional[str] = Field(default=None, description="媒体类型")
    season: Optional[int | str] = Field(default=None, description="季号")
    tmdb_id: Optional[int | str] = Field(default=None, description="TMDB ID")
    imdb_id: Optional[str] = Field(default=None, description="IMDb ID")
    douban_id: Optional[int | str] = Field(default=None, description="豆瓣 ID")
    bangumi_id: Optional[int | str] = Field(default=None, description="Bangumi ID")
    anilist_id: Optional[int | str] = Field(default=None, description="AniList ID")
    media_source: Optional[str] = Field(default=None, description="媒体来源")
    media_id: Optional[str] = Field(default=None, description="来源内媒体 ID")
    vote_average: Optional[float] = Field(default=None, description="评分")
    poster_path: Optional[str] = Field(default=None, description="海报地址")
    detail_link: Optional[str] = Field(default=None, description="详情地址")
    music_type: Optional[str] = Field(default=None, description="音乐实体类型")
    artists: list[str] = Field(default_factory=list, description="艺术家名称")
    artist_ids: list[str] = Field(default_factory=list, description="艺术家 ID")
    artist: Optional[str] = Field(default=None, description="主艺术家")
    album: Optional[str] = Field(default=None, description="专辑名称")
    album_id: Optional[str] = Field(default=None, description="专辑 ID")
    album_type: Optional[str] = Field(default=None, description="专辑类型")
    release_date: Optional[str] = Field(default=None, description="发行日期")
    disc_number: Optional[int] = Field(default=None, description="碟号")
    track_number: Optional[int] = Field(default=None, description="曲目号")
    total_tracks: Optional[int] = Field(default=None, description="总曲目数")
    duration: Optional[int | float] = Field(default=None, description="时长")
    isrc: Optional[str] = Field(default=None, description="ISRC")
    version: Optional[str] = Field(default=None, description="版本说明")
    genres: list[str] = Field(default_factory=list, description="流派")
    category: Optional[str] = Field(default=None, description="分类")
    listen_count: Optional[int] = Field(default=None, description="收听次数")
    overview: Optional[str] = Field(default=None, description="简介")
