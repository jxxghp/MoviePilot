from typing import Any, Generator, List, Optional, Tuple, Union

from app.schemas.dashboard import Statistic as _SchemaStatistic
from app.schemas.mediaserver import MediaServerItem as _SchemaMediaServerItem
from app.schemas.mediaserver import MediaServerLibrary as _SchemaMediaServerLibrary
from app.schemas.mediaserver import MediaServerPlayItem as _SchemaMediaServerPlayItem
from app.schemas.mediaserver import MediaServerSeasonInfo as _SchemaMediaServerSeasonInfo
from app.schemas.mediaserver import WebhookEventInfo as _SchemaWebhookEventInfo
from app.runtime.log import logger
from app.modules._base import _MediaServerModuleBase
from app.modules.zspace.zspace import ZSpace
from app.schemas.event import AuthCredentials
from app.schemas.event import AuthInterceptCredentials
from app.schemas.types import ChainEventType


class ZSpaceModule(_MediaServerModuleBase[ZSpace]):

    # 媒体库标识（ExistMediaInfo.server_type）
    _server_type_value = "zspace"

    def init_module(self) -> None:
        """
        初始化模块
        """
        super().init_service(service_name=ZSpace.__name__.lower(),
                             service_type=lambda conf: ZSpace(**conf.config, sync_libraries=conf.sync_libraries))

    @staticmethod
    def get_name() -> str:
        return "极影视"

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效
        """
        return 6

    def stop(self):
        pass

    def _test_server(self, server, name: str) -> Optional[str]:
        """极影视用重连结果与用户信息探测连接状态。"""
        if server.is_inactive() and not server.reconnect():
            return f"无法连接{self.get_name()}服务器：{name}"
        if not server.user:
            return f"无法连接{self.get_name()}服务器：{name}"
        return None

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        pass

    def webhook_parser(self, body: Any, form: Any, args: Any) -> Optional[_SchemaWebhookEventInfo]:
        """
        解析Webhook报文体
        :param body:  请求体
        :param form:  请求表单
        :param args:  请求参数
        :return: 字典，解析为消息时需要包含：title、text、image
        """
        source = args.get("source")
        if source:
            server: ZSpace = self.get_instance(source)
            if not server:
                return None
            result = server.get_webhook_message(form, args)
            if result:
                result.server_name = source
            return result

        for server in self.get_instances().values():
            if server:
                result = server.get_webhook_message(form, args)
                if result:
                    return result
        return None

    def media_statistic(self, server: Optional[str] = None) -> Optional[List[_SchemaStatistic]]:
        """
        媒体数量统计
        """
        if server:
            server_obj: ZSpace = self.get_instance(server)
            if not server_obj:
                return None
            servers = [server_obj]
        else:
            servers = self.get_instances().values()
        media_statistics = []
        for s in servers:
            media_statistic = s.get_medias_count()
            if not media_statistic:
                continue
            media_statistic.user_count = s.get_user_count()
            media_statistics.append(media_statistic)
        return media_statistics

    def mediaserver_librarys(self, server: str,
                             username: Optional[str] = None,
                             hidden: Optional[bool] = False) -> Optional[List[_SchemaMediaServerLibrary]]:
        """
        媒体库列表
        """
        server_obj: ZSpace = self.get_instance(server)
        if server_obj:
            return server_obj.get_librarys(username=username, hidden=hidden)
        return None

    def mediaserver_items(self, server: str, library_id: Union[str, int], start_index: Optional[int] = 0,
                          limit: Optional[int] = -1) -> Optional[Generator]:
        """
        获取媒体服务器项目列表，支持分页和不分页逻辑，默认不分页获取所有数据

        :param server: 媒体服务器名称
        :param library_id: 媒体库ID，用于标识要获取的媒体库
        :param start_index: 起始索引，用于分页获取数据。默认为 0，即从第一个项目开始获取
        :param limit: 每次请求的最大项目数，用于分页。如果为 None 或 -1，则表示一次性获取所有数据，默认为 -1

        :return: 返回一个生成器对象，用于逐步获取媒体服务器中的项目
        """
        server_obj: ZSpace = self.get_instance(server)
        if server_obj:
            return server_obj.get_items(library_id, start_index, limit)
        return None

    def mediaserver_items_count(self, server: str, library_id: Union[str, int]) -> Optional[int]:
        """
        获取指定媒体库可同步的媒体条目总数

        :param server: 媒体服务器名称
        :param library_id: 媒体库ID
        :return: 媒体条目总数，查询失败时返回None
        """
        server_obj: ZSpace = self.get_instance(server)
        if server_obj:
            return server_obj.get_items_count(library_id)
        return None

    def mediaserver_iteminfo(self, server: str, item_id: str) -> Optional[_SchemaMediaServerItem]:
        """
        媒体库项目详情
        """
        server_obj: ZSpace = self.get_instance(server)
        if server_obj:
            return server_obj.get_iteminfo(item_id)
        return None

    def mediaserver_tv_episodes(self, server: str,
                                item_id: Union[str, int]) -> Optional[List[_SchemaMediaServerSeasonInfo]]:
        """
        获取剧集信息
        """
        server_obj: ZSpace = self.get_instance(server)
        if not server_obj:
            return None
        _, seasoninfo = server_obj.get_tv_episodes(item_id=item_id)
        if not seasoninfo:
            return []
        return [_SchemaMediaServerSeasonInfo(
            season=season,
            episodes=episodes
        ) for season, episodes in seasoninfo.items()]

    def mediaserver_playing(self, server: str, count: Optional[int] = 20,
                            username: Optional[str] = None) -> Optional[List[_SchemaMediaServerPlayItem]]:
        """
        获取媒体服务器正在播放信息
        """
        server_obj: ZSpace = self.get_instance(server)
        if not server_obj:
            return None
        return server_obj.get_resume(num=count, username=username)

    def mediaserver_play_url(self, server: str, item_id: Union[str, int]) -> Optional[str]:
        """
        获取媒体库播放地址
        """
        server_obj: ZSpace = self.get_instance(server)
        if not server_obj:
            return None
        return server_obj.get_play_url(item_id)

    def mediaserver_latest(self, server: Optional[str] = None, count: Optional[int] = 20,
                           username: Optional[str] = None) -> Optional[List[_SchemaMediaServerPlayItem]]:
        """
        获取媒体服务器最新入库条目
        """
        server_obj: ZSpace = self.get_instance(server)
        if not server_obj:
            return None
        return server_obj.get_latest(num=count, username=username)

    def mediaserver_latest_images(self,
                                  server: Optional[str] = None,
                                  count: Optional[int] = 10,
                                  username: Optional[str] = None,
                                  remote: Optional[bool] = False
                                  ) -> List[str]:
        """
        获取媒体服务器最新入库条目的图片

        :param server: 媒体服务器名称
        :param count: 获取数量
        :param username: 用户名
        :param remote: True为外网链接, False为内网链接
        :return: 图片链接列表
        """
        server_obj: ZSpace = self.get_instance(server)
        if not server_obj:
            return []

        links = []
        items = self.mediaserver_latest(server=server, count=count, username=username) or []
        for item in items:
            if item.BackdropImageTags:
                image_url = server_obj.get_backdrop_url(item_id=item.id,
                                                        image_tag=item.BackdropImageTags[0],
                                                        remote=remote)
                if image_url:
                    links.append(image_url)
        return links
