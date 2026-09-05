"""搜索 API 输出模型。"""

from typing import Literal, Union

from pydantic import BaseModel, Field

from app.schemas.common import JsonData
from app.schemas.context import Context as _Context
from app.schemas.context import SubtitleInfo
from app.schemas.context import TorrentInfo as TorrentInfo


class SearchLastContextData(BaseModel):
    """上一次搜索的请求参数与结果。"""

    params: dict[str, JsonData] = Field(default_factory=dict)
    results: list[Union[_Context, SubtitleInfo]] = Field(default_factory=list)


class SearchRecommendStatusData(BaseModel):
    """AI 搜索结果推荐任务状态。"""

    status: Literal["disabled", "idle", "running", "completed", "error"]
    results: list[int] = Field(default_factory=list)
