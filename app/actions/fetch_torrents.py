import random
import time
from typing import Optional, List

from pydantic import Field

from app.actions import BaseAction
from app.chain.search import SearchChain
from app.core.config import global_vars
from app.log import logger
from app.schemas import ActionParams, ActionContext, MediaType


class FetchTorrentsParams(ActionParams):
    """
    获取站点资源参数
    """
    search_type: Optional[str] = Field("keyword", description="搜索类型")
    name: Optional[str] = Field(None, description="资源名称")
    year: Optional[str] = Field(None, description="年份")
    type: Optional[str] = Field(None, description="资源类型 (电影/电视剧)")
    season: Optional[int] = Field(None, description="季度")
    sites: Optional[List[int]] = Field([], description="站点列表")


class FetchTorrentsAction(BaseAction):
    """
    搜索站点资源
    """

    _torrents = []

    def __init__(self):
        super().__init__()
        self.searchchain = SearchChain()

    @classmethod
    @property
    def name(cls) -> str: # noqa
        return "搜索站点资源"

    @classmethod
    @property
    def description(cls) -> str: # noqa
        return "搜索站点种子资源列表"

    @classmethod
    @property
    def data(cls) -> dict: # noqa
        return FetchTorrentsParams().dict()

    @property
    def success(self) -> bool:
        return self.done

    def execute(self, workflow_id: int, params: dict, context: ActionContext) -> ActionContext:
        """
        搜索站点，获取资源列表
        """
        params = FetchTorrentsParams(**params)
        if params.search_type == "keyword":
            # 按关键字搜索
            torrents = self.searchchain.search_by_title(title=params.name, sites=params.sites, cache_local=False)
            for torrent in torrents:
                if global_vars.is_workflow_stopped(workflow_id):
                    break
                if params.year and torrent.meta_info.year != params.year:
                    continue
                if params.type and torrent.media_info and torrent.media_info.type != MediaType(params.type):
                    continue
                if params.season and torrent.meta_info.begin_season != params.season:
                    continue
                # 识别媒体信息
                torrent.media_info = self.searchchain.recognize_media(torrent.meta_info)
                if not torrent.media_info:
                    logger.warning(f"{torrent.torrent_info.title} 未识别到媒体信息")
                    continue
                self._torrents.append(torrent)
        else:
            # 搜索媒体列表
            for media in context.medias:
                if global_vars.is_workflow_stopped(workflow_id):
                    break
                torrents = self.searchchain.search_by_id(tmdbid=media.tmdb_id,
                                                         doubanid=media.douban_id,
                                                         mtype=MediaType(media.type),
                                                         sites=params.sites)
                for torrent in torrents:
                    self._torrents.append(torrent)

                # 随机休眠 10-60秒
                sleep_time = random.randint(10, 60)
                logger.info(f"随机休眠 {sleep_time} 秒 ...")
                time.sleep(sleep_time)

        if self._torrents:
            context.torrents.extend(self._torrents)
            logger.info(f"共搜索到 {len(self._torrents)} 条资源")

        self.job_done(f"搜索到 {len(self._torrents)} 个资源")
        return context
