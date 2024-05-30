
from pydantic import BaseModel


class RadarrMovie(BaseModel):
    id: int | None
    title: str | None
    year: str | None
    isAvailable: bool = False
    monitored: bool = False
    tmdbId: int | None
    imdbId: str | None
    titleSlug: str | None
    folderName: str | None
    path: str | None
    profileId: int | None
    qualityProfileId: int | None
    added: str | None
    hasFile: bool = False


class SonarrSeries(BaseModel):
    id: int | None
    title: str | None
    sortTitle: str | None
    seasonCount: int | None
    status: str | None
    overview: str | None
    network: str | None
    airTime: str | None
    images: list = []
    remotePoster: str | None
    seasons: list = []
    year: str | None
    path: str | None
    profileId: int | None
    languageProfileId: int | None
    seasonFolder: bool = False
    monitored: bool = False
    useSceneNumbering: bool = False
    runtime: int | None
    tmdbId: int | None
    imdbId: str | None
    tvdbId: int | None
    tvRageId: int | None
    tvMazeId: int | None
    firstAired: str | None
    seriesType: str | None
    cleanTitle: str | None
    titleSlug: str | None
    certification: str | None
    genres: list = []
    tags: list = []
    added: str | None
    ratings: dict | None
    qualityProfileId: int | None
    statistics: dict = {}
