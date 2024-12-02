from typing import Any, Generator, List, Optional, Tuple, Union

from app import schemas
from app.core.context import MediaInfo
from app.core.event import eventmanager
from app.log import logger
from app.modules import _MediaServerBase, _ModuleBase
from app.modules.jellyfin.jellyfin import Jellyfin
from app.schemas.event import AuthCredentials, AuthInterceptCredentials
from app.schemas.types import MediaType, ModuleType, ChainEventType


class JellyfinModule(_ModuleBase, _MediaServerBase[Jellyfin]):

    def init_module(self) -> None:
        """
        初始化模块
        """
        super().init_service(service_name=Jellyfin.__name__.lower(),
                             service_type=lambda conf: Jellyfin(**conf.config, sync_libraries=conf.sync_libraries))

    @staticmethod
    def get_name() -> str:
        return "Jellyfin"

    @staticmethod
    def get_type() -> ModuleType:
        """
        获取模块类型
        """
        return ModuleType.MediaServer

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效
        """
        return 2

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        pass

    def scheduler_job(self) -> None:
        """
        定时任务，每10分钟调用一次
        """
        # 定时重连
        for name, server in self.get_instances().items():
            if server.is_inactive():
                logger.info(f"Jellyfin {name} 服务器连接断开，尝试重连 ...")
                server.reconnect()

    def stop(self):
        pass

    def test(self) -> Optional[Tuple[bool, str]]:
        """
        测试模块连接性
        """
        if not self.get_instances():
            return None
        for name, server in self.get_instances().items():
            if server.is_inactive():
                server.reconnect()
            if not server.get_user():
                return False, f"无法连接Jellyfin服务器：{name}"
        return True, ""

    def user_authenticate(self, credentials: AuthCredentials, service_name: Optional[str] = None) \
            -> Optional[AuthCredentials]:
        """
        使用Jellyfin用户辅助完成用户认证
        :param credentials: 认证数据
        :param service_name: 指定要认证的媒体服务器名称，若为 None 则认证所有服务
        :return: 认证数据
        """
        # Jellyfin认证
        if not credentials or credentials.grant_type != "password":
            return None
        # 确定要认证的服务器列表
        if service_name:
            # 如果指定了服务名，获取该服务实例
            servers = [(service_name, server)] if (server := self.get_instance(service_name)) else []
        else:
            # 如果没有指定服务名，遍历所有服务
            servers = self.get_instances().items()
        # 遍历要认证的服务器
        for name, server in servers:
            # 触发认证拦截事件
            intercept_event = eventmanager.send_event(
                etype=ChainEventType.AuthIntercept,
                data=AuthInterceptCredentials(username=credentials.username, channel=self.get_name(),
                                              service=name, status="triggered")
            )
            if intercept_event and intercept_event.event_data:
                intercept_data: AuthInterceptCredentials = intercept_event.event_data
                if intercept_data.cancel:
                    continue
            token = server.authenticate(credentials.username, credentials.password)
            if token:
                credentials.channel = self.get_name()
                credentials.service = name
                credentials.token = token
                return credentials
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
            server: Jellyfin = self.get_instance(source)
            if not server:
                return None
            result = server.get_webhook_message(body)
            if result:
                result.server_name = source
            return result

        for server in self.get_instances().values():
            if server:
                result = server.get_webhook_message(body)
                if result:
                    return result
        return None

    def media_exists(self, mediainfo: MediaInfo, itemid: str = None,
                     server: str = None) -> Optional[schemas.ExistMediaInfo]:
        """
        判断媒体文件是否存在
        :param mediainfo:  识别的媒体信息
        :param itemid:  媒体服务器ItemID
        :param server:  媒体服务器名称
        :return: 如不存在返回None，存在时返回信息，包括每季已存在所有集{type: movie/tv, seasons: {season: [episodes]}}
        """
        if server:
            servers = [(server, self.get_instance(server))]
        else:
            servers = self.get_instances().items()
        for name, server in servers:
            if not server:
                continue
            if mediainfo.type == MediaType.MOVIE:
                if itemid:
                    movie = server.get_iteminfo(itemid)
                    if movie:
                        logger.info(f"媒体库 {name} 中找到了 {movie}")
                        return schemas.ExistMediaInfo(
                            type=MediaType.MOVIE,
                            server_type="jellyfin",
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
                        server_type="jellyfin",
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
                        server_type="jellyfin",
                        server=name,
                        itemid=itemid
                    )
        return None

    def media_statistic(self, server: str = None) -> Optional[List[schemas.Statistic]]:
        """
        媒体数量统计
        """
        if server:
            server_obj: Jellyfin = self.get_instance(server)
            if not server_obj:
                return None
            servers = [server_obj]
        else:
            servers = self.get_instances().values()
        media_statistics = []
        for server in servers:
            media_statistic = server.get_medias_count()
            if not media_statistic:
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
        server_obj: Jellyfin = self.get_instance(server)
        if server_obj:
            return server_obj.get_librarys(username=username, hidden=hidden)
        return None

    def mediaserver_items(self, server: str, library_id: Union[str, int], start_index: int = 0,
                          limit: Optional[int] = -1) -> Optional[Generator]:
        """
        获取媒体服务器项目列表，支持分页和不分页逻辑，默认不分页获取所有数据

        :param server: 媒体服务器名称
        :param library_id: 媒体库ID，用于标识要获取的媒体库
        :param start_index: 起始索引，用于分页获取数据。默认为 0，即从第一个项目开始获取
        :param limit: 每次请求的最大项目数，用于分页。如果为 None 或 -1，则表示一次性获取所有数据，默认为 -1

        :return: 返回一个生成器对象，用于逐步获取媒体服务器中的项目
        """
        server_obj: Jellyfin = self.get_instance(server)
        if server_obj:
            return server_obj.get_items(library_id, start_index, limit)
        return None

    def mediaserver_iteminfo(self, server: str, item_id: str) -> Optional[schemas.MediaServerItem]:
        """
        媒体库项目详情
        """
        server_obj: Jellyfin = self.get_instance(server)
        if server_obj:
            return server_obj.get_iteminfo(item_id)
        return None

    def mediaserver_tv_episodes(self, server: str,
                                item_id: Union[str, int]) -> Optional[List[schemas.MediaServerSeasonInfo]]:
        """
        获取剧集信息
        """
        server_obj: Jellyfin = self.get_instance(server)
        if not server_obj:
            return None
        _, seasoninfo = server_obj.get_tv_episodes(item_id=item_id)
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
        server_obj: Jellyfin = self.get_instance(server)
        if not server_obj:
            return []
        return server_obj.get_resume(num=count, username=username)

    def mediaserver_play_url(self, server: str, item_id: Union[str, int]) -> Optional[str]:
        """
        获取媒体库播放地址
        """
        server_obj: Jellyfin = self.get_instance(server)
        if not server_obj:
            return None
        return server_obj.get_play_url(item_id)

    def mediaserver_latest(self, server: str = None, count: int = 20,
                           username: str = None) -> List[schemas.MediaServerPlayItem]:
        """
        获取媒体服务器最新入库条目
        """
        server_obj: Jellyfin = self.get_instance(server)
        if not server_obj:
            return []
        return server_obj.get_latest(num=count, username=username)

    def mediaserver_latest_images(self,
                                  server: str = None,
                                  count: int = 20,
                                  username: str = None,
                                  remote: bool = False,
                                  ) -> List[str]:
        """
        获取媒体服务器最新入库条目的图片

        :param server: 媒体服务器名称
        :param count: 获取数量
        :param username: 用户名
        :param remote: True为外网链接, False为内网链接
        :return: 图片链接列表
        """
        server_obj: Jellyfin = self.get_instance(server)
        if not server:
            return []

        links = []
        items: List[schemas.MediaServerPlayItem] = self.mediaserver_latest(server=server, count=count,
                                                                           username=username)
        for item in items:
            if item.BackdropImageTags:
                image_url = server_obj.get_backdrop_url(item_id=item.id,
                                                        image_tag=item.BackdropImageTags[0],
                                                        remote=remote)
                if image_url:
                    links.append(image_url)
        return links
