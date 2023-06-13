from typing import Optional
from pydantic import BaseModel


class RadarrMovie(BaseModel):
    id: Optional[int]
    title: str
    year: Optional[int]
    isAvailable: bool
    monitored: bool
    tmdbId: Optional[int]
    imdbId: Optional[str]
    titleSlug: Optional[str]
    folderName: Optional[str]
    path: Optional[str]
    profileId: Optional[int]
    qualityProfileId: Optional[int]
    added: Optional[str]
    hasFile: bool


class SonarrSeries(BaseModel):
    id: Optional[int]
    title: str
    sortTitle: Optional[str]
    seasonCount: Optional[int]
    status: Optional[str]
    overview: Optional[str]
    network: Optional[str]
    airTime: Optional[str]
    images: Optional[list]
    remotePoster: Optional[str]
    seasons: Optional[list]
    year: Optional[int]
    path: Optional[str]
    profileId: Optional[int]
    languageProfileId: Optional[int]
    seasonFolder: Optional[bool]
    monitored: Optional[bool]
    useSceneNumbering: Optional[bool]
    runtime: Optional[int]
    tvdbId: Optional[int]
    tvRageId: Optional[int]
    tvMazeId: Optional[int]
    firstAired: Optional[str]
    seriesType: Optional[str]
    cleanTitle: Optional[str]
    imdbId: Optional[str]
    titleSlug: Optional[str]
    certification: Optional[str]
    genres: Optional[list]
    tags: Optional[list]
    added: Optional[str]
    ratings: Optional[dict]
    qualityProfileId: Optional[int]
    statistics: Optional[dict]
