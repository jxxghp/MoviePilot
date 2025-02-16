from typing import Optional

from pydantic import BaseModel, Field


class DownloadTask(BaseModel):
    """
     下载任务
    """
    download_id: Optional[str] = Field(None, description="任务ID")
    downloader: Optional[str] = Field(None, description="下载器")
    completed: Optional[bool] = Field(False, description="是否完成")
