from typing import Optional, Tuple, Union, Any, List, Generator, Dict

from app import schemas
from app.core.config import settings
from app.core.context import MediaInfo
from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from app.modules import _ModuleBase, _MediaServerBase
from app.modules.jellyfin.jellyfin import Jellyfin
from app.schemas.types import MediaType


class JellyfinModule(_ModuleBase, _MediaServerBase):

    def init_module(self) -> None:
        """
        初始化模块
        """
        # 读取媒体服务器配置
        self._servers: Dict[str, Jellyfin] = {}
        mediaservers = MediaServerHelper().get_mediaservers()
        if not mediaservers:
            return
        for server in mediaservers:
            if server.type == "jellyfin" and server.enabled:
                self._servers[server.name] = Jellyfin(**server.config, sync_libraries=server.sync_libraries)

    @staticmethod
    def get_name() -> str:
        return "Jellyfin"

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        pass

    def scheduler_job(self) -> None:
        """
        定时任务，每10分钟调用一次
        """
        # 定时重连
        for name, server in self._servers.items():
            if server.is_inactive():
                logger.info(f"Jellyfin {name} 服务器连接断开，尝试重连 ...")
                server.reconnect()

    def stop(self):
        pass

    def test(self) -> Optional[Tuple[bool, str]]:
        """
        测试模块连接性
        """
        if not self._servers:
            return None
        for name, server in self._servers.items():
            if server.is_inactive():
                server.reconnect()
            if not server.get_user():
                return False, f"无法连接Jellyfin服务器：{name}"
        return True, ""

    def user_authenticate(self, name: str, password: str) -> Optional[str]:
        """
        使用Jellyfin用户辅助完成用户认证
        :param name: 用户名
        :param password: 密码
        :return: Token or None
        """
        # Jellyfin认证
        if not settings.JELLYFIN_AUXILIARY_AUTH:
            return None
        for server in self._servers.values():
            result = server.authenticate(name, password)
            if result:
                return result
        return None

    def webhook_parser(self, body: Any, form: Any, args: Any) -> Optional[schemas.WebhookEventInfo]:
        """
        解析Webhook报文体
        :param body:  请求体
        :param form:  请求表单
        :param args:  请求参数
        :return: 字典，解析为消息时需要包含：title、text、image
        """
        source = args.get("source")
        if source:
            server: Jellyfin = self.get_server(source)
            if not server:
                return None
            return server.get_webhook_message(body)
        for server in self._servers.values():
            result = server.get_webhook_message(body)
            if result:
                return result
        return None

    def media_exists(self, mediainfo: MediaInfo, itemid: str = None) -> Optional[schemas.ExistMediaInfo]:
        """
        判断媒体文件是否存在
        :param mediainfo:  识别的媒体信息
        :param itemid:  媒体服务器ItemID
        :return: 如不存在返回None，存在时返回信息，包括每季已存在所有集{type: movie/tv, seasons: {season: [episodes]}}
        """
        for name, server in self._servers.items():
            if mediainfo.type == MediaType.MOVIE:
                if itemid:
                    movie = server.get_iteminfo(itemid)
                    if movie:
                        logger.info(f"媒体库 {name} 中找到了 {movie}")
                        return schemas.ExistMediaInfo(
                            type=MediaType.MOVIE,
                            server=name,
                            itemid=movie.item_id
                        )
                movies = server.get_movies(title=mediainfo.title, year=mediainfo.year, tmdb_id=mediainfo.tmdb_id)
                if not movies:
                    logger.info(f"{mediainfo.title_year} 没有在媒体库 {name} 中")
                    continue
                else:
                    logger.info(f"媒体库 {name} 中找到了 {movies}")
                    return schemas.ExistMediaInfo(
                        type=MediaType.MOVIE,
                        server=name,
                        itemid=movies[0].item_id
                    )
            else:
                itemid, tvs = server.get_tv_episodes(title=mediainfo.title,
                                                     year=mediainfo.year,
                                                     tmdb_id=mediainfo.tmdb_id,
                                                     item_id=itemid)
                if not tvs:
                    logger.info(f"{mediainfo.title_year} 没有在媒体库 {name} 中")
                    continue
                else:
                    logger.info(f"{mediainfo.title_year} 在媒体库 {name} 中找到了这些季集：{tvs}")
                    return schemas.ExistMediaInfo(
                        type=MediaType.TV,
                        seasons=tvs,
                        server=name,
                        itemid=itemid
                    )
        return None

    def media_statistic(self, server: str = None) -> Optional[List[schemas.Statistic]]:
        """
        媒体数量统计
        """
        if server:
            server: Jellyfin = self.get_server(server)
            if not server:
                return None
            servers = [server]
        else:
            servers = self._servers.values()
        media_statistics = []
        for server in servers:
            media_statistic = server.get_medias_count()
            if not media_statistics:
                continue
            media_statistic.user_count = server.get_user_count()
            media_statistics.append(media_statistic)
        return media_statistics

    def mediaserver_librarys(self, server: str = None,
                             username: str = None,
                             hidden: bool = False) -> Optional[List[schemas.MediaServerLibrary]]:
        """
        媒体库列表
        """
        server: Jellyfin = self.get_server(server)
        if server:
            return server.get_librarys(username=username, hidden=hidden)
        return None

    def mediaserver_items(self, server: str, library_id: str) -> Optional[Generator]:
        """
        媒体库项目列表
        """
        server: Jellyfin = self.get_server(server)
        if server:
            return server.get_items(library_id)
        return None

    def mediaserver_iteminfo(self, server: str, item_id: str) -> Optional[schemas.MediaServerItem]:
        """
        媒体库项目详情
        """
        server: Jellyfin = self.get_server(server)
        if server:
            return server.get_iteminfo(item_id)
        return None

    def mediaserver_tv_episodes(self, server: str,
                                item_id: Union[str, int]) -> Optional[List[schemas.MediaServerSeasonInfo]]:
        """
        获取剧集信息
        """
        server: Jellyfin = self.get_server(server)
        if not server:
            return None
        _, seasoninfo = server.get_tv_episodes(item_id=item_id)
        if not seasoninfo:
            return []
        return [schemas.MediaServerSeasonInfo(
            season=season,
            episodes=episodes
        ) for season, episodes in seasoninfo.items()]

    def mediaserver_playing(self, server: str,
                            count: int = 20, username: str = None) -> List[schemas.MediaServerPlayItem]:
        """
        获取媒体服务器正在播放信息
        """
        server: Jellyfin = self.get_server(server)
        if not server:
            return []
        return server.get_resume(num=count, username=username)

    def mediaserver_play_url(self, server: str, item_id: Union[str, int]) -> Optional[str]:
        """
        获取媒体库播放地址
        """
        server: Jellyfin = self.get_server(server)
        if not server:
            return None
        return server.get_play_url(item_id)

    def mediaserver_latest(self, server: str,
                           count: int = 20, username: str = None) -> List[schemas.MediaServerPlayItem]:
        """
        获取媒体服务器最新入库条目
        """
        server: Jellyfin = self.get_server(server)
        if not server:
            return []
        return server.get_latest(num=count, username=username)
