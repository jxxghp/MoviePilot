from typing import Optional, Tuple, Union, Any, List, Generator

from app import schemas
from app.core.context import MediaInfo
from app.log import logger
from app.modules import _ModuleBase
from app.modules.plex.plex import Plex
from app.schemas.types import MediaType


class PlexModule(_ModuleBase):
    plex: Plex = None

    def init_module(self) -> None:
        self.plex = Plex()

    @staticmethod
    def get_name() -> str:
        return "Plex"

    def stop(self):
        pass

    def test(self) -> Tuple[bool, str]:
        """
        测试模块连接性
        """
        if self.plex.is_inactive():
            self.plex.reconnect()
        if not self.plex.get_librarys():
            return False, "无法连接Plex，请检查参数配置"
        return True, ""

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        return "MEDIASERVER", "plex"

    def scheduler_job(self) -> None:
        """
        定时任务，每10分钟调用一次
        """
        # 定时重连
        if self.plex.is_inactive():
            self.plex.reconnect()

    def webhook_parser(self, body: Any, form: Any, args: Any) -> Optional[schemas.WebhookEventInfo]:
        """
        解析Webhook报文体
        :param body:  请求体
        :param form:  请求表单
        :param args:  请求参数
        :return: 字典，解析为消息时需要包含：title、text、image
        """
        return self.plex.get_webhook_message(form)

    def media_exists(self, mediainfo: MediaInfo, itemid: str = None) -> Optional[schemas.ExistMediaInfo]:
        """
        判断媒体文件是否存在
        :param mediainfo:  识别的媒体信息
        :param itemid:  媒体服务器ItemID
        :return: 如不存在返回None，存在时返回信息，包括每季已存在所有集{type: movie/tv, seasons: {season: [episodes]}}
        """
        if mediainfo.type == MediaType.MOVIE:
            if itemid:
                movie = self.plex.get_iteminfo(itemid)
                if movie:
                    logger.info(f"媒体库中已存在：{movie}")
                    return schemas.ExistMediaInfo(
                        type=MediaType.MOVIE,
                        server="plex",
                        itemid=movie.item_id
                    )
            movies = self.plex.get_movies(title=mediainfo.title,
                                          original_title=mediainfo.original_title,
                                          year=mediainfo.year,
                                          tmdb_id=mediainfo.tmdb_id)
            if not movies:
                logger.info(f"{mediainfo.title_year} 在媒体库中不存在")
                return None
            else:
                logger.info(f"媒体库中已存在：{movies}")
                return schemas.ExistMediaInfo(
                    type=MediaType.MOVIE,
                    server="plex",
                    itemid=movies[0].item_id
                )
        else:
            item_id, tvs = self.plex.get_tv_episodes(title=mediainfo.title,
                                                     original_title=mediainfo.original_title,
                                                     year=mediainfo.year,
                                                     tmdb_id=mediainfo.tmdb_id,
                                                     item_id=itemid)
            if not tvs:
                logger.info(f"{mediainfo.title_year} 在媒体库中不存在")
                return None
            else:
                logger.info(f"{mediainfo.title_year} 媒体库中已存在：{tvs}")
                return schemas.ExistMediaInfo(
                    type=MediaType.TV,
                    seasons=tvs,
                    server="plex",
                    itemid=item_id
                )

    def media_statistic(self) -> List[schemas.Statistic]:
        """
        媒体数量统计
        """
        media_statistic = self.plex.get_medias_count()
        media_statistic.user_count = 1
        return [media_statistic]

    def mediaserver_librarys(self, server: str = None, **kwargs) -> Optional[List[schemas.MediaServerLibrary]]:
        """
        媒体库列表
        """
        if server and server != "plex":
            return None
        return self.plex.get_librarys()

    def mediaserver_items(self, server: str, library_id: str) -> Optional[Generator]:
        """
        媒体库项目列表
        """
        if server != "plex":
            return None
        return self.plex.get_items(library_id)

    def mediaserver_iteminfo(self, server: str, item_id: str) -> Optional[schemas.MediaServerItem]:
        """
        媒体库项目详情
        """
        if server != "plex":
            return None
        return self.plex.get_iteminfo(item_id)

    def mediaserver_tv_episodes(self, server: str,
                                item_id: Union[str, int]) -> Optional[List[schemas.MediaServerSeasonInfo]]:
        """
        获取剧集信息
        """
        if server != "plex":
            return None
        _, seasoninfo = self.plex.get_tv_episodes(item_id=item_id)
        if not seasoninfo:
            return []
        return [schemas.MediaServerSeasonInfo(
            season=season,
            episodes=episodes
        ) for season, episodes in seasoninfo.items()]

    def mediaserver_playing(self, count: int = 20, server: str = None, **kwargs) -> List[schemas.MediaServerPlayItem]:
        """
        获取媒体服务器正在播放信息
        """
        if server and server != "plex":
            return []
        return self.plex.get_resume(count)

    def mediaserver_latest(self, count: int = 20, server: str = None, **kwargs) -> List[schemas.MediaServerPlayItem]:
        """
        获取媒体服务器最新入库条目
        """
        if server and server != "plex":
            return []
        return self.plex.get_latest(count)

    def mediaserver_play_url(self, server: str, item_id: Union[str, int]) -> Optional[str]:
        """
        获取媒体库播放地址
        """
        if server != "plex":
            return None
        return self.plex.get_play_url(item_id)
