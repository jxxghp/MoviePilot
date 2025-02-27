from typing import List

from pydantic import Field

from app.actions import BaseAction
from app.schemas import ActionParams, ActionContext
from app.core.config import settings
from app.core.event import eventmanager
from app.log import logger
from app.schemas import RecommendSourceEventData, MediaInfo
from app.schemas.types import ChainEventType
from app.utils.http import RequestUtils


class FetchMediasParams(ActionParams):
    """
    获取媒体数据参数
    """
    sources: List[str] = Field([], description="媒体数据来源")


class FetchMediasAction(BaseAction):
    """
    获取媒体数据
    """

    __inner_sources = [
        {
            "api_path": 'recommend/tmdb_trending',
            "name": '流行趋势',
        },
        {
            "api_path": 'recommend/douban_showing',
            "name": '正在热映',
        },
        {
            "api_path": 'bangumi/calendar',
            "name": 'Bangumi每日放送',
        },
        {
            "api_path": 'recommend/tmdb_movies',
            "name": 'TMDB热门电影',
        },
        {
            "api_path": 'recommend/tmdb_tvs?with_original_language=zh|en|ja|ko',
            "name": 'TMDB热门电视剧',
        },
        {
            "api_path": 'recommend/douban_movie_hot',
            "name": '豆瓣热门电影',
        },
        {
            "api_path": 'recommend/douban_tv_hot',
            "name": '豆瓣热门电视剧',
        },
        {
            "api_path": 'recommend/douban_tv_animation',
            "name": '豆瓣热门动漫',
        },
        {
            "api_path": 'recommend/douban_movies',
            "name": '豆瓣最新电影',
        },
        {
            "api_path": 'recommend/douban_tvs',
            "name": '豆瓣最新电视剧',
        },
        {
            "api_path": 'recommend/douban_movie_top250',
            "name": '豆瓣电影TOP250',
        },
        {
            "api_path": 'recommend/douban_tv_weekly_chinese',
            "name": '豆瓣国产剧集榜',
        },
        {
            "api_path": 'recommend/douban_tv_weekly_global',
            "name": '豆瓣全球剧集榜',
        }
    ]

    __medias = []

    def __init__(self):
        super().__init__()
        # 广播事件，请示额外的推荐数据源支持
        event_data = RecommendSourceEventData()
        event = eventmanager.send_event(ChainEventType.RecommendSource, event_data)
        # 使用事件返回的上下文数据
        if event and event.event_data:
            event_data: RecommendSourceEventData = event.event_data
            if event_data.extra_sources:
                self.__inner_sources.extend([s.dict() for s in event_data.extra_sources])

    @property
    def name(self) -> str:
        return "获取媒体数据"

    @property
    def description(self) -> str:
        return "获取榜单等媒体数据列表"

    @property
    def data(self) -> dict:
        return FetchMediasParams().dict()

    @property
    def success(self) -> bool:
        return True if self.__medias else False

    def __get_source(self, source: str):
        """
        获取数据源
        """
        for s in self.__inner_sources:
            if s['name'] == source:
                return s
        return None

    def execute(self, params: dict, context: ActionContext) -> ActionContext:
        """
        获取媒体数据，填充到medias
        """
        params = FetchMediasParams(**params)
        for name in params.sources:
            source = self.__get_source(name)
            if not source:
                continue
            logger.info(f"获取媒体数据 {source} ...")
            # 调用内部API获取数据
            api_url = f"http://127.0.0.1:{settings.PORT}/api/v1/{source['api_path']}?token={settings.API_TOKEN}"
            res = RequestUtils(timeout=15).post_res(api_url)
            if res:
                results = res.json()
                logger.info(f"{name} 获取到 {len(results)} 条数据")
                self.__medias.extend([MediaInfo(**r) for r in results])
            else:
                logger.error(f"{name} 获取数据失败")

        if self.__medias:
            context.medias.extend(self.__medias)

        self.job_done()
        return context
