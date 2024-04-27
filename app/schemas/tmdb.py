from typing import Optional

from pydantic import BaseModel


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


class TmdbEpisode(BaseModel):
    """
    TMDB集信息
    """
    air_date: Optional[str] = None
    episode_number: Optional[int] = None
    name: Optional[str] = None
    overview: Optional[str] = None
    runtime: Optional[int] = None
    season_number: Optional[int] = None
    still_path: Optional[str] = None
    vote_average: Optional[float] = None
    crew: Optional[list] = []
    guest_stars: Optional[list] = []
