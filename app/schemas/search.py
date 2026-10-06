"""搜索 API 输出模型。"""

from typing import Literal, Optional, Union

from pydantic import BaseModel, Field

from app.schemas.common import JsonData
from app.schemas.context import Context as _Context
from app.schemas.context import SubtitleInfo
from app.schemas.context import TorrentInfo as TorrentInfo


class SearchSourcePage(BaseModel):  # type: ignore[misc]
    """单页 SSE 的来源结果；客户端持有页号并回传 source，原始页摘要由后端缓存。"""

    # 不透明的来源标识，客户端不解析
    source: str
    site_name: Optional[str] = None
    # 本次请求的页号（从 0 开始）
    page: int
    # 是否允许继续请求（成功后翻页、失败后重试）；不保证下一页一定有资源
    can_continue: bool
    # 本页请求失败的原因
    error: Optional[str] = None


class SearchLastContextData(BaseModel):
    """上一次搜索的请求参数与结果。"""

    params: dict[str, JsonData] = Field(default_factory=dict)
    results: list[Union[_Context, SubtitleInfo]] = Field(default_factory=list)


class SearchRecommendStatusData(BaseModel):
    """AI 搜索结果推荐任务状态。"""

    status: Literal["disabled", "idle", "running", "completed", "error"]
    results: list[int] = Field(default_factory=list)
