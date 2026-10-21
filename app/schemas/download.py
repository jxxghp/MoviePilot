"""
下载任务、下载器状态和源文件分类接口的数据结构。
"""

from typing import Literal, Optional

from pydantic import BaseModel, Field
from pydantic import model_validator as _model_validator

from app.schemas.types import MediaSource as _MediaSource
from app.schemas.types import MusicEntityType as _MusicEntityType

# 下载器报告同一 hash 的任务已存在时返回的统一说明，订阅链路据此区分复用与新增提交。
DOWNLOAD_TASK_ALREADY_EXISTS = "下载任务已存在"


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
