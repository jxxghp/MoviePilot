from typing import Literal, Optional

from pydantic import BaseModel, Field
from pydantic import model_validator as _model_validator

from app.schemas.types import MediaSource as _MediaSource
from app.schemas.types import MusicEntityType as _MusicEntityType


class DownloadTask(BaseModel):
    """
    下载任务
    """

    download_id: Optional[str] = Field(default=None, description="任务ID")
    downloader: Optional[str] = Field(default=None, description="下载器")
    path: Optional[str] = Field(default=None, description="下载路径")
    completed: Optional[bool] = Field(default=False, description="是否完成")


class DownloadDirectory(BaseModel):
    """
    下载目录
    """

    name: Optional[str] = Field(default=None, description="目录名称")
    storage: Optional[str] = Field(default="local", description="存储类型")
    download_path: Optional[str] = Field(default=None, description="配置的下载目录")
    save_path: Optional[str] = Field(default=None, description="可直接传给下载接口 save_path 的路径")
    priority: Optional[int] = Field(default=0, description="目录优先级")
    media_type: Optional[str] = Field(default=None, description="适用媒体类型")
    media_category: Optional[str] = Field(default=None, description="适用媒体分类")
    media_category_id: Optional[str] = Field(default=None, description="适用媒体分类稳定 ID")


class DownloadAddedData(BaseModel):
    """下载任务添加结果。"""

    download_id: Optional[str] = Field(default=None, description="下载任务 ID")
    requires_confirmation: bool = Field(
        default=False,
        description="是否需要用户确认后下载未识别资源",
    )


class SubtitleDownloadData(BaseModel):
    """字幕下载结果。"""

    files: list[str] = Field(default_factory=list, description="已保存字幕文件列表")


class DownloadTaskUpdateRequest(BaseModel):  # type: ignore[misc]
    """下载任务高级修改请求。"""

    action: Optional[Literal["start", "stop"]] = None
    tags: Optional[list[str]] = None
    downloader: Optional[str] = None
    download_limit: Optional[float] = None
    upload_limit: Optional[float] = None
    trackers: Optional[list[str]] = None
    save_path: Optional[str] = None
    category: Optional[str] = None
    ratio_limit: Optional[float] = None
    seeding_time_limit: Optional[int] = None


class DownloadTaskMutationResult(BaseModel):  # type: ignore[misc]
    """下载任务单个修改动作的执行结果。"""

    operation: str = Field(description="修改动作")
    success: bool = Field(description="动作是否成功")
    message: str = Field(description="动作结果说明")


class DownloadTaskUpdateData(BaseModel):  # type: ignore[misc]
    """一次下载任务高级修改的聚合结果。"""

    hash: str = Field(description="下载任务 Hash")
    downloader: str = Field(description="实际使用的下载器实例")
    results: list[DownloadTaskMutationResult] = Field(default_factory=list, description="各修改动作结果")


class DownloadSourceClassificationRequest(BaseModel):  # type: ignore[misc]
    """已有任务的识别、归类与种子根目录重命名请求。"""

    downloader: Optional[str] = None
    execute: bool = False
    mode: Literal["recognize", "manual"] = "recognize"
    target_path: Optional[str] = None
    type_name: Optional[Literal["电影", "电视剧", "音乐"]] = None
    media_source: Optional[_MediaSource] = None
    media_id: Optional[str] = None
    # 已有任务资源归类额外支持 artist，用于单种子艺术家大合集。
    music_type: Optional[_MusicEntityType] = None
    episode_group: Optional[str] = None
    media_category: Optional[str] = None
    smart_rename: bool = True
    expected_current_path: Optional[str] = None
    expected_target_path: Optional[str] = None
    expected_content_path: Optional[str] = None
    expected_root_name: Optional[str] = None

    @_model_validator(mode="after")  # type: ignore[misc]
    def validate_mode_and_identity(self) -> "DownloadSourceClassificationRequest":
        """手动模式必须给出目录，ID 不能脱离其所属数据源。"""
        if self.mode == "manual" and not str(self.target_path or "").strip():
            raise ValueError("手动指定目录模式必须填写目标路径")
        if self.media_source is None and str(self.media_id or "").strip():
            raise ValueError("填写媒体 ID 时必须选择数据源")
        return self


class DownloadSourceClassificationData(BaseModel):  # type: ignore[misc]
    """识别与资源目录变更计划，包含可审计的执行结果。"""

    hash: str
    downloader: str
    mode: Literal["recognize", "manual"]
    recognized: bool
    media_type: Optional[str] = None
    media_source: Optional[str] = None
    media_id: Optional[str] = None
    title: Optional[str] = None
    year: Optional[str] = None
    current_save_path: str
    target_save_path: str
    current_content_path: Optional[str] = None
    category: Optional[str] = None
    secondary_categories: list[str] = Field(default_factory=list)
    current_root_name: Optional[str] = None
    proposed_root_name: Optional[str] = None
    rename_supported: bool = False
    rename_required: bool = False
    changed: bool
    executed: bool
    relocated: bool = False
    renamed: bool = False
