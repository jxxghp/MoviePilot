"""
Navidrome 媒体服务器模块。
"""

from typing import Any, Dict, Generator, List, Optional, Tuple, Union

from app.application.mediaserver import MusicMediaServerHelper
from app.domain.context import MediaInfo, MusicInfo
from app.modules import _MediaServerBase, _ModuleBase
from app.modules.navidrome.navidrome import Navidrome
from app.runtime.events import eventmanager
from app.runtime.log import logger
from app.schemas.dashboard import Statistic as _SchemaStatistic
from app.schemas.event import AuthCredentials, AuthInterceptCredentials
from app.schemas.mediaserver import ExistMediaInfo as _SchemaExistMediaInfo
from app.schemas.mediaserver import MediaServerField, MediaServerQueryOptions
from app.schemas.mediaserver import MediaServerItem as _SchemaMediaServerItem
from app.schemas.mediaserver import MediaServerLibrary as _SchemaMediaServerLibrary
from app.schemas.mediaserver import MediaServerPlayItem as _SchemaMediaServerPlayItem
from app.schemas.mediaserver import MediaServerSeasonInfo as _SchemaMediaServerSeasonInfo
from app.schemas.types import ChainEventType, MediaServerType, MediaType, ModuleType


class NavidromeModule(_ModuleBase, _MediaServerBase[Navidrome]):
    """
    将 Navidrome 接入 MoviePilot 媒体服务器统一接口。
    """

    def init_module(self) -> None:
        """
        读取配置并初始化启用的 Navidrome 实例。
        """
        super().init_service(
            service_name=Navidrome.__name__.lower(),
            service_type=lambda conf: Navidrome(**conf.config, sync_libraries=conf.sync_libraries),
        )

    @staticmethod
    def get_name() -> str:
        """
        返回模块显示名称。
        """
        return "Navidrome"

    @staticmethod
    def get_type() -> ModuleType:
        """
        返回模块类型。
        """
        return ModuleType.MediaServer

    @staticmethod
    def get_subtype() -> MediaServerType:
        """
        返回媒体服务器子类型。
        """
        return MediaServerType.Navidrome

    @staticmethod
    def get_priority() -> int:
        """
        返回模块优先级。
        """
        return 7

    def stop(self) -> None:
        """
        保留模块停止入口；本实现没有需要主动释放的额外资源。
        """

    def init_setting(self) -> Optional[Tuple[str, Union[str, bool]]]:
        """
        Navidrome 与其它媒体服务器一致，由服务配置控制启用，无需系统开关。
        """
        return None

    def scheduler_job(self) -> None:
        """
        定时检查 Navidrome 连接状态并尝试重连。
        """
        for name, server in self.get_instances().items():
            if server.is_inactive():
                logger.info(f"Navidrome {name} 服务器连接断开，尝试重连 ...")
                server.reconnect()

    def test(self) -> Optional[Tuple[bool, str]]:
        """
        逐个验证启用的 Navidrome 实例。

        :return: 全部通过时返回 True 和空说明；首个失败时返回 False 和原因；无实例时返回 None
        """
        if not self.get_instances():
            return None
        for name, server in self.get_instances().items():
            if server.is_inactive():
                server.reconnect()
            if not server.get_user():
                return False, f"无法连接Navidrome服务器：{name}"
        return True, ""

    def user_authenticate(
        self, credentials: AuthCredentials, service_name: Optional[str] = None
    ) -> Optional[AuthCredentials]:
        """
        使用 Navidrome 用户凭据完成辅助认证。

        每次尝试前发送认证拦截事件；拦截器取消的实例不会发起登录验证。

        :param credentials: 密码授权凭据；验证成功会原地填充渠道、服务名和令牌
        :param service_name: 优先验证的实例名称；未指定或名称无效时依次尝试所有实例
        :return: 验证成功时返回修改后的原凭据；授权类型不支持、全部被拦截或验证失败时返回 None
        """
        if not credentials or credentials.grant_type != "password":
            return None
        if credentials.username is None or credentials.password is None:
            return None
        servers: list[tuple[Optional[str], Optional[Navidrome]]] = (
            [(service_name, self.get_instance(service_name))]
            if service_name and self.get_instance(service_name)
            else list(self.get_instances().items())
        )
        for name, server in servers:
            if not server:
                continue
            service_name_value = name or self.get_name()
            intercept_event = eventmanager.send_event(
                etype=ChainEventType.AuthIntercept,
                data=AuthInterceptCredentials(
                    username=credentials.username,
                    channel=self.get_name(),
                    service=service_name_value,
                    status="triggered",
                ),
            )
            if (
                intercept_event
                and isinstance(intercept_event.event_data, AuthInterceptCredentials)
                and intercept_event.event_data.cancel
            ):
                continue
            token = server.authenticate(credentials.username, credentials.password)
            if token:
                credentials.channel = self.get_name()
                credentials.service = name
                credentials.token = token
                return credentials
        return None

    def media_exists(
        self,
        mediainfo: MediaInfo,
        itemid: Optional[str] = None,
        server: Optional[str] = None,
        *,
        query: Optional[MediaServerQueryOptions] = None,
    ) -> Optional[_SchemaExistMediaInfo]:
        """
        按条目标识或音乐名称检查媒体是否已入库。

        :param mediainfo: 待匹配的媒体信息，仅处理音乐
        :param itemid: 可选专辑标识，优先查询；不匹配时继续按名称搜索
        :param server: 媒体服务器配置名称；省略时汇总本类型全部实例
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :return: 首个匹配项的服务器和条目标识；非音乐或未找到匹配项时返回 None
        """
        if mediainfo.type != MediaType.MUSIC or not isinstance(mediainfo, MusicInfo):
            return None
        servers: list[tuple[Optional[str], Optional[Navidrome]]] = (
            [(server, self.get_instance(server))] if server else list(self.get_instances().items())
        )
        for name, service in servers:
            if not service:
                continue
            item = (
                service.get_iteminfo(
                    str(itemid), **({"query": query.with_fields(MediaServerField.MUSIC)} if query is not None else {})
                )
                if itemid
                else None
            )
            if item and MusicMediaServerHelper.item_matches(mediainfo, item):
                return _SchemaExistMediaInfo(
                    type=MediaType.MUSIC,
                    server_type="navidrome",
                    server=name,
                    itemid=itemid,
                )
            search_params = MusicMediaServerHelper.search_params(mediainfo)
            matches = service.search_music(
                title=search_params["title"],
                artist=search_params["artist"],
                album=search_params["album"],
                **({"query": query.with_fields(MediaServerField.MUSIC)} if query is not None else {}),
            )
            match = MusicMediaServerHelper.find_match(mediainfo, matches)
            if match:
                return _SchemaExistMediaInfo(
                    type=MediaType.MUSIC,
                    server_type="navidrome",
                    server=name,
                    itemid=match.item_id,
                )
        return None

    def media_statistic(self, server: Optional[str] = None) -> Optional[List[_SchemaStatistic]]:
        """
        汇总可用 Navidrome 实例的音乐和用户数量。

        :param server: 媒体服务器配置名称；省略时汇总本类型全部实例
        :return: 统计列表；无适用实例时返回空列表，用户数量仅表示当前凭据是否有效
        """
        servers = [self.get_instance(server)] if server else list(self.get_instances().values())
        result: List[_SchemaStatistic] = []
        for service in servers:
            if not service:
                continue
            statistic = service.get_medias_count()
            statistic.user_count = service.get_user_count()
            result.append(statistic)
        return result

    def mediaserver_librarys(
        self,
        server: str,
        username: Optional[str] = None,
        hidden: Optional[bool] = False,
        *,
        query: Optional[MediaServerQueryOptions] = None,
    ) -> Optional[List[_SchemaMediaServerLibrary]]:
        """
        查询实例提供的虚拟音乐库。

        :param server: 媒体服务器配置名称
        :param username: 为统一媒体服务器接口保留，当前使用实例配置用户
        :param hidden: 是否隐藏未被同步配置选中的音乐库
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :return: 库列表；实例不存在时返回 None
        """
        service = self.get_instance(server)
        return (
            service.get_librarys(hidden=hidden, **({"query": query} if query is not None else {})) if service else None
        )

    def mediaserver_items(
        self,
        server: str,
        library_id: Union[str, int],
        start_index: Optional[int] = 0,
        limit: Optional[int] = -1,
        *,
        query: Optional[MediaServerQueryOptions] = None,
    ) -> Optional[Generator[_SchemaMediaServerItem, None, None]]:
        """
        返回虚拟音乐库的专辑迭代器。

        :param server: 媒体服务器配置名称
        :param library_id: 统一接口的库标识，Navidrome 只有一个虚拟音乐库，当前不使用
        :param start_index: 专辑列表起始索引，默认 0
        :param limit: 最多返回条目数，-1 或 None 表示返回剩余全部条目
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :return: 专辑生成器；实例不存在时返回 None，实际查询在迭代时执行
        """
        service = self.get_instance(server)
        return (
            service.get_items(
                start_index if start_index is not None else 0,
                limit if limit is not None else -1,
                **({"query": query} if query is not None else {}),
            )
            if service
            else None
        )

    def mediaserver_items_count(self, server: str, library_id: Union[str, int]) -> Optional[int]:
        """
        查询音乐库的专辑数量。

        :param server: 媒体服务器配置名称
        :param library_id: 虚拟音乐库标识，客户端不依此值筛选
        :return: 专辑数量；实例不存在时返回 None
        """
        service = self.get_instance(server)
        return service.get_items_count(str(library_id)) if service else None

    def mediaserver_iteminfo(
        self, server: str, item_id: str, *, query: Optional[MediaServerQueryOptions] = None
    ) -> Optional[_SchemaMediaServerItem]:
        """
        查询 Navidrome 专辑详情。

        :param server: 媒体服务器配置名称
        :param item_id: Navidrome 条目标识
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :return: 统一专辑模型；实例不存在、专辑不存在或请求失败时返回 None
        """
        service = self.get_instance(server)
        return service.get_iteminfo(item_id, **({"query": query} if query is not None else {})) if service else None

    def mediaserver_tv_episodes(
        self, server: str, item_id: Union[str, int], *, query: Optional[MediaServerQueryOptions] = None
    ) -> List[_SchemaMediaServerSeasonInfo]:
        """
        为统一接口提供空剧集结果；纯音乐服务没有季集信息。

        :param server: 媒体服务器配置名称
        :param item_id: Navidrome 条目标识
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :return: 始终返回空列表
        """
        return []

    def mediaserver_playing(
        self,
        server: str,
        count: Optional[int] = 20,
        username: Optional[str] = None,
        *,
        query: Optional[MediaServerQueryOptions] = None,
    ) -> Optional[List[_SchemaMediaServerPlayItem]]:
        """
        查询服务端当前正在播放的音乐条目。

        :param server: 媒体服务器配置名称
        :param count: 最多返回条目数，默认 20
        :param username: 为统一媒体服务器接口保留，当前使用实例配置用户
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :return: 播放条目列表；实例不存在时返回 None，服务端请求失败时返回空列表
        """
        service = self.get_instance(server)
        return service.get_resume(
            count if count is not None else 20,
            **({"query": query} if query is not None else {}),
        ) if service else None

    def mediaserver_play_url(self, server: str, item_id: Union[str, int]) -> Optional[str]:
        """
        构造 Navidrome 音频流地址。

        :param server: 媒体服务器配置名称
        :param item_id: Navidrome 条目标识
        :return: 带鉴权参数的播放 URL；实例或服务地址不存在时返回 None
        """
        service = self.get_instance(server)
        return service.get_play_url(str(item_id)) if service else None

    def mediaserver_latest(
        self,
        server: Optional[str] = None,
        count: Optional[int] = 20,
        username: Optional[str] = None,
        *,
        query: Optional[MediaServerQueryOptions] = None,
    ) -> Optional[List[_SchemaMediaServerPlayItem]]:
        """
        查询指定实例最近新增的专辑。

        :param server: 服务器配置名称，省略时选择默认实例
        :param count: 最多返回条目数，默认 20
        :param username: 为统一媒体服务器接口保留，当前使用实例配置用户
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :return: 最近新增专辑列表；实例不存在时返回 None
        """
        service = self.get_instance(server)
        return service.get_latest(
            count if count is not None else 20,
            **({"query": query} if query is not None else {}),
        ) if service else None

    def mediaserver_latest_images(
        self,
        server: Optional[str] = None,
        count: Optional[int] = 10,
        username: Optional[str] = None,
        remote: Optional[bool] = False,
        *,
        query: Optional[MediaServerQueryOptions] = None,
    ) -> List[str]:
        """
        按统一壁纸规则筛选最新媒体图片。

        :param server: 服务器配置名称，省略时选择默认实例
        :param count: 最多查询的最新条目数，默认 10
        :param username: 为统一媒体服务器接口保留，当前使用实例配置用户
        :param remote: 兼容统一接口的远程图片选项，当前不使用
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :return: 仅保留电影与电视剧图片；正常 Navidrome 音乐条目会全部被排除，返回空列表
        """
        if query is not None and not query.includes(MediaServerField.IMAGES):
            return []
        return [
            item.image
            for item in (
                self.mediaserver_latest(server, count, username, **({"query": query} if query is not None else {}))
                or []
            )
            if item.type in (MediaType.MOVIE.value, MediaType.TV.value) and item.image
        ]
