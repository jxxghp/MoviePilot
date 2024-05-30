
from pydantic import BaseModel


class TmdbSeason(BaseModel):
    """
    TMDB季信息
    """
    air_date: str | None = None
    episode_count: int | None = None
    name: str | None = None
    overview: str | None = None
    poster_path: str | None = None
    season_number: int | None = None
    vote_average: float | None = None


class TmdbEpisode(BaseModel):
    """
    TMDB集信息
    """
    air_date: str | None = None
    episode_number: int | None = None
    name: str | None = None
    overview: str | None = None
    runtime: int | None = None
    season_number: int | None = None
    still_path: str | None = None
    vote_average: float | None = None
    crew: list | None = []
    guest_stars: list | None = []
