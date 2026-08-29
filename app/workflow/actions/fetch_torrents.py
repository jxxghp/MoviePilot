import random
import time
from typing import List, Optional, cast

from pydantic import Field

from app.chain.media import MediaChain
from app.chain.search.facade import SearchChain
from app.domain.context import Context as SearchContext
from app.runtime.log import logger
from app.runtime.stop import runtime_stop_state
from app.schemas.context import Context as WorkflowContext
from app.schemas.types import MediaType
from app.schemas.workflow import ActionContext, ActionParams
from app.workflow.actions import BaseAction


class FetchTorrentsParams(ActionParams):
    """
    获取站点资源参数
    """
    search_type: Optional[str] = Field(default="keyword", description="搜索类型")
    name: Optional[str] = Field(default=None, description="资源名称")
    year: Optional[str] = Field(default=None, description="年份")
    type: Optional[str] = Field(default=None, description="资源类型 (电影/电视剧)")
    season: Optional[int] = Field(default=None, description="季度")
    sites: Optional[List[int]] = Field(default=[], description="站点列表")
    match_media: Optional[bool] = Field(default=False, description="匹配媒体信息")


class FetchTorrentsAction(BaseAction):
    """
    搜索站点资源
    """

    contract = {
        "inputs": [{"name": "medias", "label": "媒体", "kind": "list"}],
        "outputs": [{"name": "torrents", "label": "资源", "kind": "list"}],
    }

    def __init__(self, action_id: str):
        super().__init__(action_id)
        self._torrents: list[WorkflowContext] = []

    name = "搜索站点资源"
    description = "搜索站点种子资源列表"
    data = FetchTorrentsParams().model_dump()

    @property
    def success(self) -> bool:
        return self.done

    def execute(self, workflow_id: int, params: dict, context: ActionContext) -> ActionContext:
        """
        搜索站点，获取资源列表
        """
        params = FetchTorrentsParams(**params)
        searchchain = SearchChain()
        if params.search_type == "keyword":
            # 按关键字搜索
            torrents = searchchain.search_by_title(
                title=params.name or "",
                sites=params.sites,
            )
            for torrent in torrents:
                if runtime_stop_state.is_workflow_stopped(workflow_id):
                    break
                meta_info = torrent.meta_info
                if params.year and (not meta_info or meta_info.year != params.year):
                    continue
                if params.type and torrent.media_info and torrent.media_info.type != MediaType(params.type):
                    continue
                if params.season is not None and (
                    not meta_info or meta_info.begin_season != params.season
                ):
                    continue
                # 识别媒体信息
                if params.match_media:
                    if not meta_info:
                        continue
                    torrent.media_info = MediaChain().recognize_by_meta(
                        meta_info,
                        obtain_images=False,
                    )
                    if not torrent.media_info:
                        logger.warning(f"{torrent.torrent_info.title} 未识别到媒体信息")
                        continue
                self._torrents.append(self._to_workflow_context(torrent))
        else:
            # 搜索媒体列表
            for media in context.medias or []:
                if runtime_stop_state.is_workflow_stopped(workflow_id):
                    break
                if not media.media_source or not media.media_id or not media.type:
                    logger.warning("媒体身份或类型不完整，跳过资源搜索")
                    continue
                torrents = searchchain.search_by_id(
                    media_source=media.media_source,
                    media_id=media.media_id,
                    mtype=MediaType(media.type),
                    sites=params.sites,
                )
                for torrent in torrents:
                    self._torrents.append(self._to_workflow_context(torrent))

                # 随机休眠 5-30秒
                sleep_time = random.randint(5, 30)
                logger.info(f"随机休眠 {sleep_time} 秒 ...")
                time.sleep(sleep_time)

        if self._torrents:
            if context.torrents is None:
                context.torrents = []
            context.torrents.extend(self._torrents)
            logger.info(f"共搜索到 {len(self._torrents)} 条资源")

        self.job_done(f"搜索到 {len(self._torrents)} 个资源")
        return context

    @staticmethod
    def _to_workflow_context(context: SearchContext) -> WorkflowContext:
        """把领域搜索结果投影为工作流传输上下文。"""
        return cast(WorkflowContext, WorkflowContext.model_validate(context.to_dict()))
