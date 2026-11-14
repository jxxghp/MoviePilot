"""
极影视媒体服务器模块，将客户端能力适配为统一媒体服务器接口。
"""

from typing import Any, Generator, Iterable, List, Optional, Tuple, Union

from app.modules._base.mediaserver import _MediaServerModuleBase
from app.modules.zspace.zspace import ZSpace
from app.runtime.log import logger
from app.schemas.dashboard import Statistic as _SchemaStatistic
from app.schemas.event import AuthCredentials, AuthInterceptCredentials
from app.schemas.mediaserver import MediaServerItem as _SchemaMediaServerItem
from app.schemas.mediaserver import MediaServerLibrary as _SchemaMediaServerLibrary
from app.schemas.mediaserver import MediaServerPlayItem as _SchemaMediaServerPlayItem
from app.schemas.mediaserver import MediaServerSeasonInfo as _SchemaMediaServerSeasonInfo
from app.schemas.mediaserver import WebhookEventInfo as _SchemaWebhookEventInfo
from app.schemas.types import ChainEventType, MediaServerType, MediaType, ModuleType


class ZSpaceModule(_MediaServerModuleBase[ZSpace]):
    """
    极影视媒体服务器模块。
    """

    # 媒体库标识（ExistMediaInfo.server_type）
    _server_type_value = "zspace"

    def init_module(self) -> None:
        """
        初始化模块。
        """
        super().init_service(service_name=ZSpace.__name__.lower(),
                             service_type=lambda conf: ZSpace(**conf.config, sync_libraries=conf.sync_libraries))

    @staticmethod
    def get_name() -> str:
        """
        返回媒体服务器模块名称。
        """
        return "极影视"

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
        return MediaServerType.ZSpace

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效。
        """
        return 6

    def stop(self) -> None:
        """
        该客户端没有需要额外停止的后台资源。
        """
        pass

    def _test_server(self, server: ZSpace, name: Optional[str]) -> Optional[str]:
        """
        极影视用重连结果与用户信息探测连接状态。
        """
        if server.is_inactive() and not server.reconnect():
            return f"无法连接{self.get_name()}服务器：{name}"
        if not server.user:
            return f"无法连接{self.get_name()}服务器：{name}"
        return None

    def init_setting(self) -> Optional[Tuple[str, Union[str, bool]]]:
        """
        模块不提供额外的初始化开关。
        """
        pass

    def webhook_parser(self, body: Any, form: Any, args: Any) -> Optional[_SchemaWebhookEventInfo]:
        """
        按 source 选择极影视实例并解析通知，未指定时依次尝试实例。

        :param body: 兼容统一通知接口的请求体，当前不读取
        :param form: 包含 data JSON 字符串的表单
        :param args: 查询参数，source 用于指定实例
        :return: 首个解析成功的统一事件；实例不可用或无法解析时返回 None
        """
        source = args.get("source")
        if source:
            server = self.get_instance(source)
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
        汇总指定实例或全部实例的媒体与用户数量。

        :param server: 目标实例名称，省略时汇总全部实例
        :return: 统计列表；明确指定的实例不存在时返回 None
        """
        servers: Iterable[ZSpace]
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

    def mediaserver_librarys(self, server: str,
                             username: Optional[str] = None,
                             hidden: Optional[bool] = False) -> Optional[List[_SchemaMediaServerLibrary]]:
        """
        查询极影视媒体库列表。

        :param server: 媒体服务器配置名称，空值使用默认实例
        :param username: 目标用户，空值使用当前用户
        :param hidden: 是否隐藏同步范围外的媒体库
        :return: 媒体库列表；实例不存在或查询失败时返回 None
        """
        server_obj = self.get_instance(server)
        if server_obj:
            return server_obj.get_librarys(username=username, hidden=hidden)
        return None

    def mediaserver_items(self, server: str, library_id: Union[str, int], start_index: Optional[int] = 0,
                          limit: Optional[int] = -1) -> Optional[Generator[Optional[_SchemaMediaServerItem], None, None]]:
        """
        返回媒体库同步条目的迭代器。

        :param server: 媒体服务器配置名称，空值使用默认实例
        :param library_id: 媒体库标识
        :param start_index: 零基起始偏移，空值或负数按 0 处理
        :param limit: None 或 -1 表示分批读取全部；其他值只请求一页
        :return: 条目生成器，合集会展开；实例不存在时返回 None，请求失败可能提前结束迭代
        """
        server_obj = self.get_instance(server)
        if server_obj:
            return server_obj.get_items(library_id, start_index, limit)
        return None

    def mediaserver_items_count(self, server: str, library_id: Union[str, int]) -> Optional[int]:
        """
        查询媒体库递归条目总数，可能包含无法同步的类型。

        :param server: 媒体服务器配置名称，空值使用默认实例
        :param library_id: 媒体库标识
        :return: 服务端总数；实例不存在或查询失败时返回 None
        """
        server_obj = self.get_instance(server)
        if server_obj:
            return server_obj.get_items_count(library_id)
        return None

    def mediaserver_iteminfo(self, server: str, item_id: str) -> Optional[_SchemaMediaServerItem]:
        """
        读取单条极影视媒体详情。

        :param server: 媒体服务器配置名称，空值使用默认实例
        :param item_id: 极影视条目标识
        :return: 统一媒体模型；实例不可用或查询失败时返回 None
        """
        server_obj = self.get_instance(server)
        if server_obj:
            return server_obj.get_iteminfo(item_id)
        return None

    def mediaserver_tv_episodes(self, server: str,
                                item_id: Union[str, int]) -> Optional[List[_SchemaMediaServerSeasonInfo]]:
        """
        查询电视剧中各季已有的集号。

        :param server: 媒体服务器配置名称，空值使用默认实例
        :param item_id: 极影视条目标识
        :return: 季集列表；实例不存在时返回 None，客户端未返回季集时为空列表
        """
        server_obj = self.get_instance(server)
        if not server_obj:
            return None
        _, seasoninfo = server_obj.get_tv_episodes(
            item_id=str(item_id) if item_id is not None else None
        )
        if not seasoninfo:
            return []
        return [_SchemaMediaServerSeasonInfo(
            season=season,
            episodes=episodes
        ) for season, episodes in seasoninfo.items()]

    def mediaserver_playing(self, server: str, count: Optional[int] = 20,
                            username: Optional[str] = None) -> Optional[List[_SchemaMediaServerPlayItem]]:
        """
        读取当前用户的继续观看列表。

        :param server: 媒体服务器配置名称，空值使用默认实例
        :param count: 目标数量，0 返回空列表，None 不截断已读取结果
        :param username: 目标用户名，空值使用当前用户
        :return: 统一播放列表；实例不可用或查询失败时返回 None
        """
        server_obj = self.get_instance(server)
        if not server_obj:
            return None
        return server_obj.get_resume(num=count, username=username)

    def mediaserver_play_url(self, server: str, item_id: Union[str, int]) -> Optional[str]:
        """
        构造极影视条目的网页播放链接。

        :param server: 媒体服务器配置名称，空值使用默认实例
        :param item_id: 极影视条目标识
        :return: 网页 URL；实例不存在时返回 None
        """
        server_obj = self.get_instance(server)
        if not server_obj:
            return None
        return server_obj.get_play_url(str(item_id))

    def mediaserver_latest(self, server: Optional[str] = None, count: Optional[int] = 20,
                           username: Optional[str] = None) -> Optional[List[_SchemaMediaServerPlayItem]]:
        """
        查询最近入库的电影和电视剧。

        :param server: 媒体服务器配置名称，空值使用默认实例
        :param count: 目标数量，0 返回空列表，None 不截断已读取结果
        :param username: 目标用户名，空值使用当前用户
        :return: 统一播放列表；实例不可用或查询失败时返回 None
        """
        server_obj = self.get_instance(server)
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
        从最新入库电影和电视剧中提取有背景图标签的图片链接。

        :param server: 媒体服务器配置名称，空值使用默认实例
        :param count: 用于查询最新条目的数量，图片可能少于此数量
        :param username: 目标用户名，空值使用当前用户
        :param remote: 是否优先使用外网播放地址
        :return: 带登录令牌的图片链接列表；实例不存在或无匹配图片时为空列表
        """
        server_obj = self.get_instance(server)
        if not server_obj:
            return []

        links = []
        items = self.mediaserver_latest(server=server, count=count, username=username) or []
        for item in items:
            if item.type not in (MediaType.MOVIE.value, MediaType.TV.value):
                continue
            if item.BackdropImageTags:
                image_url = server_obj.get_backdrop_url(item_id=str(item.id),
                                                        image_tag=item.BackdropImageTags[0],
                                                        remote=remote)
                if image_url:
                    links.append(image_url)
        return links
