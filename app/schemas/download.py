from typing import Optional

from pydantic import BaseModel, Field


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


class DownloadAddedData(BaseModel):
    """下载任务添加结果。"""

    download_id: Optional[str] = Field(default=None, description="下载任务 ID")


class SubtitleDownloadData(BaseModel):
    """字幕下载结果。"""

    files: list[str] = Field(default_factory=list, description="已保存字幕文件列表")
