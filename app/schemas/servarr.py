from typing import Optional
from pydantic import BaseModel, Field


class RadarrMovie(BaseModel):
    id: Optional[int] = None
    title: Optional[str] = None
    year: Optional[str] = None
    isAvailable: bool = False
    monitored: bool = False
    tmdbId: Optional[int] = None
    imdbId: Optional[str] = None
    titleSlug: Optional[str] = None
    folderName: Optional[str] = None
    path: Optional[str] = None
    profileId: Optional[int] = None
    qualityProfileId: Optional[int] = None
    added: Optional[str] = None
    hasFile: bool = False


class SonarrSeries(BaseModel):
    id: Optional[int] = None
    title: Optional[str] = None
    sortTitle: Optional[str] = None
    seasonCount: Optional[int] = None
    status: Optional[str] = None
    overview: Optional[str] = None
    network: Optional[str] = None
    airTime: Optional[str] = None
    images: list = Field(default_factory=list)
    remotePoster: Optional[str] = None
    seasons: list = Field(default_factory=list)
    year: Optional[str] = None
    path: Optional[str] = None
    profileId: Optional[int] = None
    languageProfileId: Optional[int] = None
    seasonFolder: bool = False
    monitored: bool = False
    useSceneNumbering: bool = False
    runtime: Optional[int] = None
    tmdbId: Optional[int] = None
    imdbId: Optional[str] = None
    tvdbId: Optional[int] = None
    tvRageId: Optional[int] = None
    tvMazeId: Optional[int] = None
    firstAired: Optional[str] = None
    seriesType: Optional[str] = None
    cleanTitle: Optional[str] = None
    titleSlug: Optional[str] = None
    certification: Optional[str] = None
    genres: list = Field(default_factory=list)
    tags: list = Field(default_factory=list)
    added: Optional[str] = None
    ratings: Optional[dict] = None
    qualityProfileId: Optional[int] = None
    statistics: dict = Field(default_factory=dict)
    isAvailable: Optional[bool] = False
    hasFile: Optional[bool] = False
