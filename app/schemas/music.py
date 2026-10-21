"""
音乐元数据、曲目、发行版及整理预览的数据结构。
"""

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas.category import ClassificationFactValue, ClassificationResult
from app.schemas.common import JsonData
from app.schemas.media import OptionalMediaIdentityMixin, RequiredMediaIdentityMixin
from app.schemas.types import MediaSource, MusicEntityType, MusicTargetEntityType


class MusicMeta(OptionalMediaIdentityMixin, BaseModel):
    """
    音乐名称及音频文件解析结果。
    """

    type: Literal["音乐"] = Field(default="音乐", description="类型标识")
    music_type: Optional[MusicEntityType] = Field(default=None, description="已知主身份的实体类型，未知时不推断")
    album_type: Optional[str] = Field(default=None, description="标签声明的发行主类型，例如 Album、EP、Single")
    secondary_types: list[str] = Field(default_factory=list, description="标签声明的 Compilation、Soundtrack 等副类型")
    music_layout: Optional[str] = Field(default=None, description="音频资源形态，例如整轨 image_cue 或分轨 tracks_cue")
    cue_filename: Optional[str] = Field(default=None, description="同目录关联的 CUE 文件名")
    cue_tracks: list[dict[str, JsonData]] = Field(default_factory=list, description="CUE 逻辑曲目，INDEX 起点单位为 75 帧每秒")
    organization_error: Optional[str] = Field(default=None, description="阻止自动整理的资源结构错误")
    org_string: Optional[str] = Field(default=None, description="原字符串")
    apply_words: list[str] = Field(default_factory=list, description="资源解析时实际应用的自定义识别词")
    title: Optional[str] = Field(default=None, description="标题")
    artists: list[str] = Field(default_factory=list, description="艺术家列表")
    composers: list[str] = Field(default_factory=list, description="作曲家，独立于艺人显示署名")
    conductors: list[str] = Field(default_factory=list, description="指挥")
    orchestras: list[str] = Field(default_factory=list, description="乐团或合唱团")
    performers: dict[str, list[str]] = Field(default_factory=dict, description="乐器或声部对应的演奏者；未注明职责用performer")
    artist: Optional[str] = Field(default=None, description="艺术家")
    album: Optional[str] = Field(default=None, description="专辑")
    album_artist: Optional[str] = Field(default=None, description="专辑级艺人署名")
    year: Optional[int] = Field(default=None, description="媒体发行年份")
    disc_number: Optional[int] = Field(default=None, description="碟号")
    track_number: Optional[int] = Field(default=None, description="曲目号")
    total_discs: Optional[int] = Field(default=None, description="专辑总碟数")
    total_tracks: Optional[int] = Field(default=None, description="专辑预期总曲目数")
    version: Optional[str] = Field(default=None, description="版本号")
    audio_format: Optional[str] = Field(default=None, description="音频编码格式")
    audio_lossless: Optional[bool] = Field(default=None, description="是否无损音频")
    audio_quality: Optional[Literal["hires", "lossless", "lossy"]] = Field(default=None, description="音质等级")
    audio_quality_score: int = Field(default=0, description="音质比较使用的排序分数")
    audio_specs: Optional[str] = Field(default=None, description="音频技术规格的展示文本")
    bit_depth: Optional[int] = Field(default=None, description="实际位深（bit）")
    sample_rate: Optional[int] = Field(default=None, description="实际采样率（Hz）")
    bitrate: Optional[int] = Field(default=None, description="实际码率（bps）")
    duration: Optional[int] = Field(default=None, description="音乐时长，单位为秒")
    isrc: Optional[str] = Field(default=None, description="国际标准录音制品编码")
    media_source: Optional[MediaSource] = Field(default=None, description="媒体数据源")
    media_id: Optional[str] = Field(default=None, description="数据源原生 ID")
    musicbrainz_release_id: Optional[str] = Field(default=None, description="具体发行版 MBID")
    musicbrainz_release_group_id: Optional[str] = Field(default=None, description="发行组 MBID")
    musicbrainz_release_track_id: Optional[str] = Field(default=None, description="发行版内曲目 MBID，不是 Recording ID")
    original_year: Optional[int] = Field(default=None, description="原始发行年份")
    release_year: Optional[int] = Field(default=None, description="当前发行版年份")
    field_sources: dict[str, str] = Field(
        default_factory=dict,
        description="字段的 tag/album_tags/stream/filename/directory/torrent/remote/manual 证据来源",
    )


class MusicInfo(OptionalMediaIdentityMixin, BaseModel):
    """
    标准化音乐元数据信息。
    """

    type: Literal["音乐"] = Field(default="音乐", description="类型标识")
    music_type: MusicEntityType = Field(default="recording", description="音乐实体类型：recording 单曲、album 专辑、artist 艺术家")
    media_source: Optional[MediaSource] = Field(default=None, description="媒体数据源")
    media_id: Optional[str] = Field(default=None, description="数据源原生 ID")
    title: Optional[str] = Field(default=None, description="标题")
    artists: list[str] = Field(default_factory=list, description="艺术家列表")
    composers: list[str] = Field(default_factory=list, description="作曲家，独立于艺人显示署名")
    conductors: list[str] = Field(default_factory=list, description="指挥")
    orchestras: list[str] = Field(default_factory=list, description="乐团或合唱团")
    performers: dict[str, list[str]] = Field(default_factory=dict, description="乐器或声部对应的演奏者；未注明职责用performer")
    artist: Optional[str] = Field(default=None, description="艺术家")
    artist_ids: list[str] = Field(default_factory=list, description="艺术家 ID")
    album: Optional[str] = Field(default=None, description="专辑")
    album_artist: Optional[str] = Field(default=None, description="专辑级艺人署名")
    album_id: Optional[str] = Field(default=None, description="专辑 ID")
    musicbrainz_release_id: Optional[str] = Field(default=None, description="具体发行版 MBID")
    musicbrainz_release_group_id: Optional[str] = Field(default=None, description="发行组 MBID")
    musicbrainz_release_track_id: Optional[str] = Field(default=None, description="发行版内曲目 MBID，不是 Recording ID")
    album_type: Optional[str] = Field(default=None, description="标签声明的发行主类型，例如 Album、EP、Single")
    secondary_types: list[str] = Field(default_factory=list, description="标签声明的 Compilation、Soundtrack 等副类型")
    year: Optional[int] = Field(default=None, description="媒体发行年份")
    original_year: Optional[int] = Field(default=None, description="原始发行年份")
    release_year: Optional[int] = Field(default=None, description="当前发行版年份")
    release_date: Optional[str] = Field(default=None, description="发行日期")
    disc_number: Optional[int] = Field(default=None, description="碟号")
    track_number: Optional[int] = Field(default=None, description="曲目号")
    total_tracks: Optional[int] = Field(default=None, description="专辑预期总曲目数")
    total_discs: Optional[int] = Field(default=None, description="专辑总碟数")
    duration: Optional[int] = Field(default=None, description="音乐时长，单位为秒")
    isrc: Optional[str] = Field(default=None, description="国际标准录音制品编码")
    cover_url: Optional[str] = Field(default=None, description="音乐封面图片地址")
    lyrics: Optional[str] = Field(default=None, description="歌词文本")
    version: Optional[str] = Field(default=None, description="版本号")
    audio_format: Optional[str] = Field(default=None, description="音频编码格式")
    audio_lossless: Optional[bool] = Field(default=None, description="是否无损音频")
    audio_quality: Optional[Literal["hires", "lossless", "lossy"]] = Field(default=None, description="音质等级")
    audio_quality_score: int = Field(default=0, description="音质比较使用的排序分数")
    audio_specs: Optional[str] = Field(default=None, description="音频技术规格的展示文本")
    bit_depth: Optional[int] = Field(default=None, description="实际位深（bit）")
    sample_rate: Optional[int] = Field(default=None, description="实际采样率（Hz）")
    bitrate: Optional[int] = Field(default=None, description="实际码率（bps）")
    library_category: Optional[str] = Field(default="", description="媒体库目录分类；category 是过渡期兼容字段")
    metadata_category: Optional[str] = Field(default="", description="来源提供的描述性分类，不参与目录选择")
    classification: Optional[ClassificationResult] = Field(default=None, description="分类策略计算结果")
    classification_facts: dict[str, ClassificationFactValue] = Field(default_factory=dict, description="用于分类的媒体事实")
    category: Optional[str] = Field(default="", description="媒体分类")
    genres: list[str] = Field(default_factory=list, description="流派")
    tags: list[str] = Field(default_factory=list, description="标签列表")
    artist_country: Optional[str] = Field(default=None, description="艺术家国家或地区")
    release_status: Optional[str] = Field(default=None, description="发行状态")
    names: list[str] = Field(default_factory=list, description="别名和译名")
    title_aliases: list[str] = Field(default_factory=list, description="媒体或专辑的其他标题")
    album_aliases: list[str] = Field(default_factory=list, description="专辑的其他名称")
    artist_aliases: list[str] = Field(default_factory=list, description="艺人的其他名称")
    detail_link: Optional[str] = Field(default=None, description="详情地址")
    listen_count: Optional[int] = Field(default=None, description="收听次数")
    raw_data: dict[str, JsonData] = Field(default_factory=dict, description="数据源返回的原始音乐详情")
    field_sources: dict[str, str] = Field(
        default_factory=dict,
        description="字段的 tag/album_tags/stream/filename/directory/torrent/remote/manual 证据来源",
    )
    music_layout: Optional[str] = Field(default=None, description="音频资源形态，例如整轨 image_cue 或分轨 tracks_cue")
    organization_error: Optional[str] = Field(default=None, description="阻止自动整理的资源结构错误")
    title_year: Optional[str] = Field(default=None, description="标题（年份）")
    poster_path: Optional[str] = Field(default=None, description="海报地址")
    backdrop_path: Optional[str] = Field(default=None, description="背景图片")
    overview: Optional[str] = Field(default=None, description="简介")
    vote_average: float = Field(default=0.0, description="评分")

    @field_validator("album_type", mode="before")
    @classmethod
    def _strip_album_type(cls, v: Optional[str]) -> Optional[str]:
        """
        去除专辑类型字段两端的空白，防止数据源返回带空格的值。
        """
        return v.strip() if isinstance(v, str) and v.strip() else None

    @model_validator(mode="before")
    @classmethod
    def _normalize_category_compatibility(cls, value: Any) -> Any:
        """
        迁移旧音乐 category 元数据，并让兼容字段只映射媒体库分类。
        """
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        has_library = "library_category" in payload
        has_metadata = "metadata_category" in payload
        legacy_category = payload.get("category") or ""
        if not has_library and not has_metadata and legacy_category:
            payload["metadata_category"] = legacy_category
            payload["library_category"] = ""
        library_category = payload.get("library_category") or ""
        payload["library_category"] = library_category
        payload["category"] = library_category
        return payload

    def __getattr__(self, name: str) -> None:
        """
        影视专用字段兜底返回 None：音乐模型不存在这些字段，避免下游逐点安全访问。

        与 domain 层的 MusicInfo 保持一致：通知、整理等共享影视字段的读写路径
        直接访问缺失属性时按空值处理，而不是抛 AttributeError。dunder 特殊方法
        除外，避免 copy/pickle 等机制对钩子的 hasattr 探测误判。
        """
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        return None


class MusicLibraryStatusRequest(BaseModel):
    """
    批量查询音乐媒体库状态的请求。
    """

    items: list[MusicInfo] = Field(min_length=1, max_length=500, description="待查询入库状态的音乐专辑列表，数量为 1 至 500 个")

    @field_validator("items")
    @classmethod
    def _validate_album_identities(cls, items: list[MusicInfo]) -> list[MusicInfo]:
        """
        只接受带稳定来源身份的专辑，避免按模糊标题误报已入库。
        """
        supported_sources = {
            MediaSource.MusicBrainz,
            MediaSource.TheAudioDB,
            MediaSource.DoubanMusic,
        }
        if any(
            item.music_type != "album"
            or item.media_source not in supported_sources
            or not item.media_id
            for item in items
        ):
            raise ValueError("仅支持带稳定音乐来源和媒体 ID 的专辑")
        return items


class MusicLibraryStatus(BaseModel):
    """
    音乐专辑在媒体库中的存在状态。
    """

    media_source: MediaSource = Field(description="媒体数据源")
    media_id: str = Field(description="数据源原生 ID")
    exists: bool = Field(description="音乐条目是否已存在于媒体库")


class MusicRelease(BaseModel):
    """
    音乐专辑下的单个发行版本。
    """

    media_id: Optional[str] = Field(default=None, description="数据源原生 ID")
    title: Optional[str] = Field(default=None, description="标题")
    date: Optional[str] = Field(default=None, description="时间")
    year: Optional[int] = Field(default=None, description="媒体发行年份")
    country: Optional[str] = Field(default=None, description="发行或艺人所属国家")
    status: Optional[str] = Field(default=None, description="发行版状态，例如正式发行或宣传版")
    packaging: Optional[str] = Field(default=None, description="音乐发行版的包装形式")
    formats: list[str] = Field(default_factory=list, description="发行版的载体格式列表")
    track_count: Optional[int] = Field(default=None, description="发行版曲目数量")
    cover_url: Optional[str] = Field(default=None, description="音乐封面图片地址")


class MusicAlbumInfo(OptionalMediaIdentityMixin, BaseModel):
    """
    标准化音乐专辑信息。
    """

    type: Literal["音乐"] = Field(default="音乐", description="类型标识")
    music_type: Literal["album"] = Field(default="album", description="音乐实体类型，固定为 album，表示专辑")
    media_source: Optional[MediaSource] = Field(default=None, description="媒体数据源")
    media_id: Optional[str] = Field(default=None, description="数据源原生 ID")
    musicbrainz_release_id: Optional[str] = Field(default=None, description="具体发行版 MBID")
    musicbrainz_release_group_id: Optional[str] = Field(default=None, description="发行组 MBID")
    original_year: Optional[int] = Field(default=None, description="原始发行年份")
    total_discs: Optional[int] = Field(default=None, description="专辑总碟数")
    field_sources: dict[str, str] = Field(
        default_factory=dict,
        description="字段的 tag/album_tags/stream/filename/directory/torrent/remote/manual 证据来源",
    )
    title: Optional[str] = Field(default=None, description="标题")
    artists: list[str] = Field(default_factory=list, description="艺术家列表")
    composers: list[str] = Field(default_factory=list, description="作曲家，独立于艺人显示署名")
    conductors: list[str] = Field(default_factory=list, description="指挥")
    orchestras: list[str] = Field(default_factory=list, description="乐团或合唱团")
    performers: dict[str, list[str]] = Field(default_factory=dict, description="乐器或声部对应的演奏者；未注明职责用performer")
    artist: Optional[str] = Field(default=None, description="艺术家")
    artist_ids: list[str] = Field(default_factory=list, description="艺术家 ID")
    title_aliases: list[str] = Field(default_factory=list, description="媒体或专辑的其他标题")
    artist_aliases: list[str] = Field(default_factory=list, description="艺人的其他名称")
    album: Optional[str] = Field(default=None, description="专辑")
    album_type: Optional[str] = Field(default=None, description="标签声明的发行主类型，例如 Album、EP、Single")
    secondary_types: list[str] = Field(default_factory=list, description="标签声明的 Compilation、Soundtrack 等副类型")
    year: Optional[int] = Field(default=None, description="媒体发行年份")
    release_date: Optional[str] = Field(default=None, description="发行日期")
    total_tracks: Optional[int] = Field(default=None, description="专辑预期总曲目数")
    duration: Optional[int] = Field(default=None, description="音乐时长，单位为秒")
    cover_url: Optional[str] = Field(default=None, description="音乐封面图片地址")
    genres: list[str] = Field(default_factory=list, description="流派")
    tags: list[str] = Field(default_factory=list, description="标签列表")
    artist_country: Optional[str] = Field(default=None, description="艺术家国家或地区")
    release_status: Optional[str] = Field(default=None, description="发行状态")
    library_category: Optional[str] = Field(default="", description="媒体库目录分类；category 是过渡期兼容字段")
    metadata_category: Optional[str] = Field(default="", description="来源提供的描述性分类，不参与目录选择")
    classification: Optional[ClassificationResult] = Field(default=None, description="分类策略计算结果")
    classification_facts: dict[str, ClassificationFactValue] = Field(default_factory=dict, description="用于分类的媒体事实")
    category: Optional[str] = Field(default="", description="媒体分类")
    rating: float = Field(default=0.0, description="专辑评分，统一为十分制")
    rating_votes: Optional[int] = Field(default=None, description="音乐评分的投票人数")
    detail_link: Optional[str] = Field(default=None, description="详情地址")
    tracks: list[MusicInfo] = Field(default_factory=list, description="专辑曲目列表")
    releases: list[MusicRelease] = Field(default_factory=list, description="音乐专辑的发行版列表")
    raw_data: dict[str, JsonData] = Field(default_factory=dict, description="数据源返回的原始音乐详情")
    title_year: Optional[str] = Field(default=None, description="标题（年份）")
    poster_path: Optional[str] = Field(default=None, description="海报地址")
    backdrop_path: Optional[str] = Field(default=None, description="背景图片")
    overview: Optional[str] = Field(default=None, description="简介")
    vote_average: float = Field(default=0.0, description="评分")

    @field_validator("album_type", mode="before")
    @classmethod
    def _strip_album_type(cls, v: Optional[str]) -> Optional[str]:
        """
        去除专辑类型字段两端的空白，防止数据源返回带空格的值。
        """
        return v.strip() if isinstance(v, str) and v.strip() else None

    @model_validator(mode="before")
    @classmethod
    def _normalize_category_compatibility(cls, value: Any) -> Any:
        """
        迁移旧专辑 category 元数据，并让兼容字段只映射媒体库分类。
        """
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        has_library = "library_category" in payload
        has_metadata = "metadata_category" in payload
        legacy_category = payload.get("category") or ""
        if not has_library and not has_metadata and legacy_category:
            payload["metadata_category"] = legacy_category
            payload["library_category"] = ""
        library_category = payload.get("library_category") or ""
        payload["library_category"] = library_category
        payload["category"] = library_category
        return payload


class MusicArtistInfo(OptionalMediaIdentityMixin, BaseModel):
    """
    标准化音乐艺术家信息。
    """

    type: Literal["音乐"] = Field(default="音乐", description="类型标识")
    music_type: Literal["artist"] = Field(default="artist", description="音乐实体类型，固定为 artist，表示艺人")
    media_source: Optional[MediaSource] = Field(default=None, description="媒体数据源")
    media_id: Optional[str] = Field(default=None, description="数据源原生 ID")
    name: Optional[str] = Field(default=None, description="显示名称")
    title: Optional[str] = Field(default=None, description="标题")
    sort_name: Optional[str] = Field(default=None, description="艺人排序使用的名称")
    disambiguation: Optional[str] = Field(default=None, description="用于区分同名艺人的补充信息")
    artist_type: Optional[str] = Field(default=None, description="艺人实体类型")
    gender: Optional[str] = Field(default=None, description="人物或艺人的性别信息")
    country: Optional[str] = Field(default=None, description="发行或艺人所属国家")
    area: Optional[str] = Field(default=None, description="艺人所属地区")
    begin_date: Optional[str] = Field(default=None, description="艺人开始活动的日期")
    end_date: Optional[str] = Field(default=None, description="艺人结束活动的日期")
    ended: bool = Field(default=False, description="艺人的活动是否已经结束")
    life_span: Optional[str] = Field(default=None, description="艺人的活动起止时间")
    genres: list[str] = Field(default_factory=list, description="流派")
    tags: list[str] = Field(default_factory=list, description="标签列表")
    aliases: list[str] = Field(default_factory=list, description="艺人的其他名称")
    classification_facts: dict[str, ClassificationFactValue] = Field(default_factory=dict, description="用于分类的媒体事实")
    relation: Optional[str] = Field(default=None, description="人物或艺人与当前条目的关系")
    image_url: Optional[str] = Field(default=None, description="展示图片地址")
    detail_link: Optional[str] = Field(default=None, description="详情地址")
    external_links: dict[str, str] = Field(default_factory=dict, description="艺人在外部平台的关联链接")
    album_count: Optional[int] = Field(default=None, description="艺人的专辑数量")
    raw_data: dict[str, JsonData] = Field(default_factory=dict, description="数据源返回的原始音乐详情")
    poster_path: Optional[str] = Field(default=None, description="海报地址")
    overview: Optional[str] = Field(default=None, description="简介")


class MusicRecognizeRequest(RequiredMediaIdentityMixin, BaseModel):
    """
    音乐元数据详情识别请求。
    """

    media_source: MediaSource = Field(description="媒体数据源")
    media_id: str = Field(description="数据源原生 ID")
    music_type: Optional[MusicTargetEntityType] = Field(default=None, description="音乐实体类型：recording 单曲、album 专辑")


class MusicRecognitionCacheItem(BaseModel):
    """
    单条 MusicBrainz 识别缓存。
    """

    key: str = Field(description="缓存条目的键")
    media_id: str = Field(default="", description="数据源原生 ID")
    title: str = Field(default="", description="标题")
    artists: list[str] = Field(default_factory=list, description="艺术家列表")
    album: str = Field(default="", description="专辑")
    year: str | int = Field(default="", description="媒体发行年份")
    music_type: str = Field(default="recording", description="音乐实体类型：recording 单曲、album 专辑")
    cover_url: str = Field(default="", description="音乐封面图片地址")


class MusicRecognitionCacheData(BaseModel):
    """
    MusicBrainz 识别缓存统计及明细。
    """

    count: int = Field(default=0, description="条目数量")
    recognized: int = Field(default=0, description="成功识别的音乐缓存条目数量")
    unrecognized: int = Field(default=0, description="尚未识别的缓存条目数量")
    data: list[MusicRecognitionCacheItem] = Field(default_factory=list, description="音乐识别缓存明细列表")
