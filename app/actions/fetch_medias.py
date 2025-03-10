from typing import List, Optional

from pydantic import Field

from app.actions import BaseAction
from app.chain.recommend import RecommendChain
from app.schemas import ActionParams, ActionContext
from app.core.config import settings, global_vars
from app.core.event import eventmanager
from app.log import logger
from app.schemas import RecommendSourceEventData, MediaInfo
from app.schemas.types import ChainEventType
from app.utils.http import RequestUtils


class FetchMediasParams(ActionParams):
    """
    获取媒体数据参数
    """
    source_type: Optional[str] = Field(default="ranking", description="来源")
    sources: Optional[List[str]] = Field(default=[], description="榜单")
    api_path: Optional[str] = Field(default=None, description="API路径")


class FetchMediasAction(BaseAction):
    """
    获取媒体数据
    """

    _inner_sources = []
    _medias = []
    _has_error = False

    def __init__(self, action_id: str):
        super().__init__(action_id)

        self._medias = []
        self._has_error = False
        self.__inner_sources = [
            {
                "func": RecommendChain().tmdb_trending,
                "name": '流行趋势',
            },
            {
                "func": RecommendChain().douban_movie_showing,
                "name": '正在热映',
            },
            {
                "func": RecommendChain().bangumi_calendar,
                "name": 'Bangumi每日放送',
            },
            {
                "func": RecommendChain().tmdb_movies,
                "name": 'TMDB热门电影',
            },
            {
                "func": RecommendChain().tmdb_tvs,
                "name": 'TMDB热门电视剧',
            },
            {
                "func": RecommendChain().douban_movie_hot,
                "name": '豆瓣热门电影',
            },
            {
                "func": RecommendChain().douban_tv_hot,
                "name": '豆瓣热门电视剧',
            },
            {
                "func": RecommendChain().douban_tv_animation,
                "name": '豆瓣热门动漫',
            },
            {
                "func": RecommendChain().douban_movies,
                "name": '豆瓣最新电影',
            },
            {
                "func": RecommendChain().douban_tvs,
                "name": '豆瓣最新电视剧',
            },
            {
                "func": RecommendChain().douban_movie_top250,
                "name": '豆瓣电影TOP250',
            },
            {
                "func": RecommendChain().douban_tv_weekly_chinese,
                "name": '豆瓣国产剧集榜',
            },
            {
                "func": RecommendChain().douban_tv_weekly_global,
                "name": '豆瓣全球剧集榜',
            }
        ]

        # 广播事件，请示额外的推荐数据源支持
        event_data = RecommendSourceEventData()
        event = eventmanager.send_event(ChainEventType.RecommendSource, event_data)
        # 使用事件返回的上下文数据
        if event and event.event_data:
            event_data: RecommendSourceEventData = event.event_data
            if event_data.extra_sources:
                self.__inner_sources.extend([s.dict() for s in event_data.extra_sources])

    @classmethod
    @property
    def name(cls) -> str: # noqa
        return "获取媒体数据"

    @classmethod
    @property
    def description(cls) -> str: # noqa
        return "获取榜单等媒体数据列表"

    @classmethod
    @property
    def data(cls) -> dict: # noqa
        return FetchMediasParams().dict()

    @property
    def success(self) -> bool:
        return not self._has_error

    def __get_source(self, source: str):
        """
        获取数据源
        """
        for s in self.__inner_sources:
            if s['name'] == source:
                return s
        return None

    def execute(self, workflow_id: int, params: dict, context: ActionContext) -> ActionContext:
        """
        获取媒体数据，填充到medias
        """
        params = FetchMediasParams(**params)
        try:
            if params.source_type == "ranking":
                for name in params.sources:
                    if global_vars.is_workflow_stopped(workflow_id):
                        break
                    source = self.__get_source(name)
                    if not source:
                        continue
                    logger.info(f"获取媒体数据 {source} ...")
                    results = []
                    if source.get("func"):
                        results = source['func']()
                    else:
                        # 调用内部API获取数据
                        api_url = f"http://127.0.0.1:{settings.PORT}/api/v1/{source['api_path']}?token={settings.API_TOKEN}"
                        res = RequestUtils(timeout=15).post_res(api_url)
                        if res:
                            results = res.json()
                    if results:
                        logger.info(f"{name} 获取到 {len(results)} 条数据")
                        self._medias.extend([MediaInfo(**r) for r in results])
                    else:
                        logger.error(f"{name} 获取数据失败")
            else:
                # 调用内部API获取数据
                api_url = f"http://127.0.0.1:{settings.PORT}{params.api_path}?token={settings.API_TOKEN}"
                res = RequestUtils(timeout=15).post_res(api_url)
                if res:
                    results = res.json()
                    if results:
                        logger.info(f"{params.api_path} 获取到 {len(results)} 条数据")
                        self._medias.extend([MediaInfo(**r) for r in results])
        except Exception as e:
            logger.error(f"获取媒体数据失败: {e}")
            self._has_error = True

        if self._medias:
            context.medias.extend(self._medias)

        self.job_done(f"获取到 {len(self._medias)} 条媒数据")
        return context
