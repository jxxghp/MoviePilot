"""
Plex 模块，将认证、媒体库查询和播放事件接入统一媒体服务器接口。
"""

from typing import Any, Dict, Generator, Iterable, List, Optional, Tuple, Union

from app.application.mediaserver import MusicMediaServerHelper
from app.domain.context import MediaInfo, MusicInfo
from app.modules._base.mediaserver import _MediaServerModuleBase
from app.modules.plex.plex import Plex
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
from app.schemas.mediaserver import WebhookEventInfo as _SchemaWebhookEventInfo
from app.schemas.types import ChainEventType, MediaServerType, MediaType, ModuleType


class PlexModule(_MediaServerModuleBase[Plex]):
    """
    Plex 媒体服务器模块。
    """

    # 媒体库标识（ExistMediaInfo.server_type）
    _server_type_value = "plex"

    def init_module(self) -> None:
        """
        初始化模块。
        """
        super().init_service(service_name=Plex.__name__.lower(),
                             service_type=lambda conf: Plex(**conf.config, sync_libraries=conf.sync_libraries))

    @staticmethod
    def get_name() -> str:
        """
        返回媒体服务器模块名称。
        """
        return "Plex"

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
        return MediaServerType.Plex

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效。
        """
        return 3

    def stop(self) -> None:
        """
        关闭各实例的自定义 HTTP 会话，单个关闭失败不阻止其余实例清理。
        """
        for server in self.get_instances().values():
            try:
                if server:
                    server.close()
            except Exception as err:
                logger.error(f"停止Plex模块实例失败：{err}")

    def _test_server(self, server: Plex, name: Optional[str]) -> Optional[str]:
        """
        Plex 用媒体库列表探测连接状态。
        """
        if server.is_inactive():
            server.reconnect()
        if not server.get_librarys():
            return f"无法连接Plex服务器：{name}"
        return None

    def init_setting(self) -> Optional[Tuple[str, Union[str, bool]]]:
        """
        模块不提供额外的初始化开关。
        """
        pass

    def user_authenticate(self, credentials: AuthCredentials, service_name: Optional[str] = None) \
            -> Optional[AuthCredentials]:
        """
        通过 Plex 账号完成辅助认证，并验证服务器访问能力。

        每次尝试前发送认证拦截事件；被取消的实例不认证。邮箱登录成功后用户名替换为 Plex 规范用户名。

        :param credentials: 密码授权凭据；成功后原地更新令牌、渠道、服务名和规范用户名
        :param service_name: 要认证的实例名称；省略时依次尝试全部实例
        :return: 成功时返回修改后的原凭据；不支持的授权类型、无可用实例或认证失败时返回 None
        """
        # Plex认证
        if not credentials or credentials.grant_type != "password":
            return None
        if credentials.username is None or credentials.password is None:
            return None
        # 确定要认证的服务器列表
        servers: Iterable[tuple[Optional[str], Plex]]
        if service_name:
            # 如果指定了服务名，获取该服务实例
            servers = [(service_name, server)] if (server := self.get_instance(service_name)) else []
        else:
            # 如果没有指定服务名，遍历所有服务
            servers = self.get_instances().items()
        # 遍历要认证的服务器
        for name, server in servers:
            service_name = name or self.get_name()
            # 触发认证拦截事件
            intercept_event = eventmanager.send_event(
                etype=ChainEventType.AuthIntercept,
                data=AuthInterceptCredentials(username=credentials.username, channel=self.get_name(),
                                              service=service_name, status="triggered")
            )
            if intercept_event and isinstance(intercept_event.event_data, AuthInterceptCredentials):
                intercept_data = intercept_event.event_data
                if intercept_data.cancel:
                    continue
            auth_result = server.authenticate(credentials.username, credentials.password)
            if auth_result:
                token, username = auth_result
                credentials.channel = self.get_name()
                credentials.service = name
                credentials.token = token
                # Plex 传入可能为邮箱，这里调整为用户名返回
                credentials.username = username
                return credentials
        return None

    def webhook_parser(self, body: Any, form: Any, args: Any) -> Optional[_SchemaWebhookEventInfo]:
        """
        路由 Plex Webhook 到对应服务器。

        :param body: 为统一接口保留的请求体，Plex 使用表单 payload
        :param form: 包含 payload JSON 字符串的表单
        :param args: 查询参数；source 指定实例，省略时尝试所有实例
        :return: 首个成功解析的统一事件；指定实例不存在或所有实例均解析失败时返回 None
        """
        source = args.get("source")
        if source:
            server = self.get_instance(source)
            if not server:
                return None
            result = server.get_webhook_message(form)
            if result:
                result.server_name = source
            return result

        for server in self.get_instances().values():
            if server:
                result = server.get_webhook_message(form)
                if result:
                    return result
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
        按媒体类型查询首个匹配的入库记录。

        :param mediainfo: 识别后的媒体信息，用于名称、年份和来源身份匹配
        :param itemid: 缓存条目标识；电影优先直接查详情，剧集失效后可回退搜索
        :param server: 指定实例名称，省略时依次查询全部实例
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :return: 存在信息，电视剧结果包含季集映射；没有匹配记录时返回 None
        """
        servers: Iterable[tuple[Optional[str], Optional[Plex]]]
        if server:
            servers = [(server, self.get_instance(server))]
        else:
            servers = self.get_instances().items()
        for name, s in servers:
            if not s:
                continue
            if mediainfo.type == MediaType.MUSIC:
                if not isinstance(mediainfo, MusicInfo):
                    continue
                search_params = MusicMediaServerHelper.search_params(mediainfo)
                matches = s.get_music(
                    title=search_params["title"],
                    artist=search_params["artist"],
                    album=search_params["album"],
                    **({"query": query.with_fields(MediaServerField.MUSIC)} if query is not None else {}),
                )
                match = MusicMediaServerHelper.find_match(mediainfo, matches)
                if match:
                    return _SchemaExistMediaInfo(
                        type=MediaType.MUSIC,
                        server_type="plex",
                        server=name,
                        itemid=match.item_id,
                    )
                continue
            if mediainfo.type == MediaType.MOVIE:
                if itemid:
                    movie = s.get_iteminfo(itemid, **MediaServerQueryOptions.as_kwargs(query))
                    if movie:
                        logger.info(f"媒体库 {name} 中找到了 {movie}")
                        return _SchemaExistMediaInfo(
                            type=MediaType.MOVIE, server_type="plex", server=name, itemid=movie.item_id
                        )
                movies = s.get_movies(
                    title=mediainfo.title or "",
                    original_title=mediainfo.original_title,
                    year=mediainfo.year,
                    media_source=mediainfo.media_source,
                    media_id=mediainfo.media_id,
                    **MediaServerQueryOptions.as_kwargs(query),
                )
                if not movies:
                    logger.info(f"{mediainfo.title_year} 没有在媒体库 {name} 中")
                    continue
                else:
                    logger.info(f"媒体库 {name} 中找到了 {movies}")
                    return _SchemaExistMediaInfo(
                        type=MediaType.MOVIE, server_type="plex", server=name, itemid=movies[0].item_id
                    )
            else:
                item_id, tvs = s.get_tv_episodes(
                    title=mediainfo.title,
                    original_title=mediainfo.original_title,
                    year=mediainfo.year,
                    media_source=mediainfo.media_source,
                    media_id=mediainfo.media_id,
                    item_id=itemid,
                    **MediaServerQueryOptions.as_kwargs(query),
                )
                if not tvs:
                    logger.info(f"{mediainfo.title_year} 没有在媒体库 {name} 中")
                    continue
                else:
                    logger.info(f"{mediainfo.title_year} 在媒体库 {name} 中找到了这些季集：{tvs}")
                    return _SchemaExistMediaInfo(
                        type=MediaType.TV, seasons=tvs, server_type="plex", server=name, itemid=item_id
                    )
        return None

    def media_statistic(self, server: Optional[str] = None) -> Optional[List[_SchemaStatistic]]:
        """
        汇总指定实例或全部实例的媒体数量。

        :param server: 实例名称，省略时查询全部实例
        :return: 统计列表；指定实例不存在时返回 None，每个有效统计的用户数固定记为 1
        """
        servers: Iterable[Plex]
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
            media_statistic.user_count = 1
            media_statistics.append(media_statistic)
        return media_statistics

    def mediaserver_librarys(
        self,
        server: Optional[str] = None,
        hidden: Optional[bool] = False,
        query: Optional[MediaServerQueryOptions] = None,
        **kwargs: object,
    ) -> Optional[List[_SchemaMediaServerLibrary]]:
        """
        查询可用的电影、剧集和音乐库。

        :param server: 服务器配置名称，空值使用默认实例
        :param hidden: 是否隐藏同步范围外的媒体库
        :param kwargs: 兼容其他媒体服务器的额外参数，当前不使用
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :return: 统一媒体库列表；实例不可用或库目录查询失败时返回 None
        """
        server_obj = self.get_instance(server)
        if server_obj:
            return server_obj.get_librarys(hidden, **MediaServerQueryOptions.as_kwargs(query))
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
        返回媒体库同步条目的生成器。

        :param server: 服务器配置名称，空值使用默认实例
        :param library_id: Plex 媒体库数字标识
        :param start_index: 起始条目索引，空值或负值按 0 处理
        :param limit: 本次最多读取条目数；None 或 -1 表示分页读取剩余全部条目
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :return: 统一条目生成器；实例不存在时返回 None，迭代中的查询错误向上传播
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
        读取单个 Plex 条目详情。

        :param server: 服务器配置名称，空值使用默认实例
        :param item_id: Plex 条目标识，可以是数字标识或 /library/metadata/ 路径
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :return: 统一媒体模型；实例不存在、条目无效或查询失败时返回 None
        """
        server_obj = self.get_instance(server)
        if server_obj:
            return server_obj.get_iteminfo(item_id, **MediaServerQueryOptions.as_kwargs(query))
        return None

    def mediaserver_tv_episodes(
        self, server: str, item_id: Union[str, int], *, query: Optional[MediaServerQueryOptions] = None
    ) -> Optional[List[_SchemaMediaServerSeasonInfo]]:
        """
        读取电视剧中各季已存在的分集编号。

        :param server: 服务器配置名称，空值使用默认实例
        :param item_id: Plex 条目标识，可以是数字标识或 /library/metadata/ 路径
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :return: 季集列表；实例不存在时返回 None，无可用季集时返回空列表
        """
        server_obj = self.get_instance(server)
        if not server_obj:
            return None
        _, seasoninfo = server_obj.get_tv_episodes(item_id=str(item_id), **MediaServerQueryOptions.as_kwargs(query))
        if not seasoninfo:
            return []
        return [
            _SchemaMediaServerSeasonInfo(season=season, episodes=episodes) for season, episodes in seasoninfo.items()
        ]

    def mediaserver_playing(
        self, server: str, count: Optional[int] = 20, query: Optional[MediaServerQueryOptions] = None, **kwargs: object
    ) -> Optional[List[_SchemaMediaServerPlayItem]]:
        """
        查询同步范围内的继续观看列表。

        :param server: 服务器配置名称，空值使用默认实例
        :param count: 最多返回的条目数量，默认 20
        :param kwargs: 兼容其他媒体服务器的额外参数，当前不使用
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :return: 继续观看条目列表；实例不可用或无法读取媒体库范围时返回 None
        """
        server_obj = self.get_instance(server)
        if not server_obj:
            return None
        return server_obj.get_resume(num=count, **MediaServerQueryOptions.as_kwargs(query))

    def mediaserver_latest(
        self,
        server: Optional[str] = None,
        count: Optional[int] = 20,
        query: Optional[MediaServerQueryOptions] = None,
        **kwargs: object,
    ) -> Optional[List[_SchemaMediaServerPlayItem]]:
        """
        查询同步范围内最近新增的媒体。

        :param server: 服务器配置名称，空值使用默认实例
        :param count: 最多返回的条目数量，默认 20
        :param kwargs: 兼容其他媒体服务器的额外参数，当前不使用
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :return: 最新媒体列表；实例不可用或无法读取媒体库范围时返回 None
        """
        server_obj = self.get_instance(server)
        if not server_obj:
            return None
        return server_obj.get_latest(num=count, **MediaServerQueryOptions.as_kwargs(query))

    def mediaserver_latest_images(
        self,
        server: Optional[str] = None,
        count: Optional[int] = 20,
        username: Optional[str] = None,
        query: Optional[MediaServerQueryOptions] = None,
        **kwargs: object,
    ) -> List[str]:
        """
        为最新电影和电视剧查找可直接访问的外部背景图。

        :param server: 服务器配置名称，空值使用默认实例
        :param count: 最多检查的最新媒体数量，默认 20
        :param username: 兼容统一接口的用户名，当前不参与用户筛选
        :param kwargs: 兼容其他媒体服务器的额外参数，当前不使用
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :return: 外部图片 URL 列表；不回退含 Plex 访问令牌的本地图片，无可用图片时返回空列表
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
            if not item.id:
                continue
            link = server_obj.get_remote_image_by_id(
                item_id=str(item.id),
                image_type="Backdrop",
                plex_url=False,
                **MediaServerQueryOptions.as_kwargs(query),
            )
            if link:
                links.append(link)
        return links

    def mediaserver_play_url(self, server: str, item_id: Union[str, int]) -> Optional[str]:
        """
        构造带 Plex 访问令牌的网页播放链接。

        :param server: 服务器配置名称，空值使用默认实例
        :param item_id: Plex 条目标识，可以是数字标识或 /library/metadata/ 路径
        :return: 条目播放 URL；实例不存在时返回 None
        """
        server_obj = self.get_instance(server)
        if not server_obj:
            return None
        return server_obj.get_play_url(str(item_id))

    def mediaserver_season_episode_ids(
        self, server: str, item_id: Union[str, int], season: int, *, query: Optional[MediaServerQueryOptions] = None
    ) -> Optional[Dict[int, str]]:
        """
        读取指定季的集号到条目路径映射。

        :param server: 服务器配置名称，空值使用默认实例
        :param item_id: Plex 条目标识，可以是数字标识或 /library/metadata/ 路径
        :param season: 目标季号
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :return: 集号到 Plex 条目路径的映射；实例不存在时返回 None，无分集或查询失败时返回空映射
        """
        server_obj = self.get_instance(server)
        if not server_obj:
            return None
        return server_obj.get_season_episode_ids(
            str(item_id), season, **MediaServerQueryOptions.as_kwargs(query)
        )
