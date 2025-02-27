from typing import Optional

from pydantic import Field

from app.actions import BaseAction
from app.schemas import ActionParams, ActionContext
from app.schemas.types import MediaType


class FilterMediasParams(ActionParams):
    """
    过滤媒体数据参数
    """
    type: Optional[str] = Field(None, description="媒体类型 (电影/电视剧)")
    category: Optional[str] = Field(None, description="媒体类别 (二级分类)")
    vote: Optional[int] = Field(0, description="评分")
    year: Optional[str] = Field(None, description="年份")


class FilterMediasAction(BaseAction):
    """
    过滤媒体数据
    """

    __medias = []

    @property
    def name(self) -> str:
        return "过滤媒体数据"

    @property
    def description(self) -> str:
        return "对媒体数据列表进行过滤"

    @property
    def data(self) -> dict:
        return FilterMediasParams().dict()

    @property
    def success(self) -> bool:
        return True if self.__medias else False

    async def execute(self, params: FilterMediasParams, context: ActionContext) -> ActionContext:
        """
        过滤medias中媒体数据
        """
        for media in context.medias:
            if params.type and media.type != MediaType(params.type):
                continue
            if params.category and media.category != params.category:
                continue
            if params.vote and media.vote_average < params.vote:
                continue
            if params.year and media.year != params.year:
                continue
            self.__medias.append(media)

        if self.__medias:
            context.medias = self.__medias

        self.job_done()
        return context
