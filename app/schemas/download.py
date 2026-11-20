"""
下载任务、下载器状态和源文件分类接口的数据结构。
"""

from enum import Enum as _Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field
from pydantic import model_validator as _model_validator

from app.schemas.types import MediaSource as _MediaSource
from app.schemas.types import MusicEntityType as _MusicEntityType

# 下载器模块 download() 四元组第四项的约定说明。同一 hash 已在下载器中时，内置下载器会先按
# 本次目标核验已有任务（谁暂停的、哪些文件被取消过），能受理才返回 hash，并用以下四个值之一告诉
# 下载链"这次对已有任务做了什么"；不能受理时不返回 hash，说明以 ``ExistingTaskRejection`` 开头。
# 插件提供的下载器模块不返回这些值时，下载链按原有"新建任务"语义结算。
#
# 已有任务原样复用：目标文件此前已选中，且已下载完成或任务正在运行，下载器状态未做任何改动。
DOWNLOAD_TASK_ALREADY_EXISTS = "下载任务已存在"
# 已有任务追加了本次目标文件（必要时一并恢复了以“可恢复”意图暂停的任务），下载链按新增下载发送通知。
DOWNLOAD_TASK_FILES_APPENDED = "下载任务已存在，已追加选择本次目标文件"
# 已有任务的目标文件此前已选中，任务以“可恢复”意图暂停，本次已恢复下载，没有新增文件。
DOWNLOAD_TASK_RESUMED = "下载任务已存在，已恢复以“可恢复”意图暂停的任务"
# 已有任务以"暂缓"意图暂停（不论用户还是插件）：MP 不恢复（由暂停方负责恢复），但目标文件已选中，视为已受理。
DOWNLOAD_TASK_DEFERRED = "下载任务已存在，任务已被暂缓，等待暂停方恢复后继续下载"


class DownloadTaskAction(str, _Enum):
    """
    下载器受理一次提交时对任务实际做了什么，供下载历史结算与 DownloadAdded 事件区分。

    同一个种子可以先后承载多次不同的下载请求（例如同一季包分几次下载不同集），
    "任务已存在"不再等于"本次请求已满足"，因此需要区分以下几种结果。
    """

    # 新建下载任务
    CREATED = "created"
    # 已有任务追加选择了本次目标文件
    APPENDED = "appended"
    # 已有任务的目标文件已选中，本次恢复了以“可恢复”意图暂停的任务
    RESUMED = "resumed"
    # 已有任务原样复用，未做任何改动
    REUSED = "reused"
    # 已有任务以“暂缓”意图暂停：目标文件已选中（可能本次追加），任务保持暂停，等暂停方自己恢复
    DEFERRED = "deferred"

    @classmethod
    def from_downloader_message(cls, message: Optional[str]) -> "DownloadTaskAction":
        """
        按下载器返回的说明识别本次对任务的动作。

        :param message: 下载器 download() 返回的第四项说明；仅在已返回 hash 时有意义
        :return: 已有任务的四种约定说明分别映射为追加、恢复、暂缓和复用，其余说明视为新建任务
        """
        return {
            DOWNLOAD_TASK_FILES_APPENDED: cls.APPENDED,
            DOWNLOAD_TASK_RESUMED: cls.RESUMED,
            DOWNLOAD_TASK_DEFERRED: cls.DEFERRED,
            DOWNLOAD_TASK_ALREADY_EXISTS: cls.REUSED,
        }.get(message or "", cls.CREATED)

    @property
    def is_existing_task(self) -> bool:
        """
        是否复用了下载器中已有的同一 hash 任务；为 True 时文件明细需要按路径去重。
        """
        return self is not DownloadTaskAction.CREATED


class DownloadTaskActor(str, _Enum):
    """
    谁暂停了下载任务；只用于记录与展示，不决定 MP 之后怎么处理这个任务。

    MP 之后怎么处理只看暂停意图（``DownloadPauseIntent``）：同样是"放弃"，用户和插件得到的结果一样；
    插件想让 MP 换种子就声明放弃，不需要冒充用户。来源写在 ``MP暂停:…`` 标签里，方便用户在下载器和
    MP 的提示里知道是谁暂停的（格式见 ``app.domain.torrent.pause_tag``）。
    """

    # MoviePilot 系统流程（目前宿主没有以系统身份暂停已有任务的流程，保留给以后的内部流程）
    SYSTEM = "system"
    # 用户在 MoviePilot 中手动操作（Web 界面、接口，或智能助手代用户操作）
    USER = "user"
    # 插件通过 MoviePilot 接口操作，需要同时给出插件 ID
    PLUGIN = "plugin"
    # 只用于识别结果：任务已暂停但没有 MP 标签（下载器里手动、第三方工具、直接操作客户端的插件）
    EXTERNAL = "external"


class DownloadPauseIntent(str, _Enum):
    """
    暂停下载任务时声明的业务意图，决定订阅之后遇到这个任务时怎么办；与谁暂停无关。

    基本原则：MoviePilot 只按暂停方声明的意图行事，不推翻用户、插件或外部做出的决定。
    没有 MP 暂停标签的暂停（外部暂停）不知道意图，MP 不恢复、不改动，短冷却后再看。
    """

    # 放弃该资源（例如用户不想要了、判定为死种或假种）：MP 不恢复、不再复用，订阅改用其他种子
    ABANDON = "abandon"
    # 暂缓（例如分时段下载、磁盘空间不足）：MP 不恢复，但仍视为该资源在下载中，由暂停方负责恢复
    DEFER = "defer"
    # 可恢复（例如为节省资源临时停下，谁需要谁恢复）：订阅需要这个任务继续下载时，MP 可以直接恢复
    RESUMABLE = "resumable"


class ExistingTaskRejection(str, _Enum):
    """
    同一 hash 已在下载器中、但不能受理本次请求的原因；枚举值同时是失败说明的固定开头。

    下载器模块返回 ``f"{原因}（任务 {hash}）：{细节}"``，下载链用 ``from_message`` 识别原因并决定
    冷却时长，不再从自由文本里猜关键字。新增原因时同时确认 ``is_resource_level`` 的归类。
    """

    # 读取不到文件列表（磁力任务尚无元数据，或下载器查询失败）
    FILES_UNAVAILABLE = "已有任务的文件列表暂时无法读取"
    # 文件列表里找不到能完整覆盖目标集的视频
    EPISODES_NOT_FOUND = "已有任务中未找到目标集数"
    # 目标主文件（视频；没有视频的种子为全部文件）此前经 MP 确认选中过，现在被取消选择（只可能是用户、插件或外部所为）
    FILES_CANCELLED = "已有任务的目标文件已被取消选择"
    # 任务被以"放弃"意图暂停（不论是用户还是插件）
    PAUSED_ABANDONED = "已有任务已被暂停并放弃"
    # 任务已暂停但没有 MP 暂停标签，无法确认是谁、为什么暂停
    PAUSED_EXTERNALLY = "已有任务在下载器中被暂停"
    # 下载器报告任务出错（例如数据文件缺失）
    TASK_ERROR = "已有任务处于错误状态"
    # 读取任务状态或标签失败，无法判断能否继续下载
    STATE_UNAVAILABLE = "已有任务的状态暂时无法读取"
    # 追加选择、刷新优先级、恢复下载或移除"已整理"标签后回读未确认
    NOT_CONFIRMED = "已有任务的修改未能确认"

    @property
    def is_resource_level(self) -> bool:
        """
        原因是否属于"这个资源当前用不了"，需要较长冷却并让订阅换种子。

        用户/插件放弃、文件被取消、集数对不上、任务出错，在有人处理之前反复重试都会得到同样结果；
        其余原因（读取失败、外部暂停、写入未确认）可能很快恢复，按瞬时失败短冷却。
        外部暂停按瞬时处理：用户在下载器里恢复后，下次就能接着使用该任务。
        """
        return self in (
            ExistingTaskRejection.EPISODES_NOT_FOUND,
            ExistingTaskRejection.FILES_CANCELLED,
            ExistingTaskRejection.PAUSED_ABANDONED,
            ExistingTaskRejection.TASK_ERROR,
        )

    @classmethod
    def from_message(cls, message: Optional[str]) -> Optional["ExistingTaskRejection"]:
        """
        从下载器返回的失败说明识别已有任务的拒绝原因。

        :param message: 下载器 download() 返回的第四项说明
        :return: 说明以某个原因开头时返回该原因，否则返回 None
        """
        text = message or ""
        return next((item for item in cls if text.startswith(item.value)), None)

    def describe(self, torrent_hash: str, detail: str) -> str:
        """
        生成以原因开头的失败说明，供下载器模块返回。

        :param torrent_hash: 已有任务的 hash
        :param detail: 面向用户的细节与处理建议
        :return: ``from_message`` 能识别的完整说明
        """
        return f"{self.value}（任务 {torrent_hash}）：{detail}"


class DownloadTask(BaseModel):
    """
    下载任务。
    """

    download_id: Optional[str] = Field(default=None, description="任务ID")
    downloader: Optional[str] = Field(default=None, description="下载器")
    path: Optional[str] = Field(default=None, description="下载路径")
    completed: Optional[bool] = Field(default=False, description="是否完成")


class DownloadDirectory(BaseModel):
    """
    下载目录。
    """

    name: Optional[str] = Field(default=None, description="目录名称（别名），可直接作为 save_path 提交")
    storage: Optional[str] = Field(default="local", description="存储类型")
    download_path: Optional[str] = Field(default=None, description="配置的下载目录")
    save_path: Optional[str] = Field(default=None, description="可直接传给下载接口 save_path 的路径")
    priority: Optional[int] = Field(default=0, description="目录优先级")
    media_type: Optional[str] = Field(default=None, description="适用媒体类型")
    media_category: Optional[str] = Field(default=None, description="适用媒体分类")
    media_category_id: Optional[str] = Field(default=None, description="适用媒体分类稳定 ID")


class DownloadAddedData(BaseModel):
    """
    下载任务添加结果。
    """

    download_id: Optional[str] = Field(default=None, description="下载任务 ID")
    requires_confirmation: bool = Field(
        default=False,
        description="是否需要用户确认后下载未识别资源",
    )


class SubtitleDownloadData(BaseModel):
    """
    字幕下载结果。
    """

    files: list[str] = Field(default_factory=list, description="已保存字幕文件列表")


class DownloadTaskUpdateRequest(BaseModel):
    """
    下载任务高级修改请求。
    """

    action: Optional[Literal["start", "stop"]] = Field(default=None, description="任务控制动作：start 启动，stop 暂停；省略时不修改运行状态")
    tags: Optional[list[str]] = Field(default=None, description="标签列表")
    downloader: Optional[str] = Field(default=None, description="下载器")
    download_limit: Optional[float] = Field(default=None, description="下载速度上限，单位为 KB/s，0 表示不限速")
    upload_limit: Optional[float] = Field(default=None, description="上传速度上限，单位为 KB/s，0 表示不限速")
    trackers: Optional[list[str]] = Field(default=None, description="种子的 Tracker 地址列表")
    save_path: Optional[str] = Field(default=None, description="保存路径")
    category: Optional[str] = Field(default=None, description="下载器中的任务分类")
    ratio_limit: Optional[float] = Field(default=None, description="做种分享率上限")
    seeding_time_limit: Optional[int] = Field(default=None, description="做种时间限制，单位为分钟；具体支持范围由下载器决定")


class DownloadTaskMutationResult(BaseModel):
    """
    下载任务单个修改动作的执行结果。
    """

    operation: str = Field(description="修改动作")
    success: bool = Field(description="动作是否成功")
    message: str = Field(description="动作结果说明")


class DownloadTaskUpdateData(BaseModel):
    """
    一次下载任务高级修改的聚合结果。
    """

    hash: str = Field(description="下载任务 Hash")
    downloader: str = Field(description="实际使用的下载器实例")
    results: list[DownloadTaskMutationResult] = Field(default_factory=list, description="各修改动作结果")


class DownloadSourceClassificationRequest(BaseModel):
    """
    已有任务的识别、归类与种子根目录重命名请求。
    """

    downloader: Optional[str] = Field(default=None, description="下载器")
    execute: bool = Field(default=False, description="是否实际执行路径调整")
    mode: Literal["recognize", "manual"] = Field(default="recognize", description="recognize 按识别结果归类，manual 使用显式指定的目录")
    target_path: Optional[str] = Field(default=None, description="目标路径")
    type_name: Optional[Literal["电影", "电视剧", "音乐"]] = Field(default=None, description="类型")
    media_source: Optional[_MediaSource] = Field(default=None, description="媒体数据源")
    media_id: Optional[str] = Field(default=None, description="数据源原生 ID")
    music_type: Optional[_MusicEntityType] = Field(default=None, description="已有任务资源归类额外支持 artist，用于单种子艺术家大合集")
    episode_group: Optional[str] = Field(default=None, description="剧集组")
    media_category: Optional[str] = Field(default=None, description="自定义媒体类别兼容路径快照")
    smart_rename: bool = Field(default=True, description="是否按识别结果智能重命名")
    expected_current_path: Optional[str] = Field(default=None, description="执行前预期的当前路径，用于检测状态变化")
    expected_target_path: Optional[str] = Field(default=None, description="执行前预期的目标路径")
    expected_content_path: Optional[str] = Field(default=None, description="执行前预期的内容路径，用于检测状态变化")
    expected_root_name: Optional[str] = Field(default=None, description="执行前预期的根目录名称")

    @_model_validator(mode="after")
    def validate_mode_and_identity(self) -> "DownloadSourceClassificationRequest":
        """
        手动模式必须给出目录，ID 不能脱离其所属数据源。
        """
        if self.mode == "manual" and not str(self.target_path or "").strip():
            raise ValueError("手动指定目录模式必须填写目标路径")
        if self.media_source is None and str(self.media_id or "").strip():
            raise ValueError("填写媒体 ID 时必须选择数据源")
        return self


class DownloadSourceClassificationData(BaseModel):
    """
    识别与资源目录变更计划，包含可审计的执行结果。
    """

    hash: str = Field(description="下载任务 hash")
    downloader: str = Field(description="下载器")
    mode: Literal["recognize", "manual"] = Field(description="本次采用的识别归类或手动指定目录模式")
    recognized: bool = Field(description="是否成功识别出媒体身份")
    media_type: Optional[str] = Field(default=None, description="识别出的媒体类型")
    media_source: Optional[str] = Field(default=None, description="媒体数据源")
    media_id: Optional[str] = Field(default=None, description="数据源原生 ID")
    title: Optional[str] = Field(default=None, description="标题")
    year: Optional[str] = Field(default=None, description="识别出的媒体发行年份")
    current_save_path: str = Field(description="下载任务当前保存路径")
    target_save_path: str = Field(description="分类后使用的下载保存目录")
    current_content_path: Optional[str] = Field(default=None, description="下载任务当前内容路径")
    category: Optional[str] = Field(default=None, description="媒体分类")
    secondary_categories: list[str] = Field(default_factory=list, description="分类规则匹配的附加类别")
    current_root_name: Optional[str] = Field(default=None, description="下载任务当前根目录名称")
    proposed_root_name: Optional[str] = Field(default=None, description="建议使用的根目录名称")
    rename_supported: bool = Field(default=False, description="下载器是否支持重命名根目录")
    rename_required: bool = Field(default=False, description="是否需要重命名下载根目录")
    changed: bool = Field(description="分类结果是否发生变化")
    executed: bool = Field(description="是否已经执行路径调整")
    relocated: bool = Field(default=False, description="是否已经调整下载保存位置")
    renamed: bool = Field(default=False, description="是否已经完成重命名")
