"""
绿联模块，将双协议媒体库查询、刷新和播放信息接入统一接口。
"""

from typing import Any, Generator, Iterable, List, Optional, Tuple, Union

from app.modules._base.mediaserver import _MediaServerModuleBase
from app.modules.ugreen.ugreen import Ugreen
from app.runtime.log import logger
from app.schemas.dashboard import Statistic as _SchemaStatistic
from app.schemas.mediaserver import MediaServerItem as _SchemaMediaServerItem
from app.schemas.mediaserver import MediaServerLibrary as _SchemaMediaServerLibrary
from app.schemas.mediaserver import MediaServerPlayItem as _SchemaMediaServerPlayItem
from app.schemas.mediaserver import MediaServerSeasonInfo as _SchemaMediaServerSeasonInfo
from app.schemas.mediaserver import WebhookEventInfo as _SchemaWebhookEventInfo
from app.schemas.types import MediaServerType, ModuleType


class UgreenModule(_MediaServerModuleBase[Ugreen]):
    """
    将绿联影视接入媒体服务器统一接口。
    """

    # 媒体库标识（ExistMediaInfo.server_type）
    _server_type_value = "ugreen"

    def init_module(self) -> None:
        """
        初始化模块。
        """
        super().init_service(
            service_name=Ugreen.__name__.lower(),
            service_type=lambda conf: Ugreen(
                **conf.config, sync_libraries=conf.sync_libraries
            ),
        )

    @staticmethod
    def get_name() -> str:
        """
        返回媒体服务器模块名称。
        """
        return "绿联影视"

    @staticmethod
    def get_type() -> ModuleType:
        """
        获取模块类型。
        """
        return ModuleType.MediaServer

    @staticmethod
    def get_subtype() -> MediaServerType:
        """
        获取模块子类型。
        """
        return MediaServerType.Ugreen

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效。
        """
        return 5

    def init_setting(self) -> Optional[Tuple[str, Union[str, bool]]]:
        """
        模块不提供额外的初始化开关。

        :return: 始终为 None，表示不检查模块级配置项
        """
        pass

    def _is_inactive(self, server: Ugreen) -> bool:
        """
        未配置的实例不参与定时重连。
        """
        return server.is_configured() and server.is_inactive()

    def stop(self) -> None:
        """
        断开已认证实例的连接，逐个记录关闭失败并继续清理其他实例。
        """
        for server in self.get_instances().values():
            try:
                if server.is_authenticated():
                    server.disconnect()
            except Exception as err:
                logger.error(f"停止绿联影视模块实例失败：{err}")

    def _test_server(self, server: Ugreen, name: Optional[str]) -> Optional[str]:
        """
        绿联影视用配置完整性与重连结果探测连接状态。
        """
        if not server.is_configured():
            return f"{self.get_name()}配置不完整：{name}"
        if server.is_inactive() and not server.reconnect():
            return f"无法连接{self.get_name()}：{name}"
        return None

    def webhook_parser(
        self, body: Any, form: Any, args: Any
    ) -> Optional[_SchemaWebhookEventInfo]:
        """
        保留统一 Webhook 路由入口；绿联客户端目前不解析事件。

        :param body: 传给客户端的请求体
        :param form: 兼容统一接口的表单，当前不使用
        :param args: 查询参数，source 可指定实例
        :return: 当前客户端均返回 None
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
        汇总指定实例或全部绿联实例的媒体与用户统计。

        :param server: 指定实例名称；省略时汇总全部实例
        :return: 统计列表；指定实例不存在时返回 None
        """
        servers: Iterable[Ugreen]
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
        self, server: Optional[str] = None, hidden: Optional[bool] = False, **kwargs: object
    ) -> Optional[List[_SchemaMediaServerLibrary]]:
        """
        查询可见媒体库并按需应用同步范围筛选。

        :param server: 媒体服务器配置名称，空值使用默认实例
        :param hidden: 是否隐藏同步范围外的库
        :param kwargs: 为统一媒体服务器接口保留，当前不使用
        :return: 媒体库列表；实例不可用或查询失败时返回 None
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
    ) -> Optional[Generator[_SchemaMediaServerItem, None, None]]:
        """
        返回媒体库同步条目的迭代器。

        :param server: 媒体服务器配置名称，空值使用默认实例
        :param library_id: 媒体库标识
        :param start_index: 需要跳过的起始条目数
        :param limit: 期望条目上限，None 或 -1 表示不限；应传正数或不限标记
        :return: 统一媒体条目生成器；实例不存在时返回 None，底层失败可能提前结束迭代
        """
        server_obj: Optional[Ugreen] = self.get_instance(server)
        if server_obj:
            return server_obj.get_items(library_id, start_index, limit)
        return None

    def mediaserver_items_count(
        self, server: str, library_id: Union[str, int]
    ) -> Optional[int]:
        """
        获取指定媒体库可同步的媒体条目总数。

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
        查询统一媒体条目详情。

        :param server: 媒体服务器配置名称，空值使用默认实例
        :param item_id: 对应绿联服务的媒体条目标识
        :return: 条目详情；实例不存在、条目无效或查询失败时返回 None
        """
        server_obj: Optional[Ugreen] = self.get_instance(server)
        if server_obj:
            return server_obj.get_iteminfo(item_id)
        return None

    def mediaserver_tv_episodes(
        self, server: str, item_id: Union[str, int]
    ) -> Optional[List[_SchemaMediaServerSeasonInfo]]:
        """
        查询电视剧已经入库的季集编号。

        :param server: 媒体服务器配置名称，空值使用默认实例
        :param item_id: 对应绿联服务的媒体条目标识
        :return: 季集列表；标识为空或实例不存在时返回 None，无可用数据时返回空列表
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
        self, server: str, count: Optional[int] = 20, **kwargs: object
    ) -> Optional[List[_SchemaMediaServerPlayItem]]:
        """
        查询当前账号的继续观看列表。

        :param server: 媒体服务器配置名称，空值使用默认实例
        :param count: 期望条目数量，空值或 0 由客户端回退为 12
        :param kwargs: 为统一媒体服务器接口保留，当前不使用
        :return: 播放条目列表；实例不可用或请求失败时返回 None
        """
        server_obj: Optional[Ugreen] = self.get_instance(server)
        if not server_obj:
            return None
        return server_obj.get_resume(num=count)

    def mediaserver_play_url(
        self, server: str, item_id: Union[str, int]
    ) -> Optional[str]:
        """
        生成媒体条目的网页跳转链接。

        :param server: 媒体服务器配置名称，空值使用默认实例
        :param item_id: 对应绿联服务的媒体条目标识
        :return: 媒体库条目链接或 NAS 根地址；标识、实例或条目不可用时返回 None
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
        **kwargs: object,
    ) -> Optional[List[_SchemaMediaServerPlayItem]]:
        """
        查询当前账号最近入库的电影和电视剧。

        :param server: 媒体服务器配置名称，空值使用默认实例
        :param count: 期望条目数量，客户端最少请求 1 条
        :param kwargs: 为统一媒体服务器接口保留，当前不使用
        :return: 最新媒体列表；实例不可用或请求失败时返回 None
        """
        server_obj: Optional[Ugreen] = self.get_instance(server)
        if not server_obj:
            return None
        return server_obj.get_latest(num=count if count is not None else 20)

    def mediaserver_latest_images(
        self,
        server: Optional[str] = None,
        count: Optional[int] = 20,
        remote: Optional[bool] = False,
        **kwargs: object,
    ) -> List[str]:
        """
        获取最新媒体的背景图或主封面。

        :param server: 媒体服务器配置名称，空值使用默认实例
        :param count: 期望图片数量
        :param remote: 媒体库服务模式是否优先使用外网图片地址
        :param kwargs: 为统一媒体服务器接口保留，当前不使用
        :return: 图片 URL 列表；实例不可用或没有可用图片时返回空列表
        """
        server_obj: Optional[Ugreen] = self.get_instance(server)
        if not server_obj:
            return []
        return server_obj.get_latest_backdrops(
            num=count if count is not None else 20,
            remote=remote if remote is not None else False,
        ) or []

    def mediaserver_image_cookies(
        self,
        server: Optional[str] = None,
        image_url: Optional[str] = None,
        **kwargs: object,
    ) -> None:
        """
        保留统一图片 Cookie 查询接口。

        :param server: 媒体服务器配置名称，空值使用默认实例
        :param image_url: 待查询的图片地址，空值时直接返回
        :param kwargs: 为统一媒体服务器接口保留，当前不使用
        :return: 当前绿联客户端不提供图片 Cookie，返回 None
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
