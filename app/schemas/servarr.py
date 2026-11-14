"""
Radarr 和 Sonarr 兼容接口的配置、媒体及系统状态结构。
"""

from typing import Optional

from pydantic import BaseModel, Field


class ServarrVersion(BaseModel):
    """
    Servarr 兼容接口使用的版本号结构。
    """

    major: int = Field(default=0, description="主版本号")
    minor: int = Field(default=0, description="次版本号")
    build: int = Field(default=0, description="版本中的构建号")
    revision: int = Field(default=0, description="版本修订号")
    majorRevision: int = Field(default=0, description="主修订号")
    minorRevision: int = Field(default=0, description="次修订号")


class ServarrSystemStatus(BaseModel):
    """
    Servarr 系统状态响应。
    """

    appName: str = Field(description="应用名称")
    instanceName: str = Field(description="服务实例名称")
    version: str = Field(description="版本号")
    buildTime: str = Field(description="应用构建时间")
    isDebug: bool = Field(description="是否为调试构建")
    isProduction: bool = Field(description="是否为生产构建")
    isAdmin: bool = Field(description="服务进程是否具有管理员权限")
    isUserInteractive: bool = Field(description="是否处于用户交互会话")
    startupPath: str = Field(description="应用启动目录")
    appData: str = Field(description="应用数据保存目录")
    osName: str = Field(description="操作系统名称")
    osVersion: str = Field(description="操作系统版本")
    isNetCore: bool = Field(description="是否使用 .NET Core 运行时")
    isLinux: bool = Field(description="是否运行于 Linux")
    isOsx: bool = Field(description="是否运行于 macOS")
    isWindows: bool = Field(description="是否运行于 Windows")
    isDocker: bool = Field(description="是否运行于容器内")
    mode: str = Field(description="操作模式")
    branch: str = Field(description="应用构建所用分支")
    databaseType: str = Field(description="数据库类型")
    databaseVersion: ServarrVersion = Field(description="数据库版本")
    authentication: str = Field(description="接口认证方式")
    migrationVersion: int = Field(description="数据库迁移版本")
    urlBase: str = Field(description="服务接口的路径前缀")
    runtimeVersion: ServarrVersion = Field(description="运行时版本")
    runtimeName: str = Field(description="运行时名称")
    startTime: str = Field(description="服务启动时间")
    packageVersion: str = Field(description="软件包版本")
    packageAuthor: str = Field(description="软件包发布者")
    packageUpdateMechanism: str = Field(description="软件包更新机制")
    packageUpdateMechanismMessage: str = Field(description="软件包更新机制的补充说明")


class ServarrQuality(BaseModel):
    """
    Servarr 质量定义。
    """

    id: int = Field(description="记录标识")
    name: str = Field(description="显示名称")
    source: str = Field(description="资源来源类型，例如蓝光或网络发行")
    resolution: int = Field(description="分辨率")


class ServarrQualityProfileItem(BaseModel):
    """
    Servarr 质量配置中的可选质量项。
    """

    id: int = Field(description="记录标识")
    name: str = Field(description="显示名称")
    quality: ServarrQuality = Field(description="质量")
    items: list[str] = Field(default_factory=list, description="该质量分组包含的子项")
    allowed: bool = Field(description="是否允许采用此质量项")


class ServarrFormatItem(BaseModel):
    """
    Servarr 自定义格式评分项。
    """

    id: int = Field(description="记录标识")
    format: int = Field(description="自定义格式标识")
    name: str = Field(description="显示名称")
    score: int = Field(description="自定义格式得分")


class ServarrQualityProfile(BaseModel):
    """
    Servarr 质量配置响应项。
    """

    id: int = Field(description="记录标识")
    name: str = Field(description="显示名称")
    upgradeAllowed: bool = Field(description="是否允许自动升级质量或语言")
    cutoff: int = Field(description="达到此质量或语言后停止升级")
    items: list[ServarrQualityProfileItem] = Field(default_factory=list, description="按优先级排列的质量配置项")
    minFormatScore: int = Field(description="允许的最低格式评分")
    cutoffFormatScore: int = Field(description="停止升级所需的格式评分")
    formatItems: list[ServarrFormatItem] = Field(default_factory=list, description="自定义格式评分项")


class ServarrRootFolder(BaseModel):
    """
    Servarr 根目录响应项。
    """

    id: int = Field(description="记录标识")
    path: str = Field(description="文件或目录路径")
    accessible: bool = Field(description="根目录是否可以访问")
    freeSpace: int = Field(description="根目录的可用字节数")
    unmappedFolders: list[str] = Field(default_factory=list, description="尚未关联媒体的目录列表")


class ServarrTag(BaseModel):
    """
    Servarr 标签响应项。
    """

    id: int = Field(description="记录标识")
    label: str = Field(description="显示标签")


class ServarrLanguage(BaseModel):
    """
    Servarr 语言定义。
    """

    id: int = Field(description="记录标识")
    name: str = Field(description="显示名称")


class ServarrLanguageProfileItem(BaseModel):
    """
    Servarr 语言配置中的可选语言项。
    """

    id: int = Field(description="记录标识")
    language: ServarrLanguage = Field(description="语言定义")
    allowed: bool = Field(description="是否允许采用此语言项")


class ServarrLanguageProfile(BaseModel):
    """
    Servarr 语言配置响应项。
    """

    id: int = Field(description="记录标识")
    name: str = Field(description="显示名称")
    upgradeAllowed: bool = Field(description="是否允许自动升级质量或语言")
    cutoff: ServarrLanguage = Field(description="达到此质量或语言后停止升级")
    languages: list[ServarrLanguageProfileItem] = Field(default_factory=list, description="可选语言列表")


class ServarrIdResponse(BaseModel):
    """
    Servarr 新增资源后返回的资源标识。
    """

    id: int = Field(description="记录标识")


class ServarrImage(BaseModel):
    """
    Servarr 媒体图片。
    """

    coverType: Optional[str] = Field(default=None, description="图片用途，例如海报或背景图")
    url: Optional[str] = Field(default=None, description="访问地址")
    remoteUrl: Optional[str] = Field(default=None, description="远程图片地址")


class SonarrStatistics(BaseModel):
    """
    Sonarr 剧集或季度统计信息。
    """

    seasonCount: Optional[int] = Field(default=None, description="季数量")
    episodeFileCount: Optional[int] = Field(default=None, description="已有文件的剧集数量")
    episodeCount: Optional[int] = Field(default=None, description="剧集数量")
    totalEpisodeCount: Optional[int] = Field(default=None, description="剧集总集数")
    sizeOnDisk: Optional[int] = Field(default=None, description="媒体文件占用的总字节数")
    releaseGroups: list[str] = Field(default_factory=list, description="资源发布组列表")
    percentOfEpisodes: Optional[float] = Field(default=None, description="已有文件的剧集占比")
    nextAiring: Optional[str] = Field(default=None, description="下一集播出时间")
    previousAiring: Optional[str] = Field(default=None, description="上一集播出时间")


class SonarrSeason(BaseModel):
    """
    Sonarr 季度监控信息。
    """

    seasonNumber: Optional[int] = Field(default=None, description="季号")
    monitored: bool = Field(default=False, description="是否已开启监控")
    statistics: Optional[SonarrStatistics] = Field(default=None, description="剧集文件与播出统计")
    images: list[ServarrImage] = Field(default_factory=list, description="本季的图片列表")


class SonarrRatings(BaseModel):
    """
    Sonarr 剧集评分信息。
    """

    votes: Optional[int] = Field(default=None, description="评分投票人数")
    value: Optional[float] = Field(default=None, description="剧集评分值")


class RadarrMovie(BaseModel):
    """
    Radarr 兼容接口的电影结构。
    """

    id: Optional[int] = Field(default=None, description="记录标识")
    title: Optional[str] = Field(default=None, description="标题")
    year: Optional[str | int] = Field(default=None, description="媒体发行年份")
    isAvailable: bool = Field(default=False, description="媒体资源是否可用")
    monitored: bool = Field(default=False, description="是否已开启监控")
    tmdbId: Optional[int] = Field(default=None, description="TMDB 条目标识")
    imdbId: Optional[str] = Field(default=None, description="IMDb 条目标识")
    titleSlug: Optional[str] = Field(default=None, description="标题的地址友好标识")
    folderName: Optional[str] = Field(default=None, description="媒体目录名称")
    path: Optional[str] = Field(default=None, description="文件或目录路径")
    profileId: Optional[int] = Field(default=None, description="兼容接口使用的配置标识")
    qualityProfileId: Optional[int] = Field(default=None, description="质量配置标识")
    added: Optional[str] = Field(default=None, description="加入媒体库的时间")
    hasFile: bool = Field(default=False, description="是否已有媒体文件")


class SonarrSeries(BaseModel):
    """
    Sonarr 兼容接口的剧集结构。
    """

    id: Optional[int] = Field(default=None, description="记录标识")
    title: Optional[str] = Field(default=None, description="标题")
    sortTitle: Optional[str] = Field(default=None, description="排序使用的剧集标题")
    seasonCount: Optional[int] = Field(default=None, description="季数量")
    status: Optional[str] = Field(default=None, description="剧集播出状态")
    overview: Optional[str] = Field(default=None, description="简介")
    network: Optional[str] = Field(default=None, description="播出平台信息")
    airTime: Optional[str] = Field(default=None, description="剧集日常播出时间")
    images: list[ServarrImage] = Field(default_factory=list, description="剧集的图片列表")
    remotePoster: Optional[str] = Field(default=None, description="远程海报地址")
    seasons: list[SonarrSeason] = Field(default_factory=list, description="季季集清单")
    year: Optional[str | int] = Field(default=None, description="媒体发行年份")
    path: Optional[str] = Field(default=None, description="文件或目录路径")
    profileId: Optional[int] = Field(default=None, description="兼容接口使用的配置标识")
    languageProfileId: Optional[int] = Field(default=None, description="语言配置标识")
    seasonFolder: bool = Field(default=False, description="是否按季创建目录")
    monitored: bool = Field(default=False, description="是否已开启监控")
    useSceneNumbering: bool = Field(default=False, description="是否采用发布组集号")
    runtime: Optional[int] = Field(default=None, description="单集时长，单位为分钟")
    tmdbId: Optional[int] = Field(default=None, description="TMDB 条目标识")
    imdbId: Optional[str] = Field(default=None, description="IMDb 条目标识")
    tvdbId: Optional[int] = Field(default=None, description="TVDB 条目标识")
    tvRageId: Optional[int] = Field(default=None, description="TVRage 条目标识")
    tvMazeId: Optional[int] = Field(default=None, description="TVMaze 条目标识")
    firstAired: Optional[str] = Field(default=None, description="剧集首次播出时间")
    seriesType: Optional[str] = Field(default=None, description="剧集类型")
    cleanTitle: Optional[str] = Field(default=None, description="规范化后的剧集标题")
    titleSlug: Optional[str] = Field(default=None, description="标题的地址友好标识")
    certification: Optional[str] = Field(default=None, description="媒体内容分级")
    genres: list[str] = Field(default_factory=list, description="流派")
    tags: list[int] = Field(default_factory=list, description="标签列表")
    added: Optional[str] = Field(default=None, description="加入媒体库的时间")
    ratings: Optional[SonarrRatings] = Field(default=None, description="剧集评分信息")
    qualityProfileId: Optional[int] = Field(default=None, description="质量配置标识")
    statistics: SonarrStatistics = Field(default_factory=SonarrStatistics, description="剧集文件与播出统计")
    isAvailable: Optional[bool] = Field(default=False, description="媒体资源是否可用")
    hasFile: Optional[bool] = Field(default=False, description="是否已有媒体文件")
