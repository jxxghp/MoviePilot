"""
媒体服务器模块业务样板基类。

沉淀各媒体服务器模块逐字复制的样板：用户辅助认证、媒体存在性检查、
定时重连与连接测试。服务器差异（认证 API、存在性检查端点、连接探测方式）
通过类属性与钩子方法保留在各模块。
"""
from typing import Any, Iterable, Mapping, Optional, Tuple, cast

from app.application.mediaserver import MusicMediaServerHelper
from app.domain.context import MediaInfo, MusicInfo
from app.modules import TService, _MediaServerBase, _ModuleBase
from app.runtime.events import eventmanager
from app.runtime.log import logger
from app.schemas.event import AuthCredentials as _SchemaAuthCredentials
from app.schemas.event import AuthInterceptCredentials as _SchemaAuthInterceptCredentials
from app.schemas.mediaserver import ExistMediaInfo as _SchemaExistMediaInfo
from app.schemas.mediaserver import MediaServerField, MediaServerQueryOptions
from app.schemas.types import ChainEventType, MediaType


def emby_episode_numbers(item: Mapping[str, Any]) -> range:
    """
    展开 Emby 系分集条目的起止集号，缺少结束集号或倒序时按单集处理。

    :param item: 含 IndexNumber 与可空 IndexNumberEnd 的分集响应
    :return: 包含合并文件所覆盖全部集号的区间，未提供起始集号时为空
    """
    number = item.get("IndexNumber")
    if number is None:
        return range(0)
    start = int(number)
    end = item.get("IndexNumberEnd")
    return range(start, max(start, int(end) if end is not None else start) + 1)


def emby_query_params(query: Optional[MediaServerQueryOptions]) -> dict[str, str]:
    """
    把显式投影翻译为 Emby/Jellyfin 已公开的列表查询控制参数。

    名称、类型由服务端默认返回；年份必须通过 Fields 显式请求，避免精简查询丢失匹配和同步依据。
    Fields 只追加当前投影所需的字段。
    音乐完整性匹配需要 ChildCount，即使未请求额外统计也不能删除它。

    :param query: 调用方投影；None 或 fields=None 时不改变旧请求
    :return: 字段追加、图片信息和用户数据参数；不添加未公开的总量开关
    """
    if query is None or query.fields is None:
        return {}
    fields = {
        MediaServerField.LIBRARY: "ParentId",
        MediaServerField.ORIGINAL_TITLE: "OriginalTitle",
        MediaServerField.YEAR: "ProductionYear",
        MediaServerField.IDENTITY: "ProviderIds",
        MediaServerField.PATH: "Path",
        MediaServerField.MUSIC: "ChildCount",
    }
    return {
        "Fields": ",".join(value for field, value in fields.items() if query.includes(field)),
        "EnableImages": str(query.includes(MediaServerField.IMAGES)).lower(),
        "EnableUserData": str(query.includes(MediaServerField.USER_STATE)).lower(),
    }


class _MediaServerModuleBase(_ModuleBase, _MediaServerBase[TService]):
    """
    媒体服务器模块业务样板基类。
    """

    # 媒体库标识（用于 ExistMediaInfo.server_type，如 "emby"），子类覆写
    _server_type_value: str = ""
    # 仅已实现查询投影的适配器向原生客户端透传新参数。
    _query_options_supported: bool = False

    def user_authenticate(
            self,
            credentials: _SchemaAuthCredentials,
            service_name: Optional[str] = None,
    ) -> Optional[_SchemaAuthCredentials]:
        """
        使用媒体服务器用户辅助完成用户认证。

        :param credentials: 认证数据
        :param service_name: 指定要认证的媒体服务器名称，若为 None 则认证所有服务器
        :return: 成功时返回已写入渠道、服务名和令牌的原凭据；不适用或所有服务认证失败时返回 None
        """
        if not credentials or credentials.grant_type != "password":
            return None
        # 确定要认证的服务器列表
        servers: Iterable[tuple[Optional[str], TService]]
        if service_name:
            # 如果指定了服务名，获取该服务实例
            servers = (
                [(service_name, server)]
                if (server := self.get_instance(service_name))
                else []
            )
        else:
            # 如果没有指定服务名，遍历所有服务
            servers = self.get_instances().items()
        # 遍历要认证的服务器
        for name, server in servers:
            service_name = name or ""
            channel_name = self.get_name() or ""
            # 触发认证拦截事件
            intercept_event = eventmanager.send_event(
                etype=ChainEventType.AuthIntercept,
                data=_SchemaAuthInterceptCredentials(
                    username=credentials.username,
                    channel=channel_name,
                    service=service_name,
                    status="triggered",
                ),
            )
            if intercept_event and intercept_event.event_data:
                intercept_data = (
                    intercept_event.event_data
                    if isinstance(intercept_event.event_data, _SchemaAuthInterceptCredentials)
                    else _SchemaAuthInterceptCredentials.model_validate(intercept_event.event_data)
                )
                if intercept_data.cancel:
                    continue
            token = cast(Any, server).authenticate(credentials.username, credentials.password)
            if token:
                credentials.channel = channel_name
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
        判断媒体文件是否存在。

        :param mediainfo:  识别的媒体信息
        :param itemid:  媒体服务器ItemID
        :param server:  媒体服务器名称
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :return: 如不存在返回None，存在时返回信息，包括每季已存在所有集{type: movie/tv, seasons: {season: [episodes]}}
        """
        if not self._query_options_supported:
            query = None
        servers: Iterable[tuple[Optional[str], Optional[TService]]]
        if server:
            servers = [(server, self.get_instance(server))]
        else:
            servers = self.get_instances().items()
        for name, s in servers:
            if not s:
                continue
            if mediainfo.type == MediaType.MUSIC:
                # 部分服务器未实现音乐查询，退化为空列表
                if not isinstance(mediainfo, MusicInfo):
                    continue
                matches: list[Any] = getattr(s, "get_music", lambda **_: [])(
                    **MusicMediaServerHelper.search_params(mediainfo),
                    **({"query": query.with_fields(MediaServerField.MUSIC)} if query is not None else {}),
                )
                match = MusicMediaServerHelper.find_match(mediainfo, matches)
                if match:
                    return _SchemaExistMediaInfo(
                        type=MediaType.MUSIC,
                        server_type=self._server_type_value,
                        server=name,
                        itemid=match.item_id,
                    )
                continue
            service = cast(Any, s)
            if mediainfo.type == MediaType.MOVIE:
                if itemid:
                    movie = service.get_iteminfo(itemid, **({"query": query} if query is not None else {}))
                    if movie:
                        logger.info(f"媒体库 {name} 中找到了 {movie}")
                        return _SchemaExistMediaInfo(
                            type=MediaType.MOVIE, server_type=self._server_type_value, server=name, itemid=movie.item_id
                        )
                movies = service.get_movies(
                    title=mediainfo.title,
                    year=mediainfo.year,
                    media_source=mediainfo.media_source,
                    media_id=mediainfo.media_id,
                    **({"query": query} if query is not None else {}),
                )
                if not movies:
                    logger.info(f"{mediainfo.title_year} 没有在媒体库 {name} 中")
                    continue
                else:
                    logger.info(f"媒体库 {name} 中找到了 {movies}")
                    return _SchemaExistMediaInfo(
                        type=MediaType.MOVIE, server_type=self._server_type_value, server=name, itemid=movies[0].item_id
                    )
            else:
                itemid, tvs = service.get_tv_episodes(
                    title=mediainfo.title,
                    year=mediainfo.year,
                    media_source=mediainfo.media_source,
                    media_id=mediainfo.media_id,
                    item_id=itemid,
                    **({"query": query} if query is not None else {}),
                )
                if not tvs:
                    logger.info(f"{mediainfo.title_year} 没有在媒体库 {name} 中")
                    continue
                else:
                    logger.info(f"{mediainfo.title_year} 在媒体库 {name} 中找到 了这些季集：{tvs}")
                    return _SchemaExistMediaInfo(
                        type=MediaType.TV, seasons=tvs, server_type=self._server_type_value, server=name, itemid=itemid
                    )
        return None

    def scheduler_job(self) -> None:
        """
        巡检已配置媒体服务器，对失活连接发起重连。

        由模块调度器每十分钟调用一次。
        """
        # 定时重连
        for name, server in self.get_instances().items():
            if self._is_inactive(server):
                logger.info(f"{self.get_name()}服务器 {name} 连接断开，尝试重连 ...")
                cast(Any, server).reconnect()

    def _is_inactive(self, server: Any) -> bool:
        """
        定时重连的失活判断钩子，子类可覆写（如增加配置完整性检查）。
        """
        return bool(server.is_inactive())

    def test(self) -> Optional[Tuple[bool, str]]:
        """
        检查已配置媒体服务器，遇到首个不可用实例即返回失败。

        :return: 是否就绪与失败说明；没有实例时返回 None
        """
        if not self.get_instances():
            return None
        for name, server in self.get_instances().items():
            error = self._test_server(server, name)
            if error:
                return False, error
        return True, ""

    def _test_server(self, server: Any, name: Optional[str]) -> Optional[str]:
        """
        连接测试钩子，返回失败信息，None 表示就绪，子类可覆写。
        """
        if server.is_inactive():
            server.reconnect()
        if not server.get_user():
            return f"无法连接{self.get_name()}服务器：{name}"
        return None
