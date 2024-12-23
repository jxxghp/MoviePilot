from typing import Optional
from pydantic import BaseModel, Field


class RadarrMovie(BaseModel):
    id: Optional[int]
    title: Optional[str]
    year: Optional[str]
    isAvailable: bool = False
    monitored: bool = False
    tmdbId: Optional[int]
    imdbId: Optional[str]
    titleSlug: Optional[str]
    folderName: Optional[str]
    path: Optional[str]
    profileId: Optional[int]
    qualityProfileId: Optional[int]
    added: Optional[str]
    hasFile: bool = False


class SonarrSeries(BaseModel):
    id: Optional[int]
    title: Optional[str]
    sortTitle: Optional[str]
    seasonCount: Optional[int]
    status: Optional[str]
    overview: Optional[str]
    network: Optional[str]
    airTime: Optional[str]
    images: list = Field(default_factory=list)
    remotePoster: Optional[str]
    seasons: list = Field(default_factory=list)
    year: Optional[str]
    path: Optional[str]
    profileId: Optional[int]
    languageProfileId: Optional[int]
    seasonFolder: bool = False
    monitored: bool = False
    useSceneNumbering: bool = False
    runtime: Optional[int]
    tmdbId: Optional[int]
    imdbId: Optional[str]
    tvdbId: Optional[int]
    tvRageId: Optional[int]
    tvMazeId: Optional[int]
    firstAired: Optional[str]
    seriesType: Optional[str]
    cleanTitle: Optional[str]
    titleSlug: Optional[str]
    certification: Optional[str]
    genres: list = Field(default_factory=list)
    tags: list = Field(default_factory=list)
    added: Optional[str]
    ratings: Optional[dict]
    qualityProfileId: Optional[int]
    statistics: dict = Field(default_factory=dict)
