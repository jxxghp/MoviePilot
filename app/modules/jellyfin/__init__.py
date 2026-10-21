"""
Jellyfin 模块，将媒体库查询和事件解析接入统一媒体服务器接口。
"""

from typing import Any, Dict, Generator, Iterable, List, Optional, Tuple, Union

from app.modules._base.mediaserver import _MediaServerModuleBase
from app.modules.jellyfin.jellyfin import Jellyfin
from app.runtime.log import logger
from app.schemas.dashboard import Statistic as _SchemaStatistic
from app.schemas.mediaserver import MediaServerField, MediaServerQueryOptions
from app.schemas.mediaserver import MediaServerItem as _SchemaMediaServerItem
from app.schemas.mediaserver import MediaServerLibrary as _SchemaMediaServerLibrary
from app.schemas.mediaserver import MediaServerPlayItem as _SchemaMediaServerPlayItem
from app.schemas.mediaserver import MediaServerSeasonInfo as _SchemaMediaServerSeasonInfo
from app.schemas.mediaserver import WebhookEventInfo as _SchemaWebhookEventInfo
from app.schemas.types import MediaServerType, MediaType, ModuleType


class JellyfinModule(_MediaServerModuleBase[Jellyfin]):
    """
    Jellyfin 媒体服务器模块。
    """

    # 媒体库标识（ExistMediaInfo.server_type）
    _server_type_value = "jellyfin"
    _query_options_supported = True

    def init_module(self) -> None:
        """
        初始化模块。
        """
        super().init_service(service_name=Jellyfin.__name__.lower(),
                             service_type=lambda conf: Jellyfin(**conf.config, sync_libraries=conf.sync_libraries))

    @staticmethod
    def get_name() -> str:
        """
        返回模块展示名称。
        """
        return "Jellyfin"

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
        return MediaServerType.Jellyfin

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效。
        """
        return 2

    def init_setting(self) -> Optional[Tuple[str, Union[str, bool]]]:
        """
        由媒体服务器实例配置控制启用，不声明独立模块开关。

        :return: 始终为 None，表示不检查模块级配置项
        """
        pass

    def stop(self) -> None:
        """
        保留模块停止入口；本实现没有额外停止动作。
        """
        pass

    def webhook_parser(self, body: Any, form: Any, args: Any) -> Optional[_SchemaWebhookEventInfo]:
        """
        路由 Webhook 到来源服务器，并返回首个成功解析的事件。

        :param body: 原始请求体，作为 Jellyfin JSON 事件内容
        :param form: 表单数据，Jellyfin 当前不使用
        :param args: 请求参数；source 指定实例，省略时尝试本类型全部实例
        :return: 统一事件模型；指定实例不存在或所有实例均无法解析时返回 None
        """
        source = args.get("source")
        if source:
            server = self.get_instance(source)
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

    def media_statistic(self, server: Optional[str] = None) -> Optional[List[_SchemaStatistic]]:
        """
        汇总指定实例或本类型全部实例的媒体与用户数量。

        :param server: 服务器配置名称；省略时汇总全部实例
        :return: 统计列表；指定实例不存在时返回 None，无法获取统计的实例被跳过
        """
        servers: Iterable[Jellyfin]
        if server:
            server_obj = self.get_instance(server)
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
        self,
        server: Optional[str] = None,
        username: Optional[str] = None,
        hidden: Optional[bool] = False,
        *,
        query: Optional[MediaServerQueryOptions] = None,
    ) -> Optional[List[_SchemaMediaServerLibrary]]:
        """
        读取用户可见媒体库。

        :param server: 服务器配置名称，空值使用默认实例
        :param username: 用于限定可见内容的用户名，省略时使用当前实例的查询用户
        :param hidden: 是否隐藏未选入同步配置的库
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :return: 统一媒体库列表；实例不可用或查询失败时返回 None
        """
        server_obj = self.get_instance(server)
        if server_obj:
            return server_obj.get_librarys(
                username=username, hidden=hidden, **MediaServerQueryOptions.as_kwargs(query)
            )
        return None

    def mediaserver_items(
        self,
        server: str,
        library_id: Union[str, int],
        start_index: Optional[int] = 0,
        limit: Optional[int] = -1,
        *,
        query: Optional[MediaServerQueryOptions] = None,
    ) -> Optional[Generator[Optional[_SchemaMediaServerItem], None, None]]:
        """
        返回媒体库条目的迭代器，按需分页并递归展开目录。

        :param server: 服务器配置名称
        :param library_id: 要读取的媒体库标识
        :param start_index: 当前层分页起始索引，limit 不限制数量时不使用
        :param limit: 当前层请求数量；None 或 -1 不限制，子目录递归读取全部条目
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :return: 统一媒体条目的生成器；实例不存在时返回 None，迭代时的读取错误向调用方传播
        """
        server_obj = self.get_instance(server)
        if server_obj:
            return server_obj.get_items(
                library_id, start_index, limit, **MediaServerQueryOptions.as_kwargs(query)
            )
        return None

    def mediaserver_items_count(self, server: str, library_id: Union[str, int]) -> Optional[int]:
        """
        获取指定媒体库可同步的媒体条目总数。

        :param server: 媒体服务器名称
        :param library_id: 媒体库ID
        :return: 媒体条目总数，查询失败时返回None
        """
        server_obj = self.get_instance(server)
        if server_obj:
            return server_obj.get_items_count(library_id)
        return None

    def mediaserver_iteminfo(
        self, server: str, item_id: str, *, query: Optional[MediaServerQueryOptions] = None
    ) -> Optional[_SchemaMediaServerItem]:
        """
        查询单个媒体条目的详细信息。

        :param server: 服务器配置名称，空值使用默认实例
        :param item_id: Jellyfin 条目标识
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :return: 统一条目模型；实例、条目不存在或请求失败时返回 None
        """
        server_obj = self.get_instance(server)
        if server_obj:
            return server_obj.get_iteminfo(item_id, **MediaServerQueryOptions.as_kwargs(query))
        return None

    def mediaserver_tv_episodes(
        self, server: str, item_id: Union[str, int], *, query: Optional[MediaServerQueryOptions] = None
    ) -> Optional[List[_SchemaMediaServerSeasonInfo]]:
        """
        查询电视剧中各季已经入库的集号。

        :param server: 服务器配置名称，空值使用默认实例
        :param item_id: Jellyfin 条目标识
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :return: 按季组织的集号列表；实例不存在时返回 None，无分集数据或查询失败时返回空列表
        """
        server_obj = self.get_instance(server)
        if not server_obj:
            return None
        _, seasoninfo = server_obj.get_tv_episodes(
            item_id=str(item_id) if item_id is not None else None,
            **MediaServerQueryOptions.as_kwargs(query),
        )
        if not seasoninfo:
            return []
        return [
            _SchemaMediaServerSeasonInfo(season=season, episodes=episodes) for season, episodes in seasoninfo.items()
        ]

    def mediaserver_playing(
        self,
        server: str,
        count: Optional[int] = 20,
        username: Optional[str] = None,
        *,
        query: Optional[MediaServerQueryOptions] = None,
    ) -> Optional[List[_SchemaMediaServerPlayItem]]:
        """
        查询用户可继续观看的媒体列表。

        :param server: 服务器配置名称，空值使用默认实例
        :param count: 最多保留的条目数，默认 20
        :param username: 用于限定可见内容的用户名，省略时使用当前实例的查询用户
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :return: 继续观看条目列表；实例不可用或查询失败时返回 None
        """
        server_obj = self.get_instance(server)
        if not server_obj:
            return None
        return server_obj.get_resume(num=count, username=username, **MediaServerQueryOptions.as_kwargs(query))

    def mediaserver_play_url(self, server: str, item_id: Union[str, int]) -> Optional[str]:
        """
        构造媒体条目的网页播放链接。

        :param server: 服务器配置名称，空值使用默认实例
        :param item_id: Jellyfin 条目标识
        :return: 播放 URL；实例不存在时返回 None
        """
        server_obj = self.get_instance(server)
        if not server_obj:
            return None
        return server_obj.get_play_url(str(item_id))

    def mediaserver_season_episode_ids(
        self, server: str, item_id: Union[str, int], season: int, *, query: Optional[MediaServerQueryOptions] = None
    ) -> Optional[Dict[int, str]]:
        """
        获取指定季的集号到条目 ID 映射。

        :param server: Jellyfin 媒体服务器名称
        :param item_id: 剧集在 Jellyfin 中的条目 ID
        :param season: 季号
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :return: 集号到条目 ID 的映射，服务器不可用或无数据时返回 None
        """
        server_obj = self.get_instance(server)
        if not server_obj:
            return None
        return server_obj.get_season_episode_ids(
            str(item_id), season, **MediaServerQueryOptions.as_kwargs(query)
        )

    def mediaserver_latest(
        self,
        server: Optional[str] = None,
        count: Optional[int] = 20,
        username: Optional[str] = None,
        *,
        query: Optional[MediaServerQueryOptions] = None,
    ) -> Optional[List[_SchemaMediaServerPlayItem]]:
        """
        查询用户最近入库的媒体列表。

        :param server: 服务器配置名称，空值使用默认实例
        :param count: 最多查询的条目数，默认 20
        :param username: 用于限定可见内容的用户名，省略时使用当前实例的查询用户
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :return: 最新媒体列表；实例不可用或查询失败时返回 None
        """
        server_obj = self.get_instance(server)
        if not server_obj:
            return None
        return server_obj.get_latest(num=count, username=username, **MediaServerQueryOptions.as_kwargs(query))

    def mediaserver_latest_images(
        self,
        server: Optional[str] = None,
        count: Optional[int] = 20,
        username: Optional[str] = None,
        remote: Optional[bool] = False,
        *,
        query: Optional[MediaServerQueryOptions] = None,
    ) -> List[str]:
        """
        获取媒体服务器最新入库条目的图片。

        :param server: 媒体服务器名称
        :param count: 获取数量
        :param username: 用户名
        :param remote: True为外网链接, False为内网链接
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :return: 图片链接列表
        """
        if query is not None and not query.includes(MediaServerField.IMAGES):
            return []
        server_obj = self.get_instance(server)
        if not server_obj:
            return []

        links = []
        items = (
            self.mediaserver_latest(
                server=server, count=count, username=username, **MediaServerQueryOptions.as_kwargs(query)
            )
            or []
        )
        for item in items:
            if item.type not in (MediaType.MOVIE.value, MediaType.TV.value):
                continue
            if item.BackdropImageTags:
                image_url = server_obj.get_backdrop_url(
                    item_id=str(item.id), image_tag=item.BackdropImageTags[0], remote=remote
                )
                if image_url:
                    links.append(image_url)
        return links
