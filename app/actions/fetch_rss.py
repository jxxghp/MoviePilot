from typing import Optional

from pydantic import Field

from app.actions import BaseAction
from app.schemas import ActionParams, ActionContext


class FetchRssParams(ActionParams):
    """
    获取RSS资源列表参数
    """
    url: str = Field(None, description="RSS地址")
    proxy: Optional[bool] = Field(False, description="是否使用代理")
    timeout: Optional[int] = Field(15, description="超时时间")
    headers: Optional[dict] = Field(None, description="请求头")
    recognize: Optional[bool] = Field(False, description="是否识别")


class FetchRssAction(BaseAction):
    """
    获取RSS资源列表
    """

    @property
    def name(self) -> str:
        return "获取RSS资源列表"

    @property
    def description(self) -> str:
        return "请求RSS地址获取数据，并解析为资源列表"

    async def execute(self, params: FetchRssParams, context: ActionContext) -> ActionContext:
        pass

    @property
    def done(self) -> bool:
        return True

    @property
    def success(self) -> bool:
        return True
