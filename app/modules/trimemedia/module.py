"""
飞牛模块，将媒体库、播放信息与受保护图片接入统一接口。
"""

from typing import Any, Generator, Iterable, List, Optional, Tuple, Union

from app.modules._base.mediaserver import _MediaServerModuleBase
from app.modules.trimemedia.trimemedia import TrimeMedia
from app.runtime.log import logger
from app.schemas.dashboard import Statistic as _SchemaStatistic
from app.schemas.mediaserver import MediaServerItem as _SchemaMediaServerItem
from app.schemas.mediaserver import MediaServerLibrary as _SchemaMediaServerLibrary
from app.schemas.mediaserver import MediaServerPlayItem as _SchemaMediaServerPlayItem
from app.schemas.mediaserver import MediaServerSeasonInfo as _SchemaMediaServerSeasonInfo
from app.schemas.mediaserver import WebhookEventInfo as _SchemaWebhookEventInfo
from app.schemas.types import MediaServerType, ModuleType


class TrimeMediaModule(_MediaServerModuleBase[TrimeMedia]):
    """
    将飞牛影视接入媒体服务器统一接口。
    """

    # 媒体库标识（ExistMediaInfo.server_type）
    _server_type_value = "trimemedia"

    def init_module(self) -> None:
        """
        初始化模块。
        """
        super().init_service(
            service_name=TrimeMedia.__name__.lower(),
            service_type=lambda conf: TrimeMedia(
                **conf.config, sync_libraries=conf.sync_libraries
            ),
        )

    @staticmethod
    def get_name() -> str:
        """
        返回媒体服务器模块名称。
        """
        return "飞牛影视"

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
        return MediaServerType.TrimeMedia

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效。
        """
        return 4

    def init_setting(self) -> Optional[Tuple[str, Union[str, bool]]]:
        """
        模块不提供额外的初始化开关。

        :return: 始终为 None，表示不检查模块级配置项
        """
        pass

    def _is_inactive(self, server: TrimeMedia) -> bool:
        """
        未配置的实例不参与定时重连。
        """
        return server.is_configured() and server.is_inactive()

    def stop(self) -> None:
        """
        登出并断开已认证的飞牛实例，单个失败不阻止其余实例清理。
        """
        for server in self.get_instances().values():
            try:
                if server.is_authenticated():
                    server.disconnect()
            except Exception as err:
                logger.error(f"停止飞牛影视模块实例失败：{err}")

    def _test_server(self, server: TrimeMedia, name: Optional[str]) -> Optional[str]:
        """
        飞牛影视用配置完整性与重连结果探测连接状态。
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
        保留飞牛 Webhook 路由入口，当前客户端不解析事件。

        :param body: 传给客户端的请求体
        :param form: 兼容统一接口的表单，当前不使用
        :param args: 请求参数，source 可指定实例
        :return: 当前客户端始终返回 None
        """
        source = args.get("source")
        if source:
            server: Optional[TrimeMedia] = self.get_instance(source)
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
        汇总飞牛实例的媒体与用户数量。

        :param server: 指定实例，省略时汇总所有飞牛实例
        :return: 统计列表；指定实例不可用时返回 None，非管理员用户数记为 0
        """
        servers: Iterable[TrimeMedia]
        if server:
            server_obj: Optional[TrimeMedia] = self.get_instance(server)
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
        查询用户可见的媒体库。

        :param server: 媒体服务器配置名称，空值使用默认实例
        :param hidden: 是否隐藏同步范围外的媒体库
        :param kwargs: 兼容统一媒体服务器接口的额外参数，当前不使用
        :return: 媒体库列表；实例不可用或查询失败时返回 None
        """
        server_obj: Optional[TrimeMedia] = self.get_instance(server)
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
        返回飞牛媒体库的递归条目生成器。

        :param server: 媒体服务器配置名称，空值使用默认实例
        :param library_id: 媒体库 guid
        :param start_index: 传给客户端的零基页序号，当前并非条目偏移
        :param limit: 服务端每页数量，None 或 -1 表示不限
        :return: 电影和电视剧条目生成器；实例不存在时返回 None，底层查询失败可能提前结束迭代
        """
        server_obj: Optional[TrimeMedia] = self.get_instance(server)
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
        server_obj: Optional[TrimeMedia] = self.get_instance(server)
        if server_obj:
            return server_obj.get_items_count(library_id)
        return None

    def mediaserver_iteminfo(
        self, server: str, item_id: str
    ) -> Optional[_SchemaMediaServerItem]:
        """
        查询单个飞牛条目的统一详情。

        :param server: 媒体服务器配置名称，空值使用默认实例
        :param item_id: 飞牛媒体条目 guid
        :return: 统一媒体模型；实例不可用或请求失败时返回 None
        """
        server_obj: Optional[TrimeMedia] = self.get_instance(server)
        if server_obj:
            return server_obj.get_iteminfo(item_id)
        return None

    def mediaserver_tv_episodes(
        self, server: str, item_id: Union[str, int]
    ) -> Optional[List[_SchemaMediaServerSeasonInfo]]:
        """
        查询电视剧已经入库的季集编号。

        :param server: 媒体服务器配置名称，空值使用默认实例
        :param item_id: 飞牛媒体条目 guid
        :return: 季集列表；标识为空或实例不可用时返回 None，无可用数据时返回空列表
        """
        if not isinstance(item_id, str):
            return None
        server_obj: Optional[TrimeMedia] = self.get_instance(server)
        if not server_obj:
            return None
        _, seasoninfo = server_obj.get_tv_episodes(item_id=item_id)
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
        查询同步范围内的继续观看条目。

        :param server: 媒体服务器配置名称，空值使用默认实例
        :param count: 最多保留条目数，None 表示不限
        :param kwargs: 兼容统一媒体服务器接口的额外参数，当前不使用
        :return: 播放列表；实例不可用或查询失败时返回 None
        """
        server_obj: Optional[TrimeMedia] = self.get_instance(server)
        if not server_obj:
            return None
        return server_obj.get_resume(num=count)

    def mediaserver_play_url(
        self, server: str, item_id: Union[str, int]
    ) -> Optional[str]:
        """
        构造飞牛媒体条目的网页播放链接。

        :param server: 媒体服务器配置名称，空值使用默认实例
        :param item_id: 飞牛媒体条目 guid
        :return: 网页 URL；标识、实例或条目不可用时返回 None
        """
        if not isinstance(item_id, str):
            return None
        server_obj: Optional[TrimeMedia] = self.get_instance(server)
        if not server_obj:
            return None
        return server_obj.get_play_url(item_id)

    def mediaserver_latest(
        self,
        server: Optional[str] = None,
        count: Optional[int] = 20,
        **kwargs: object,
    ) -> Optional[List[_SchemaMediaServerPlayItem]]:
        """
        查询同步范围内最近创建的媒体条目。

        :param server: 媒体服务器配置名称，空值使用默认实例
        :param count: 最多保留条目数，默认 20
        :param kwargs: 兼容统一媒体服务器接口的额外参数，当前不使用
        :return: 最新媒体列表；实例不可用或请求失败时返回 None
        """
        server_obj: Optional[TrimeMedia] = self.get_instance(server)
        if not server_obj:
            return None
        return server_obj.get_latest(num=count)

    def mediaserver_latest_images(
        self,
        server: Optional[str] = None,
        count: Optional[int] = 20,
        remote: Optional[bool] = False,
        **kwargs: object,
    ) -> List[str]:
        """
        查询最新媒体的背景图或海报地址。

        :param server: 媒体服务器配置名称，空值使用默认实例
        :param count: 最多返回图片数
        :param remote: 是否优先使用外网播放地址
        :param kwargs: 兼容统一媒体服务器接口的额外参数，当前不使用
        :return: 图片 URL 列表；实例不可用或无可用图片时返回空列表，访问可能需要认证 Cookie
        """
        server_obj: Optional[TrimeMedia] = self.get_instance(server)
        if not server_obj:
            return []
        return server_obj.get_latest_backdrops(num=count, remote=remote) or []

    def mediaserver_image_cookies(
        self,
        server: Optional[str] = None,
        image_url: Optional[str] = None,
        **kwargs: object,
    ) -> Optional[dict[str, Optional[str]]]:
        """
        为指定图片查找同源飞牛实例的授权 Cookie。

        :param server: 媒体服务器配置名称，空值使用默认实例
        :param image_url: 待访问的图片地址
        :param kwargs: 兼容统一媒体服务器接口的额外参数，当前不使用
        :return: 包含登录及可选访问码凭据的 Cookie；地址为空或无允许该地址的实例时返回 None
        """
        if not image_url:
            return None
        if server:
            server_obj = self.get_instance(server)
            if not server_obj:
                return None
            return server_obj.get_image_cookies(image_url)
        else:
            for server_obj in self.get_instances().values():
                if cookies := server_obj.get_image_cookies(image_url):
                    return cookies
        return None
