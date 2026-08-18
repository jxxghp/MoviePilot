from typing import Any, Generator, List, Optional, Tuple, Union

from app.schemas.dashboard import Statistic as _SchemaStatistic
from app.schemas.mediaserver import MediaServerItem as _SchemaMediaServerItem
from app.schemas.mediaserver import MediaServerLibrary as _SchemaMediaServerLibrary
from app.schemas.mediaserver import MediaServerPlayItem as _SchemaMediaServerPlayItem
from app.schemas.mediaserver import MediaServerSeasonInfo as _SchemaMediaServerSeasonInfo
from app.schemas.mediaserver import WebhookEventInfo as _SchemaWebhookEventInfo
from app.runtime.log import logger
from app.modules._base import _MediaServerModuleBase
from app.modules.ugreen.ugreen import Ugreen


class UgreenModule(_MediaServerModuleBase[Ugreen]):

    # 媒体库标识（ExistMediaInfo.server_type）
    _server_type_value = "ugreen"

    def init_module(self) -> None:
        """
        初始化模块
        """
        super().init_service(
            service_name=Ugreen.__name__.lower(),
            service_type=lambda conf: Ugreen(
                **conf.config, sync_libraries=conf.sync_libraries
            ),
        )

    @staticmethod
    def get_name() -> str:
        return "绿联影视"

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效
        """
        return 5

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        pass

    def _is_inactive(self, server) -> bool:
        """未配置的实例不参与定时重连。"""
        return server.is_configured() and server.is_inactive()

    def stop(self) -> None:
        """停止模块"""
        for server in self.get_instances().values():
            try:
                if server.is_authenticated():
                    server.disconnect()
            except Exception as err:
                logger.error(f"停止绿联影视模块实例失败：{err}")

    def _test_server(self, server, name: str) -> Optional[str]:
        """绿联影视用配置完整性与重连结果探测连接状态。"""
        if not server.is_configured():
            return f"{self.get_name()}配置不完整：{name}"
        if server.is_inactive() and not server.reconnect():
            return f"无法连接{self.get_name()}：{name}"
        return None

    def webhook_parser(
        self, body: Any, form: Any, args: Any
    ) -> Optional[_SchemaWebhookEventInfo]:
        """
        解析Webhook报文体
        """
        source = args.get("source")
        if source:
            server: Optional[Ugreen] = self.get_instance(source)
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

    def media_statistic(
        self, server: Optional[str] = None
    ) -> Optional[List[_SchemaStatistic]]:
        """
        媒体数量统计
        """
        if server:
            server_obj: Optional[Ugreen] = self.get_instance(server)
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

    def mediaserver_librarys(
        self, server: Optional[str] = None, hidden: Optional[bool] = False, **kwargs
    ) -> Optional[List[_SchemaMediaServerLibrary]]:
        """
        媒体库列表
        """
        server_obj: Optional[Ugreen] = self.get_instance(server)
        if server_obj:
            return server_obj.get_librarys(hidden=hidden)
        return None

    def mediaserver_items(
        self,
        server: str,
        library_id: Union[str, int],
        start_index: Optional[int] = 0,
        limit: Optional[int] = -1,
    ) -> Optional[Generator]:
        """
        获取媒体服务器项目列表
        """
        server_obj: Optional[Ugreen] = self.get_instance(server)
        if server_obj:
            return server_obj.get_items(library_id, start_index, limit)
        return None

    def mediaserver_items_count(
        self, server: str, library_id: Union[str, int]
    ) -> Optional[int]:
        """
        获取指定媒体库可同步的媒体条目总数

        :param server: 媒体服务器名称
        :param library_id: 媒体库ID
        :return: 媒体条目总数，查询失败时返回None
        """
        server_obj: Optional[Ugreen] = self.get_instance(server)
        if server_obj:
            return server_obj.get_items_count(library_id)
        return None

    def mediaserver_iteminfo(
        self, server: str, item_id: str
    ) -> Optional[_SchemaMediaServerItem]:
        """
        媒体库项目详情
        """
        server_obj: Optional[Ugreen] = self.get_instance(server)
        if server_obj:
            return server_obj.get_iteminfo(item_id)
        return None

    def mediaserver_tv_episodes(
        self, server: str, item_id: Union[str, int]
    ) -> Optional[List[_SchemaMediaServerSeasonInfo]]:
        """
        获取剧集信息
        """
        if not item_id:
            return None
        server_obj: Optional[Ugreen] = self.get_instance(server)
        if not server_obj:
            return None
        _, seasoninfo = server_obj.get_tv_episodes(item_id=str(item_id))
        if not seasoninfo:
            return []
        return [
            _SchemaMediaServerSeasonInfo(season=season, episodes=episodes)
            for season, episodes in seasoninfo.items()
        ]

    def mediaserver_playing(
        self, server: str, count: Optional[int] = 20, **kwargs
    ) -> Optional[List[_SchemaMediaServerPlayItem]]:
        """
        获取媒体服务器正在播放信息
        """
        server_obj: Optional[Ugreen] = self.get_instance(server)
        if not server_obj:
            return None
        return server_obj.get_resume(num=count)

    def mediaserver_play_url(
        self, server: str, item_id: Union[str, int]
    ) -> Optional[str]:
        """
        获取媒体库播放地址
        """
        if not item_id:
            return None
        server_obj: Optional[Ugreen] = self.get_instance(server)
        if not server_obj:
            return None
        return server_obj.get_play_url(str(item_id))

    def mediaserver_latest(
        self,
        server: Optional[str] = None,
        count: Optional[int] = 20,
        **kwargs,
    ) -> Optional[List[_SchemaMediaServerPlayItem]]:
        """
        获取媒体服务器最新入库条目
        """
        server_obj: Optional[Ugreen] = self.get_instance(server)
        if not server_obj:
            return None
        return server_obj.get_latest(num=count)

    def mediaserver_latest_images(
        self,
        server: Optional[str] = None,
        count: Optional[int] = 20,
        remote: Optional[bool] = False,
        **kwargs,
    ) -> List[str]:
        """
        获取媒体服务器最新入库条目的图片
        """
        server_obj: Optional[Ugreen] = self.get_instance(server)
        if not server_obj:
            return []
        return server_obj.get_latest_backdrops(num=count, remote=remote) or []

    def mediaserver_image_cookies(
        self,
        server: Optional[str] = None,
        image_url: Optional[str] = None,
        **kwargs,
    ) -> Optional[str | dict]:
        """
        获取绿联影视服务器的图片Cookies
        """
        if not image_url:
            return None
        if server:
            server_obj: Optional[Ugreen] = self.get_instance(server)
            if not server_obj:
                return None
            return server_obj.get_image_cookies(image_url)
        for server_obj in self.get_instances().values():
            if cookies := server_obj.get_image_cookies(image_url):
                return cookies
        return None
