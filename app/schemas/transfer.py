"""
文件整理输入、执行结果、人工复核及队列投影的数据结构。
"""

from pathlib import Path
from typing import List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.common import JsonData
from app.schemas.context import MediaInfo, MetaInfo
from app.schemas.file import FileItem
from app.schemas.media import OptionalMediaIdentityMixin
from app.schemas.music import MusicInfo, MusicMeta
from app.schemas.types import MediaSource, MusicTargetEntityType


class DownloaderTorrent(BaseModel):
    """
    下载器任务信息。
    """
    downloader: Optional[str] = Field(default=None, description="下载器")
    hash: Optional[str] = Field(default=None, description="下载任务 hash")
    title: Optional[str] = Field(default=None, description="标题")
    site_name: Optional[str] = Field(default=None, description="站点名称")
    name: Optional[str] = Field(default=None, description="显示名称")
    year: Optional[str] = Field(default=None, description="媒体发行年份")
    season_episode: Optional[str] = Field(default=None, description="季集信息的展示文本")
    path: Optional[Path] = Field(default=None, description="文件或目录路径")
    size: Optional[float] = Field(default=0.0, description="文件大小，单位为字节")
    progress: Optional[float] = Field(default=0.0, description="当前完成百分比")
    state: Optional[str] = Field(default="downloading", description="当前业务状态")
    upspeed: Optional[str] = Field(default=None, description="当前上传速度的格式化展示文本")
    dlspeed: Optional[str] = Field(default=None, description="当前下载速度的格式化展示文本")
    tags: Optional[str] = Field(default=None, description="下载器返回的任务标签文本")
    save_path: Optional[str] = Field(default=None, description="保存路径")
    content_path: Optional[str] = Field(default=None, description="下载任务当前的内容路径")
    category: Optional[str] = Field(default=None, description="下载器中的任务分类")
    download_limit: Optional[float] = Field(default=None, description="下载速度上限，单位为 KB/s，0 表示不限速")
    upload_limit: Optional[float] = Field(default=None, description="上传速度上限，单位为 KB/s，0 表示不限速")
    ratio_limit: Optional[float] = Field(default=None, description="做种分享率上限")
    seeding_time_limit: Optional[int] = Field(default=None, description="做种时间限制，单位为分钟；具体支持范围由下载器决定")
    trackers: Optional[List[str]] = Field(default_factory=list, description="种子的 Tracker 地址列表")
    media: Optional["DownloadTaskMedia"] = Field(default=None, description="关联媒体信息")
    userid: Optional[str] = Field(default=None, description="用户 ID")
    username: Optional[str] = Field(default=None, description="用户名")
    left_time: Optional[str] = Field(default=None, description="预计下载剩余时间的展示文本")


class DownloaderFile(BaseModel):
    """
    下载器文件项的宿主投影，隔离各 provider SDK 的对象差异。
    """

    model_config = ConfigDict(from_attributes=True)

    id: Optional[Union[int, str]] = Field(default=None, description="记录标识")
    name: str = Field(description="下载任务内的相对文件路径")
    size: Optional[int] = Field(default=None, description="文件大小，单位为字节")
    priority: Optional[int] = Field(default=None, description="优先级")
    progress: Optional[float] = Field(default=None, description="下载器报告的文件完成进度，数值口径随下载器接口保持不变")


class DownloadTaskMedia(OptionalMediaIdentityMixin, BaseModel):
    """
    下载任务关联的影视或音乐媒体摘要。
    """

    type: Optional[str] = Field(default=None, description="类型标识")
    title: Optional[str] = Field(default=None, description="标题")
    season: Optional[list[int] | int | str] = Field(default=None, description="季号")
    episode: Optional[list[int] | int | str] = Field(default=None, description="集号")
    image: Optional[str] = Field(default=None, description="图片")
    poster: Optional[str] = Field(default=None, description="海报")
    backdrop: Optional[str] = Field(default=None, description="背景图")
    media_source: Optional[MediaSource] = Field(default=None, description="媒体数据源")
    media_id: Optional[str] = Field(default=None, description="数据源原生 ID")
    music_type: Optional[str] = Field(default=None, description="音乐实体类型：recording 单曲、album 专辑")
    artists: list[str] = Field(default_factory=list, description="艺术家列表")
    album: Optional[str] = Field(default=None, description="专辑")
    album_id: Optional[str] = Field(default=None, description="专辑 ID")
    total_tracks: Optional[int] = Field(default=None, description="专辑预期总曲目数")
    track_number: Optional[int] = Field(default=None, description="曲目号")


class TransferTorrent(DownloaderTorrent):
    """
    待转移任务信息。
    """


class DownloadingTorrent(DownloaderTorrent):
    """
    下载中任务信息。
    """


# 进程内整理工作项由 app.application.transfer.workflow 持有，避免模式层依赖领域对象形成导入环。
# 本模块只保留面向前端的 TransferJob 与 TransferJobTask 数据投影。


class TransferJobTask(BaseModel):
    """
    文件整理作业任务。
    """
    fileitem: Optional[FileItem] = Field(default=None, description="待整理的源文件或目录条目")
    meta: Optional[Union[MusicMeta, MetaInfo]] = Field(default=None, description="文件名解析对象")
    state: Optional[str] = Field(default=None, description="当前业务状态")
    downloader: Optional[str] = Field(default=None, description="下载器")
    download_hash: Optional[str] = Field(default=None, description="下载任务 hash")


class TransferJob(BaseModel):
    """
    文件整理作业。
    """
    media: Optional[Union[MusicInfo, MediaInfo]] = Field(default=None, description="关联媒体信息")
    season: Optional[int] = Field(default=None, description="季号")
    tasks: Optional[List[TransferJobTask]] = Field(default_factory=list, description="任务列表")


class TransferQueueFileItemData(BaseModel):
    """
    整理队列快照只需要的源文件字段，不携带云盘目录树和 provider 扩展字段。
    """

    path: Optional[str] = Field(default=None, description="文件或目录路径")
    storage: Optional[str] = Field(default="local", description="存储")
    type: Optional[str] = Field(default=None, description="类型标识")
    name: Optional[str] = Field(default=None, description="显示名称")
    size: Optional[int] = Field(default=None, description="文件大小，单位为字节")


class TransferQueueMediaData(BaseModel):
    """
    整理队列 UI 所需的媒体身份与海报摘要。
    """

    media_source: Optional[str] = Field(default=None, description="媒体数据源")
    media_id: Optional[str] = Field(default=None, description="数据源原生 ID")
    title: Optional[str] = Field(default=None, description="标题")
    title_year: Optional[str] = Field(default=None, description="标题（年份）")
    year: Optional[str] = Field(default=None, description="媒体发行年份")
    poster_path: Optional[str] = Field(default=None, description="海报地址")
    episode_run_time: Optional[List[int]] = Field(default_factory=list, description="来源提供的单集时长列表，单位为分钟；省略时为空列表，也允许显式传入 None")
    origin_country: Optional[List[str]] = Field(default_factory=list, description="原产国")


class TransferQueueTaskData(BaseModel):
    """
    整理队列 UI 所需的任务状态与源文件摘要。
    """

    fileitem: TransferQueueFileItemData = Field(description="队列中待整理的源文件摘要")
    state: Optional[str] = Field(default=None, description="当前业务状态")


class TransferQueueItemData(BaseModel):
    """
    单个媒体分组的轻量整理队列投影。
    """

    media: Optional[TransferQueueMediaData] = Field(default=None, description="关联媒体信息")
    season: Optional[int] = Field(default=None, description="季号")
    tasks: List[TransferQueueTaskData] = Field(default_factory=list, description="任务列表")


class TransferQueuePageData(BaseModel):
    """
    前端整理队列的受限快照。
    """

    items: List[TransferQueueItemData] = Field(default_factory=list, description="当前页条目列表")
    total: int = Field(default=0, description="总条目数量")
    page: int = Field(default=1, description="页码，从 1 开始")
    count: int = Field(default=100, description="每页最多返回的媒体分组数量，默认 100")


class TransferInfo(BaseModel):
    """
    文件整理结果。
    """
    success: bool = Field(default=True, description="是否成功标志")
    fileitem: Optional[FileItem] = Field(default=None, description="整理前的源文件或目录条目")
    target_diritem: Optional[FileItem] = Field(default=None, description="转移后的目录项，媒体的根目录")
    target_item: Optional[FileItem] = Field(default=None, description="转移后路径")
    transfer_type: Optional[str] = Field(default=None, description="整理方式")
    file_count: Optional[int] = Field(default=0, description="处理文件数")
    file_list: Optional[list[str]] = Field(default_factory=list, description="处理文件清单")
    file_list_new: Optional[list[str]] = Field(default_factory=list, description="目标文件清单")
    total_size: Optional[int] = Field(default=0, description="本次处理文件的总大小，单位为字节")
    fail_list: Optional[list[str]] = Field(default_factory=list, description="失败清单")
    message: Optional[str] = Field(default=None, description="错误信息")
    need_scrape: Optional[bool] = Field(default=False, description="是否需要刮削")
    need_notify: Optional[bool] = Field(default=False, description="是否需要通知")
    # 是否因覆盖模式判定「不覆盖」而放弃整理。
    overwrite_skipped: Optional[bool] = Field(default=False, description="这是一次正常的策略裁决而非整理故障，调用方据此决定是否写失败历史与推送失败通知")
    failure_stage: Optional[str] = Field(default=None, description="失败所在阶段和建议动作，供通知、历史和前端统一展示")
    recovery_action: Optional[str] = Field(default=None, description="当前失败状态下可执行的恢复操作")
    cleanup_status: Optional[str] = Field(default=None, description="清理下载器任务的结果；整理成功但清理失败时保留独立状态")
    cleanup_error: Optional[str] = Field(default=None, description="下载器清理失败的原因")

    def to_dict(self) -> dict[str, object]:
        """
        返回字典。

        :return: 整理结果字段字典，源文件和目标文件项转换为可序列化字典
        """
        dicts = vars(self).copy()
        dicts["fileitem"] = self.fileitem.model_dump() if self.fileitem else None
        dicts["target_item"] = self.target_item.model_dump() if self.target_item else None
        return dicts


class TransferManualReviewRequest(BaseModel):
    """
    人工判定外部结果不确定整理步骤的请求。
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    operation_id: str = Field(min_length=1, description="待判定的稳定操作标识")
    decision: Literal["not_applied", "applied"] = Field(
        description="人工判定；不公开 failed，失败终态只能由 durable 结算写入",
    )
    reason: str = Field(default="", max_length=2000, description="人工判定理由，可选")
    result_payload: Optional[dict[str, JsonData]] = Field(
        default=None,
        description="判定为 applied 时必填的外部结果证据",
    )

    @model_validator(mode="after")
    def validate_result_payload(self) -> "TransferManualReviewRequest":
        """
        要求已发生判定携带可持久化的结果证据。
        """
        if self.decision == "applied" and self.result_payload is None:
            raise ValueError("判定为 applied 时必须提供 result_payload")
        return self


class TransferManualReviewDiscardRequest(BaseModel):
    """显式放弃人工复核任务；不会操作源文件或目标文件。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    operation_id: str = Field(min_length=1, description="当前人工复核步骤的稳定操作标识")
    reason: str = Field(default="", max_length=2000, description="放弃理由，可选")


class TransferManualReviewDiscardData(BaseModel):
    """人工复核任务放弃后的公开结果。"""

    task_id: str = Field(description="任务标识")
    operation_id: str = Field(description="当前人工复核步骤标识")
    discarded: bool = Field(description="是否已解除任务占用")
    message: str = Field(description="处理结果说明")


class TransferManualReviewData(BaseModel):
    """
    人工复核提交后的公开状态投影。
    """

    task_id: str = Field(description="任务标识")
    operation_id: str = Field(description="待判定的稳定操作标识")
    decision: Literal["not_applied", "applied"] = Field(description="人工判定；不公开 failed，失败终态只能由 durable 结算写入")
    state: str = Field(description="当前业务状态")
    review_revision: int = Field(description="人工复核状态的版本号")


class TransferManualReviewSourceData(BaseModel):
    """
    人工复核任务的源文件身份。
    """

    storage: str = Field(description="存储")
    path: str = Field(description="文件或目录路径")


class TransferManualReviewStepData(BaseModel):
    """
    人工复核步骤的公开意图与事实证据。
    """

    operation_id: str = Field(description="待判定的稳定操作标识")
    kind: str = Field(description="数据项类型")
    intent: dict[str, JsonData] = Field(description="人工复核步骤的操作意图")
    evidence: Optional[dict[str, JsonData]] = Field(default=None, description="人工复核所依据的证据")
    error: Optional[str] = Field(default=None, description="错误摘要")


class TransferManualReviewTaskData(BaseModel):
    """
    可由管理员发现和判定的 durable 整理任务。
    """

    task_id: str = Field(description="任务标识")
    source: TransferManualReviewSourceData = Field(description="待复核任务的源存储和文件路径")
    state: Literal["manual_review", "retry_wait"] = Field(description="当前业务状态")
    step: TransferManualReviewStepData = Field(description="人工复核当前步骤")
    review_revision: int = Field(description="人工复核状态的版本号")


class TransferManualReviewPageData(BaseModel):
    """
    人工复核任务分页结果。
    """

    items: list[TransferManualReviewTaskData] = Field(default_factory=list, description="当前页条目列表")
    total: int = Field(default=0, description="总条目数量")
    page: int = Field(default=1, description="页码，从 1 开始")
    page_size: int = Field(default=30, description="每页返回的记录数量")


class EpisodeFormat(BaseModel):
    """
    剧集自定义识别格式。
    """
    format: Optional[str] = Field(default=None, description="格式定义")
    detail: Optional[str] = Field(default=None, description="剧集格式的详细解析信息")
    part: Optional[str] = Field(default=None, description="分段媒体的分段标识")
    offset: Optional[str] = Field(default=None, description="集号偏移量")


class EpisodeFormatRule(BaseModel):
    """
    集数定位规则。
    """
    name: str = Field(description="显示名称")
    enabled: bool = Field(default=True, description="是否启用")
    order: int = Field(default=0, description="展示顺序")
    pattern: str = Field(description="剧集名称匹配表达式")
    min_file_size_mb: int = Field(default=0, description="最小文件大小，单位为 MiB")


class EpisodeFormatRecommendItem(BaseModel):
    """
    集数定位推荐请求。
    """
    fileitem: Optional[FileItem] = Field(default=None, description="用于推荐集数格式的单个文件或目录")
    fileitems: Optional[List[FileItem]] = Field(default=None, description="文件项列表（前端多选时传入）")


class ManualTransferItem(OptionalMediaIdentityMixin, BaseModel):
    """
    手动整理请求，媒体身份接受内置或插件来源与原生 ID。
    """

    fileitem: Optional[FileItem] = Field(default=None, description="文件项")
    fileitems: Optional[List[FileItem]] = Field(default=None, description="文件项列表（前端多选时传入）")
    logid: Optional[int] = Field(default=None, description="日志ID")
    logids: Optional[List[int]] = Field(default=None, description="日志ID列表（前端多选历史记录时传入）")
    target_storage: Optional[str] = Field(default=None, description="目标存储")
    target_path: Optional[str] = Field(default=None, description="目标路径")
    media_source: Optional[MediaSource] = Field(default=None, description="媒体数据源")
    media_id: Optional[str] = Field(default=None, description="数据源原生ID")
    music_type: Optional[MusicTargetEntityType] = Field(default=None, description="音乐实体类型")
    musicbrainz_release_id: Optional[str] = Field(
        default=None,
        pattern=r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$",
        description="显式选择具体发行版；media_id 仍为 Release Group，不能互换",
    )
    music_release_regions: Optional[List[str]] = Field(
        default=None,
        max_length=3,
        description="本次手动整理的 MusicBrainz 发行地区优先级；空值继承系统设置",
    )
    music_release_scripts: Optional[List[str]] = Field(
        default=None,
        max_length=3,
        description="本次手动整理的 MusicBrainz 文字字形优先级；空值继承系统设置",
    )
    music_cue_enable: Optional[bool] = Field(default=None, strict=True, description="本次手动整理是否识别 CUE；空值继承系统设置")
    type_name: Optional[str] = Field(default=None, description="类型")
    season: Optional[int] = Field(default=None, description="季号")
    transfer_type: Optional[str] = Field(default=None, description="整理方式")
    episode_format: Optional[str] = Field(default=None, description="自定义格式")
    episode_detail: Optional[str] = Field(default=None, description="指定集数")
    episode_part: Optional[str] = Field(default=None, description="指定PART")
    episode_offset: Optional[str] = Field(default=None, description="集数偏移")
    min_filesize: Optional[int] = Field(default=0, description="最低文件大小阈值，单位为 MiB；0 不限制，启用时仅保留严格大于阈值的文件")
    scrape: Optional[bool] = Field(default=False, description="刮削")
    library_type_folder: Optional[bool] = Field(default=None, description="媒体库类型子目录")
    library_category_folder: Optional[bool] = Field(default=None, description="媒体库类别子目录")
    from_history: Optional[bool] = Field(default=False, description="复用历史识别信息")
    episode_group: Optional[str] = Field(default=None, description="剧集组")
    preview: Optional[bool] = Field(default=False, description="仅预览，不执行整理")
    reorganize: Optional[bool] = Field(default=False, description="重新整理，清理命中的成功历史及其旧目标")
    skip_success: bool = Field(default=False, description="跳过成功历史，优先于重新整理；预览与执行使用相同过滤范围")

    @model_validator(mode="after")
    def validate_music_release_selection(self) -> "ManualTransferItem":
        """
        具体发行只用于明确的 MusicBrainz 专辑，禁止混用录音或其它来源身份。
        """
        if self.musicbrainz_release_id is None:
            return self
        if self.media_source != MediaSource.MusicBrainz or not self.media_id or self.music_type != "album":
            raise ValueError("指定音乐发行版需要 MusicBrainz 专辑来源、Release Group ID 和 music_type=album")
        if (self.type_name or "").strip().lower() not in {"", "自动", "auto", "none", "音乐"}:
            raise ValueError("音乐发行版不能用于影视整理")
        self.musicbrainz_release_id = self.musicbrainz_release_id.lower()
        return self

    @model_validator(mode="after")
    def normalize_music_release_preferences(self) -> "ManualTransferItem":
        """
        规范手动发行偏好并拒绝重复或非法 ISO 代码。
        """
        if self.music_release_regions is not None:
            regions = [str(value).strip().upper() for value in self.music_release_regions]
            if any(len(value) != 2 or not value.isascii() or not value.isalpha() for value in regions):
                raise ValueError("音乐发行地区必须使用两位代码")
            if len(set(regions)) != len(regions):
                raise ValueError("音乐发行地区优先级不能重复")
            self.music_release_regions = regions
        if self.music_release_scripts is not None:
            scripts = [str(value).strip().title() for value in self.music_release_scripts]
            if any(len(value) != 4 or not value.isascii() or not value.isalpha() for value in scripts):
                raise ValueError("音乐文字字形必须使用四位 ISO 15924 代码")
            if len(set(scripts)) != len(scripts):
                raise ValueError("音乐文字字形优先级不能重复")
            self.music_release_scripts = scripts
        return self


class ManualTransferHistoryInfo(BaseModel):
    """
    手动整理命中的成功历史摘要。
    """

    reorganize: bool = Field(default=False, description="是否应显示重新整理操作")
    history_count: int = Field(default=0, description="命中的成功历史数量")


class ManualTransferTargetPath(BaseModel):
    """
    手动整理目的路径匹配结果。
    """

    target_storage: Optional[str] = Field(default=None, description="目标存储")
    target_path: Optional[str] = Field(default=None, description="目标路径")
    transfer_type: Optional[str] = Field(default=None, description="整理方式")
    scrape: Optional[bool] = Field(default=False, description="刮削")
    library_type_folder: Optional[bool] = Field(default=False, description="媒体库类型子目录")
    library_category_folder: Optional[bool] = Field(default=False, description="媒体库类别子目录")


class ManualTransferPreviewSummary(BaseModel):
    """
    手动整理预览数量统计。
    """

    total: int = Field(default=0, description="总条目数量")
    success: int = Field(default=0, description="预览成功的文件数量")
    failed: int = Field(default=0, description="失败条目数量")


class MusicTransferCandidate(BaseModel):
    """
    待确认候选的最小身份摘要；录音、发行组和发行ID保持各自语义。
    """

    media_source: Optional[str] = Field(default=None, description="媒体数据源")
    media_id: Optional[str] = Field(default=None, description="数据源原生 ID")
    music_type: Optional[str] = Field(default=None, description="音乐实体类型：recording 单曲、album 专辑")
    release_id: Optional[str] = Field(default=None, description="音乐发行版标识")
    album_id: Optional[str] = Field(default=None, description="专辑 ID")
    title: Optional[str] = Field(default=None, description="标题")
    artist: Optional[str] = Field(default=None, description="艺术家")
    year: Optional[str] = Field(default=None, description="媒体发行年份")


class MusicTransferPreview(BaseModel):
    """
    音乐识别证据与当前扫描分组；与文件执行成功状态相互独立。
    """

    status: Literal["local_tags", "local_cue", "matched", "manual", "metadata", "not_found",
                    "ambiguous", "conflict", "service_error", "budget_exhausted", "unsupported"] = Field(
        description=(
            "识别结论：local_tags 本地标签、local_cue 本地曲目表、matched 在线确认、manual 手工指定、"
            "metadata 仅元数据、not_found 未匹配、ambiguous 有歧义、conflict 证据冲突、"
            "service_error 服务失败、budget_exhausted 预算耗尽、unsupported 不支持"
        ),
    )
    online_confirmed: bool = Field(default=False, description="是否已通过在线元数据确认身份")
    music_type: Optional[str] = Field(default=None, description="音乐实体类型：recording 单曲、album 专辑")
    media_source: Optional[str] = Field(default=None, description="媒体数据源")
    media_id: Optional[str] = Field(default=None, description="数据源原生 ID")
    title: Optional[str] = Field(default=None, description="标题")
    album: Optional[str] = Field(default=None, description="专辑")
    artists: list[str] = Field(default_factory=list, description="艺术家列表")
    album_artist: Optional[str] = Field(default=None, description="专辑级艺人署名")
    year: Optional[int] = Field(default=None, description="媒体发行年份")
    musicbrainz_release_id: Optional[str] = Field(default=None, description="具体发行版 MBID")
    musicbrainz_release_group_id: Optional[str] = Field(default=None, description="发行组 MBID")
    disc_number: Optional[int] = Field(default=None, description="碟号")
    track_number: Optional[int] = Field(default=None, description="曲目号")
    total_discs: Optional[int] = Field(default=None, description="专辑总碟数")
    total_tracks: Optional[int] = Field(default=None, description="专辑预期总曲目数")
    layout: Optional[str] = Field(default=None, description="音乐资源布局")
    field_sources: dict[str, str] = Field(
        default_factory=dict,
        description="字段的 tag/album_tags/stream/filename/directory/torrent/remote/manual 证据来源",
    )
    candidates: list[MusicTransferCandidate] = Field(default_factory=list, description="需要用户确认的音乐身份候选列表")
    read_status: Literal["tags", "stream_only", "unreadable", "name_only", "companion", "unknown"] = Field(
        default="unknown",
        description="读取状态：tags 有音频标签、stream_only 仅流信息、unreadable 无法读取、name_only 仅文件名、companion 配套文件、unknown 未知",
    )
    file_role: Literal["audio", "companion"] = Field(default="audio", description="音乐文件在资源组中的角色")
    group_id: Optional[str] = Field(default=None, description="标识只供当前已预览文件分组；不授予权限，也不授权展开该目录")
    group_directory: Optional[str] = Field(default=None, description="音乐资源组所在目录")
    group_size: int = Field(default=0, description="音乐资源组的文件数量")


class ManualTransferPreviewItem(BaseModel):
    """
    单个文件的手动整理预览。
    """

    source: Optional[str] = Field(default=None, description="待整理的源文件路径")
    source_storage: Optional[str] = Field(default=None, description="源文件所在存储类型")
    source_item: Optional[FileItem] = Field(default=None, description="源文件信息，即待整理的文件信息")
    music: Optional[MusicTransferPreview] = Field(default=None, description="音乐识别证据与资源分组预览")
    target: Optional[str] = Field(default=None, description="预览生成的目标文件路径")
    target_dir: Optional[str] = Field(default=None, description="整理目标目录")
    success: bool = Field(default=False, description="操作是否成功")
    message: Optional[str] = Field(default=None, description="面向调用方的结果说明")
    failure_stage: Optional[str] = Field(default=None, description="失败阶段、建议动作和覆盖策略裁决，供前端直接给出下一步处理方式")
    recovery_action: Optional[str] = Field(default=None, description="当前失败状态下可执行的恢复操作")
    overwrite_skipped: bool = Field(default=False, description="这是一次正常的策略裁决而非整理故障，调用方据此决定是否写失败历史与推送失败通知")
    type: Optional[str] = Field(default=None, description="类型标识")
    title: Optional[str] = Field(default=None, description="标题")
    season: Optional[int] = Field(default=None, description="季号")
    episode: Optional[int] = Field(default=None, description="集号")
    episode_end: Optional[int] = Field(default=None, description="连续剧集范围的结束集数")
    part: Optional[int | str] = Field(default=None, description="分段媒体的分段标识")
    org_string: Optional[str] = Field(default=None, description="原字符串")
    apply_words: list[str] = Field(default_factory=list, description="资源解析时实际应用的自定义识别词")
    resource_team: Optional[str] = Field(default=None, description="识别的制作组/字幕组")
    customization: Optional[str] = Field(default=None, description="本次整理采用的自定义参数")


class ManualTransferResultData(BaseModel):
    """
    手动整理预览数据，不表示执行或入库已完成。
    """

    summary: Optional[ManualTransferPreviewSummary] = Field(default=None, description="本次整理结果的数量汇总")
    items: list[ManualTransferPreviewItem] = Field(default_factory=list, description="逐文件整理预览结果")
    message: Optional[str] = Field(default=None, description="面向调用方的结果说明")


class ManualTransferSubmissionItem(BaseModel):
    """
    实际提交的逐文件回执，接收和等待重试均不表示已入库。
    """

    state: Literal["accepted", "completed", "failed", "retry_wait", "skipped", "manual_review"] = Field(
        description="当前业务状态",
    )
    source: Optional[str] = Field(default=None, description="本次提交的源文件路径")
    target: Optional[str] = Field(default=None, description="本次提交对应的目标文件路径")
    target_dir: Optional[str] = Field(default=None, description="整理目标目录")
    success: bool = Field(default=False, description="表示本次操作被接收或完成；实际入库只以 completed 为准")
    message: Optional[str] = Field(default=None, description="面向调用方的结果说明")
    failure_stage: Optional[str] = Field(default=None, description="失败所在阶段和建议动作，供通知、历史和前端统一展示")
    recovery_action: Optional[str] = Field(default=None, description="当前失败状态下可执行的恢复操作")
    overwrite_skipped: bool = Field(default=False, description="这是一次正常的策略裁决而非整理故障，调用方据此决定是否写失败历史与推送失败通知")


class ManualTransferSubmissionData(BaseModel):
    """
    实际提交结果，即使批次失败也保留已接收、完成和跳过的文件。
    """

    items: list[ManualTransferSubmissionItem] = Field(default_factory=list, description="逐文件整理提交回执")
    message: Optional[str] = Field(default=None, description="面向调用方的结果说明")


class EpisodeFormatRecommendData(BaseModel):
    """
    集数定位模板推荐结果。
    """

    rule_name: str = Field(description="命中的剧集格式规则名称")
    episode_format: str = Field(description="自定义格式")
    sample_file: str = Field(description="用于推荐剧集格式的示例文件")
    pattern: Optional[str] = Field(default=None, description="剧集名称匹配表达式")
    rule_index: Optional[int] = Field(default=None, description="命中规则在规则列表中的位置")
    min_file_size_mb: Optional[int] = Field(default=None, description="最小文件大小，单位为 MiB")
    sample_count: Optional[int] = Field(default=None, description="实际参与比较的唯一有效样本数量")
    majority_count: Optional[int] = Field(default=None, description="支持所选剧集格式的样本数量")
    confidence: Optional[str] = Field(default=None, description="剧集格式推荐的置信程度")
    size_filter_relaxed: Optional[bool] = Field(default=None, description="是否放宽了最小文件大小筛选")
    native_verified_count: Optional[int] = Field(default=None, description="通过原始集号验证的样本数量")
    native_fallback_count: Optional[int] = Field(default=None, description="使用原始集号回退的样本数量")
    native_conflict_count: Optional[int] = Field(default=None, description="原始集号与推荐结果冲突的样本数量")
    reason: Optional[str] = Field(default=None, description="推荐或无法推荐剧集格式的主要原因")
    reasons: list[str] = Field(default_factory=list, description="剧集格式推荐的依据")
    message: Optional[str] = Field(default=None, description="面向调用方的结果说明")
