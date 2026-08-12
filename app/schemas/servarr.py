from typing import Optional

from pydantic import BaseModel, Field


class ServarrVersion(BaseModel):
    """Servarr 兼容接口使用的版本号结构。"""

    major: int = 0
    minor: int = 0
    build: int = 0
    revision: int = 0
    majorRevision: int = 0
    minorRevision: int = 0


class ServarrSystemStatus(BaseModel):
    """Servarr 系统状态响应。"""

    appName: str
    instanceName: str
    version: str
    buildTime: str
    isDebug: bool
    isProduction: bool
    isAdmin: bool
    isUserInteractive: bool
    startupPath: str
    appData: str
    osName: str
    osVersion: str
    isNetCore: bool
    isLinux: bool
    isOsx: bool
    isWindows: bool
    isDocker: bool
    mode: str
    branch: str
    databaseType: str
    databaseVersion: ServarrVersion
    authentication: str
    migrationVersion: int
    urlBase: str
    runtimeVersion: ServarrVersion
    runtimeName: str
    startTime: str
    packageVersion: str
    packageAuthor: str
    packageUpdateMechanism: str
    packageUpdateMechanismMessage: str


class ServarrQuality(BaseModel):
    """Servarr 质量定义。"""

    id: int
    name: str
    source: str
    resolution: int


class ServarrQualityProfileItem(BaseModel):
    """Servarr 质量配置中的可选质量项。"""

    id: int
    name: str
    quality: ServarrQuality
    items: list[str] = Field(default_factory=list)
    allowed: bool


class ServarrFormatItem(BaseModel):
    """Servarr 自定义格式评分项。"""

    id: int
    format: int
    name: str
    score: int


class ServarrQualityProfile(BaseModel):
    """Servarr 质量配置响应项。"""

    id: int
    name: str
    upgradeAllowed: bool
    cutoff: int
    items: list[ServarrQualityProfileItem] = Field(default_factory=list)
    minFormatScore: int
    cutoffFormatScore: int
    formatItems: list[ServarrFormatItem] = Field(default_factory=list)


class ServarrRootFolder(BaseModel):
    """Servarr 根目录响应项。"""

    id: int
    path: str
    accessible: bool
    freeSpace: int
    unmappedFolders: list[str] = Field(default_factory=list)


class ServarrTag(BaseModel):
    """Servarr 标签响应项。"""

    id: int
    label: str


class ServarrLanguage(BaseModel):
    """Servarr 语言定义。"""

    id: int
    name: str


class ServarrLanguageProfileItem(BaseModel):
    """Servarr 语言配置中的可选语言项。"""

    id: int
    language: ServarrLanguage
    allowed: bool


class ServarrLanguageProfile(BaseModel):
    """Servarr 语言配置响应项。"""

    id: int
    name: str
    upgradeAllowed: bool
    cutoff: ServarrLanguage
    languages: list[ServarrLanguageProfileItem] = Field(default_factory=list)


class ServarrIdResponse(BaseModel):
    """Servarr 新增资源后返回的资源标识。"""

    id: int


class ServarrImage(BaseModel):
    """Servarr 媒体图片。"""

    coverType: Optional[str] = None
    url: Optional[str] = None
    remoteUrl: Optional[str] = None


class SonarrStatistics(BaseModel):
    """Sonarr 剧集或季度统计信息。"""

    seasonCount: Optional[int] = None
    episodeFileCount: Optional[int] = None
    episodeCount: Optional[int] = None
    totalEpisodeCount: Optional[int] = None
    sizeOnDisk: Optional[int] = None
    releaseGroups: list[str] = Field(default_factory=list)
    percentOfEpisodes: Optional[float] = None
    nextAiring: Optional[str] = None
    previousAiring: Optional[str] = None


class SonarrSeason(BaseModel):
    """Sonarr 季度监控信息。"""

    seasonNumber: Optional[int] = None
    monitored: bool = False
    statistics: Optional[SonarrStatistics] = None
    images: list[ServarrImage] = Field(default_factory=list)


class SonarrRatings(BaseModel):
    """Sonarr 剧集评分信息。"""

    votes: Optional[int] = None
    value: Optional[float] = None


class RadarrMovie(BaseModel):
    """Radarr 兼容接口的电影结构。"""

    id: Optional[int] = None
    title: Optional[str] = None
    year: Optional[str | int] = None
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
    """Sonarr 兼容接口的剧集结构。"""

    id: Optional[int] = None
    title: Optional[str] = None
    sortTitle: Optional[str] = None
    seasonCount: Optional[int] = None
    status: Optional[str] = None
    overview: Optional[str] = None
    network: Optional[str] = None
    airTime: Optional[str] = None
    images: list[ServarrImage] = Field(default_factory=list)
    remotePoster: Optional[str] = None
    seasons: list[SonarrSeason] = Field(default_factory=list)
    year: Optional[str | int] = None
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
    genres: list[str] = Field(default_factory=list)
    tags: list[int] = Field(default_factory=list)
    added: Optional[str] = None
    ratings: Optional[SonarrRatings] = None
    qualityProfileId: Optional[int] = None
    statistics: SonarrStatistics = Field(default_factory=SonarrStatistics)
    isAvailable: Optional[bool] = False
    hasFile: Optional[bool] = False
