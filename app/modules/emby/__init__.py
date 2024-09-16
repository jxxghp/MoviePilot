from typing import Optional, Tuple, Union, Any, List, Generator

from app import schemas
from app.core.context import MediaInfo
from app.log import logger
from app.modules import _ModuleBase
from app.modules.emby.emby import Emby
from app.schemas.types import MediaType


class EmbyModule(_ModuleBase):
    emby: Emby = None

    def init_module(self) -> None:
        self.emby = Emby()

    @staticmethod
    def get_name() -> str:
        return "Emby"

    def stop(self):
        pass

    def test(self) -> Tuple[bool, str]:
        """
        测试模块连接性
        """
        if self.emby.is_inactive():
            self.emby.reconnect()
        if not self.emby.get_user():
            return False, "无法连接Emby，请检查参数配置"
        return True, ""

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        return "MEDIASERVER", "emby"

    def scheduler_job(self) -> None:
        """
        定时任务，每10分钟调用一次
        """
        # 定时重连
        if self.emby.is_inactive():
            self.emby.reconnect()

    def user_authenticate(self, name: str, password: str) -> Optional[str]:
        """
        使用Emby用户辅助完成用户认证
        :param name: 用户名
        :param password: 密码
        :return: token or None
        """
        # Emby认证
        return self.emby.authenticate(name, password)

    def webhook_parser(self, body: Any, form: Any, args: Any) -> Optional[schemas.WebhookEventInfo]:
        """
        解析Webhook报文体
        :param body:  请求体
        :param form:  请求表单
        :param args:  请求参数
        :return: 字典，解析为消息时需要包含：title、text、image
        """
        return self.emby.get_webhook_message(form, args)

    def media_exists(self, mediainfo: MediaInfo, itemid: str = None) -> Optional[schemas.ExistMediaInfo]:
        """
        判断媒体文件是否存在
        :param mediainfo:  识别的媒体信息
        :param itemid:  媒体服务器ItemID
        :return: 如不存在返回None，存在时返回信息，包括每季已存在所有集{type: movie/tv, seasons: {season: [episodes]}}
        """
        if mediainfo.type == MediaType.MOVIE:
            if itemid:
                movie = self.emby.get_iteminfo(itemid)
                if movie:
                    logger.info(f"媒体库中已存在：{movie}")
                    return schemas.ExistMediaInfo(
                        type=MediaType.MOVIE,
                        server="emby",
                        itemid=movie.item_id
                    )
            movies = self.emby.get_movies(title=mediainfo.title,
                                          year=mediainfo.year,
                                          tmdb_id=mediainfo.tmdb_id)
            if not movies:
                logger.info(f"{mediainfo.title_year} 在媒体库中不存在")
                return None
            else:
                logger.info(f"媒体库中已存在：{movies}")
                return schemas.ExistMediaInfo(
                    type=MediaType.MOVIE,
                    server="emby",
                    itemid=movies[0].item_id
                )
        else:
            itemid, tvs = self.emby.get_tv_episodes(title=mediainfo.title,
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
                    server="emby",
                    itemid=itemid
                )

    def media_statistic(self) -> List[schemas.Statistic]:
        """
        媒体数量统计
        """
        media_statistic = self.emby.get_medias_count()
        media_statistic.user_count = self.emby.get_user_count()
        return [media_statistic]

    def mediaserver_librarys(self, server: str = None, username: str = None) -> Optional[List[schemas.MediaServerLibrary]]:
        """
        媒体库列表
        """
        if server and server != "emby":
            return None
        return self.emby.get_librarys(username)

    def mediaserver_items(self, server: str, library_id: str) -> Optional[Generator]:
        """
        媒体库项目列表
        """
        if server != "emby":
            return None
        return self.emby.get_items(library_id)

    def mediaserver_iteminfo(self, server: str, item_id: str) -> Optional[schemas.MediaServerItem]:
        """
        媒体库项目详情
        """
        if server != "emby":
            return None
        return self.emby.get_iteminfo(item_id)

    def mediaserver_tv_episodes(self, server: str,
                                item_id: Union[str, int]) -> Optional[List[schemas.MediaServerSeasonInfo]]:
        """
        获取剧集信息
        """
        if server != "emby":
            return None
        _, seasoninfo = self.emby.get_tv_episodes(item_id=item_id)
        if not seasoninfo:
            return []
        return [schemas.MediaServerSeasonInfo(
            season=season,
            episodes=episodes
        ) for season, episodes in seasoninfo.items()]

    def mediaserver_playing(self, count: int = 20,
                            server: str = None, username: str = None) -> List[schemas.MediaServerPlayItem]:
        """
        获取媒体服务器正在播放信息
        """
        if server and server != "emby":
            return []
        return self.emby.get_resume(num=count, username=username)

    def mediaserver_play_url(self, server: str, item_id: Union[str, int]) -> Optional[str]:
        """
        获取媒体库播放地址
        """
        if server != "emby":
            return None
        return self.emby.get_play_url(item_id)

    def mediaserver_latest(self, count: int = 20,
                           server: str = None, username: str = None) -> List[schemas.MediaServerPlayItem]:
        """
        获取媒体服务器最新入库条目
        """
        if server and server != "emby":
            return []
        return self.emby.get_latest(num=count, username=username)
