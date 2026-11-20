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
# 本次目标核验已有任务（是否暂停、哪些文件被取消过），能受理才返回 hash，并用以下两个值之一告诉
# 下载链"这次对已有任务做了什么"；不能受理时不返回 hash，说明以 ``ExistingTaskRejection`` 开头。
# 插件提供的下载器模块不返回这些值时，下载链按原有"新建任务"语义结算。
#
# 已有任务原样复用：目标文件此前已选中，且已下载完成或任务正在运行，下载器状态未做任何改动。
DOWNLOAD_TASK_ALREADY_EXISTS = "下载任务已存在"
# 已有任务追加了本次目标文件，下载链按新增下载发送通知。
DOWNLOAD_TASK_FILES_APPENDED = "下载任务已存在，已追加选择本次目标文件"


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
    # 已有任务原样复用，未做任何改动
    REUSED = "reused"

    @classmethod
    def from_downloader_message(cls, message: Optional[str]) -> "DownloadTaskAction":
        """
        按下载器返回的说明识别本次对任务的动作。

        :param message: 下载器 download() 返回的第四项说明；仅在已返回 hash 时有意义
        :return: 已有任务的两种约定说明分别映射为追加和复用，其余说明视为新建任务
        """
        return {
            DOWNLOAD_TASK_FILES_APPENDED: cls.APPENDED,
            DOWNLOAD_TASK_ALREADY_EXISTS: cls.REUSED,
        }.get(message or "", cls.CREATED)

    @property
    def is_existing_task(self) -> bool:
        """
        是否复用了下载器中已有的同一 hash 任务；为 True 时文件明细需要按路径去重。
        """
        return self is not DownloadTaskAction.CREATED


class ExistingTaskRejection(str, _Enum):
    """
    同一 hash 已在下载器中、但不能受理本次请求的原因；枚举值同时是失败说明的固定开头。

    下载器模块返回 ``f"{原因}（任务 {hash}）：{细节}"``，下载链用 ``from_message`` 识别原因，决定通知去重的
    范围与时长、批量下载本批内记失败的范围，不再从自由文本里猜关键字。已有任务的拒绝不会让订阅跳过候选，
    每一轮都按下载器实时状态重新判断。新增原因时同时确认 ``is_resource_level`` 与 ``is_target_specific`` 的归类。
    """

    # 读取不到文件列表（磁力任务尚无元数据，或下载器查询失败）
    FILES_UNAVAILABLE = "已有任务的文件列表暂时无法读取"
    # 文件列表里找不到能完整覆盖目标集的视频
    EPISODES_NOT_FOUND = "已有任务中未找到目标集数"
    # 目标主文件（视频；没有视频的种子为全部文件）此前经 MP 确认选中过，现在被取消选择（只可能是用户、插件或外部所为）
    FILES_CANCELLED = "已有任务的目标文件已被取消选择"
    # 任务已暂停且目标尚未下载完成；不论是谁暂停的，MP 都不恢复
    PAUSED = "已有任务已暂停"
    # 下载器报告任务出错（例如数据文件缺失）
    TASK_ERROR = "已有任务处于错误状态"
    # 读取任务状态失败，无法判断能否继续下载
    STATE_UNAVAILABLE = "已有任务的状态暂时无法读取"
    # 追加选择、刷新优先级或移除"已整理"标签后回读未确认
    NOT_CONFIRMED = "已有任务的修改未能确认"

    @property
    def is_resource_level(self) -> bool:
        """
        原因是否属于"在有人处理之前反复重试都会得到同样结果"，同一原因的通知静默期较长（24 小时）。

        目标文件被取消、集数对不上、任务出错属于这一类；其余原因（读取失败、暂停、写入未确认）可能
        很快恢复，静默期较短（1 小时）。静默期只决定多久后再通知一次，不影响下一轮是否重新判断。
        """
        return self in (
            ExistingTaskRejection.EPISODES_NOT_FOUND,
            ExistingTaskRejection.FILES_CANCELLED,
            ExistingTaskRejection.TASK_ERROR,
        )

    @property
    def is_target_specific(self) -> bool:
        """
        原因是否只针对本次请求的目标集，而不是整个任务。

        目标集找不到、目标文件被取消只说明"这几集"在这个任务上不能用，同一任务上的其他集仍可正常追加，
        通知去重与批量下载本批内的失败记录只作用于"这台下载器 + 这个种子 + 这几集"；其余原因（暂停、出错、
        读取或写入失败）对任务上的任何请求都一样，作用于"这台下载器 + 这个种子"。
        """
        return self in (ExistingTaskRejection.EPISODES_NOT_FOUND, ExistingTaskRejection.FILES_CANCELLED)

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
