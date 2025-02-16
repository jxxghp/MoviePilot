from typing import Optional

from pydantic import Field

from app.actions import BaseAction
from app.schemas import ActionParams, ActionContext


class SearchTorrentsParams(ActionParams):
    """
    搜索站点资源参数
    """
    name: str = Field(None, description="资源名称")
    year: Optional[int] = Field(None, description="年份")
    type: Optional[str] = Field(None, description="资源类型 (电影/电视剧)")
    season: Optional[int] = Field(None, description="季度")
    recognize: Optional[bool] = Field(False, description="是否识别")


class SearchTorrentsAction(BaseAction):
    """
    搜索站点资源
    """

    @property
    def name(self) -> str:
        return "搜索站点资源"

    @property
    def description(self) -> str:
        return "根据关键字搜索站点种子资源"

    async def execute(self, params: SearchTorrentsParams, context: ActionContext) -> ActionContext:
        pass

    def is_done(self, context: ActionContext) -> bool:
        pass

    def is_success(self, context: ActionContext) -> bool:
        pass
