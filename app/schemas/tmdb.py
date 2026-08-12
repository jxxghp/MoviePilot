from typing import Optional

from pydantic import BaseModel, Field


class TmdbSeason(BaseModel):
    """
    TMDB季信息
    """
    air_date: Optional[str] = None
    episode_count: Optional[int] = None
    name: Optional[str] = None
    overview: Optional[str] = None
    poster_path: Optional[str] = None
    season_number: Optional[int] = None
    vote_average: Optional[float] = None


class TmdbEpisodeCredit(BaseModel):
    """TMDB 剧集演职人员的公共信息。"""

    adult: Optional[bool] = None
    gender: Optional[int] = None
    id: Optional[int] = None
    known_for_department: Optional[str] = None
    name: Optional[str] = None
    original_name: Optional[str] = None
    popularity: Optional[float] = None
    profile_path: Optional[str] = None
    credit_id: Optional[str] = None


class TmdbEpisodeCrew(TmdbEpisodeCredit):
    """TMDB 剧集幕后人员信息。"""

    department: Optional[str] = None
    job: Optional[str] = None


class TmdbEpisodeGuestStar(TmdbEpisodeCredit):
    """TMDB 剧集客串演员信息。"""

    character: Optional[str] = None
    order: Optional[int] = None


class TmdbEpisode(BaseModel):
    """
    TMDB集信息
    """
    air_date: Optional[str] = None
    episode_number: Optional[int] = None
    episode_type: Optional[str] = None
    name: Optional[str] = None
    overview: Optional[str] = None
    runtime: Optional[int] = None
    season_number: Optional[int] = None
    still_path: Optional[str] = None
    vote_average: Optional[float] = None
    crew: Optional[list[TmdbEpisodeCrew]] = Field(default_factory=list)
    guest_stars: Optional[list[TmdbEpisodeGuestStar]] = Field(default_factory=list)


class TmdbRecognitionCacheItem(BaseModel):
    """单条 TMDB 识别缓存。"""

    key: str
    tmdb_id: int = 0
    title: str = ""
    year: str | int = ""
    media_type: str = "unknown"
    poster_path: str = ""
    backdrop_path: str = ""


class TmdbRecognitionCacheData(BaseModel):
    """TMDB 识别缓存统计及明细。"""

    count: int = 0
    recognized: int = 0
    unrecognized: int = 0
    shared_recognized: int = 0
    shared_recognize_enabled: bool = False
    data: list[TmdbRecognitionCacheItem] = Field(default_factory=list)
