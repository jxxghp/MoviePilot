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


class TmdbPerson(BaseModel):
    id: Optional[int] = None
    name: Optional[str] = None
    character: Optional[str] = None
    profile_path: Optional[str] = None
    gender: Optional[int] = None
    original_name: Optional[str] = None
    credit_id: Optional[str] = None
    also_known_as: Optional[list] = []
    birthday: Optional[str] = None
    deathday: Optional[str] = None
    imdb_id: Optional[str] = None
    known_for_department: Optional[str] = None
    place_of_birth: Optional[str] = None
    popularity: Optional[float] = None
    images: Optional[dict] = {}
    biography: Optional[str] = None


class DoubanPerson(BaseModel):
    id: Optional[str] = None
    name: Optional[str] = None
    roles: Optional[list] = []
    title: Optional[str] = None
    url: Optional[str] = None
    character: Optional[str] = None
    avatar: Optional[dict] = None
    latin_name: Optional[str] = None
