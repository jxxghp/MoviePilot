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
