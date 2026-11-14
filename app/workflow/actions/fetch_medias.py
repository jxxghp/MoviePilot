"""
从配置来源获取媒体列表并写入工作流上下文。
"""

from typing import Any, List, Optional

from pydantic import Field

from app.adapters.network.http import RequestUtils
from app.application.configuration import get_chain_runtime_config_snapshot
from app.chain.recommend import RecommendChain
from app.runtime.events import eventmanager
from app.runtime.log import logger
from app.runtime.stop import runtime_stop_state
from app.schemas.context import MediaInfo
from app.schemas.event import RecommendSourceEventData
from app.schemas.types import ChainEventType
from app.schemas.workflow import ActionContext, ActionParams
from app.workflow.actions import BaseAction


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

    contract = {
        "outputs": [{"name": "medias", "label": "媒体", "kind": "list"}],
    }

    def __init__(self, action_id: str):
        """
        初始化获取媒体动作及本次执行的媒体结果列表。
        """
        super().__init__(action_id)

        self._medias: list[MediaInfo] = []
        self._has_error = False
        self.__inner_sources = [
            {
                "func": RecommendChain().tmdb_trending,
                "name": '流行趋势',
                "api_path": "recommend/tmdb_trending"
            },
            {
                "func": RecommendChain().douban_movie_showing,
                "name": '正在热映',
                "api_path": "recommend/douban_showing"
            },
            {
                "func": RecommendChain().bangumi_calendar,
                "name": 'Bangumi每日放送',
                "api_path": "recommend/bangumi_calendar"
            },
            {
                "func": RecommendChain().tmdb_movies,
                "name": 'TMDB热门电影',
                "api_path": "recommend/tmdb_movies"
            },
            {
                "func": RecommendChain().tmdb_tvs,
                "name": 'TMDB热门电视剧',
                "api_path": "recommend/tmdb_tvs?with_original_language=zh|en|ja|ko"
            },
            {
                "func": RecommendChain().douban_movie_hot,
                "name": '豆瓣热门电影',
                "api_path": "recommend/douban_movie_hot"
            },
            {
                "func": RecommendChain().douban_tv_hot,
                "name": '豆瓣热门电视剧',
                "api_path": "recommend/douban_tv_hot"
            },
            {
                "func": RecommendChain().douban_tv_animation,
                "name": '豆瓣热门动漫',
                "api_path": "recommend/douban_tv_animation"
            },
            {
                "func": RecommendChain().douban_movies,
                "name": '豆瓣最新电影',
                "api_path": "recommend/douban_movies"
            },
            {
                "func": RecommendChain().douban_tvs,
                "name": '豆瓣最新电视剧',
                "api_path": "recommend/douban_tvs"
            },
            {
                "func": RecommendChain().douban_movie_top250,
                "name": '豆瓣电影TOP250',
                "api_path": "recommend/douban_movie_top250"
            },
            {
                "func": RecommendChain().douban_tv_weekly_chinese,
                "name": '豆瓣国产剧集榜',
                "api_path": "recommend/douban_tv_weekly_chinese"
            },
            {
                "func": RecommendChain().douban_tv_weekly_global,
                "name": '豆瓣全球剧集榜',
                "api_path": "recommend/douban_tv_weekly_global"
            }
        ]

        # 广播事件，请示额外的推荐数据源支持
        event_data = RecommendSourceEventData()
        event = eventmanager.send_event(ChainEventType.RecommendSource, event_data)
        # 使用事件返回的上下文数据
        if event and event.event_data:
            source_data = event.event_data
            if not isinstance(source_data, RecommendSourceEventData):
                raise AttributeError("推荐来源事件载荷缺少 extra_sources")
            if source_data.extra_sources:
                self.__inner_sources.extend([s.model_dump() for s in source_data.extra_sources])

    name = "获取媒体数据"
    description = "获取榜单等媒体数据列表"
    data = FetchMediasParams().model_dump()

    @property
    def success(self) -> bool:
        """
返回媒体来源读取过程是否未记录错误。

        :return: 未遇到读取错误时为 True
        """
        return not self._has_error

    def __get_source(self, source: str) -> Optional[dict[str, Any]]:
        """
        获取数据源
        """
        for s in self.__inner_sources:
            if s['api_path'] == source:
                return s
        return None

    def execute(self, workflow_id: int, params: dict[str, object], context: ActionContext) -> ActionContext:
        """
        获取媒体数据，填充到medias
        """
        action_params = FetchMediasParams.model_validate(params)
        try:
            if action_params.source_type == "ranking":
                for api_path in action_params.sources or []:
                    if runtime_stop_state.is_workflow_stopped(workflow_id):
                        break
                    source = self.__get_source(api_path)
                    if not source:
                        continue
                    logger.info(f"获取媒体数据 {source} ...")
                    name = source.get("name")
                    results = []
                    if source.get("func"):
                        results = source['func']()
                    else:
                        # 调用内部API获取数据
                        runtime_config = get_chain_runtime_config_snapshot()
                        api_url = (
                            f"http://127.0.0.1:{runtime_config.api_port}"
                            f"/api/v1/{source['api_path']}?token={runtime_config.api_token}"
                        )
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
                runtime_config = get_chain_runtime_config_snapshot()
                api_url = (
                    f"http://127.0.0.1:{runtime_config.api_port}{action_params.api_path}"
                    f"?token={runtime_config.api_token}"
                )
                res = RequestUtils(timeout=15).post_res(api_url)
                if res:
                    results = res.json()
                    if results:
                        logger.info(f"{action_params.api_path} 获取到 {len(results)} 条数据")
                        self._medias.extend([MediaInfo(**r) for r in results])
        except Exception as e:
            logger.error(f"获取媒体数据失败: {e}")
            self._has_error = True

        if self._medias:
            if context.medias is None:
                context.medias = []
            context.medias.extend(self._medias)

        self.job_done(f"获取到 {len(self._medias)} 条媒数据")
        return context
