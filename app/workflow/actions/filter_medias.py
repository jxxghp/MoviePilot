"""
根据动作参数筛选工作流中的媒体列表。
"""

from typing import Optional

from pydantic import Field

from app.runtime.log import logger
from app.runtime.stop import runtime_stop_state
from app.schemas.context import MediaInfo
from app.schemas.workflow import ActionContext, ActionParams
from app.workflow.actions import BaseAction


class FilterMediasParams(ActionParams):
    """
    过滤媒体数据参数
    """
    type: Optional[str] = Field(default=None, description="媒体类型 (电影/电视剧)")
    vote: Optional[float] = Field(default=None, description="评分（支持小数）")
    year: Optional[str] = Field(default=None, description="年份")


class FilterMediasAction(BaseAction):
    """
    过滤媒体数据
    """

    contract = {
        "inputs": [{"name": "medias", "label": "媒体", "kind": "list"}],
        "outputs": [{"name": "medias", "label": "媒体", "kind": "list", "merge": "replace"}],
    }

    def __init__(self, action_id: str) -> None:
        """
        初始化当前动作独立的执行状态。
        """
        super().__init__(action_id)
        self._medias: list[MediaInfo] = []

    name = "过滤媒体数据"
    description = "对媒体数据列表进行过滤"
    data = FilterMediasParams().model_dump()

    @property
    def success(self) -> bool:
        """
        返回当前动作是否已达到完成条件。
        """
        return self.done

    def execute(self, workflow_id: int, params: dict[str, object], context: ActionContext) -> ActionContext:
        """
        过滤medias中媒体数据
        """
        action_params = FilterMediasParams.model_validate(params)
        for media in context.medias or []:
            if runtime_stop_state.is_workflow_stopped(workflow_id):
                break
            if action_params.type and media.type != action_params.type:
                continue
            if action_params.vote is not None and (
                    media.vote_average is None or media.vote_average < action_params.vote
            ):
                continue
            if action_params.year and media.year != action_params.year:
                continue
            self._medias.append(media)

        logger.info(f"过滤后剩余 {len(self._medias)} 条媒体数据")

        context.medias = self._medias

        self.job_done(f"过滤后剩余 {len(self._medias)} 条媒体数据")
        return context
