"""媒体服务器模块业务样板基类。

沉淀各媒体服务器模块逐字复制的样板：用户辅助认证、媒体存在性检查、
定时重连与连接测试。服务器差异（认证 API、存在性检查端点、连接探测方式）
通过类属性与钩子方法保留在各模块。
"""
from typing import Optional, Tuple

from app.schemas.event import AuthCredentials as _SchemaAuthCredentials
from app.schemas.event import AuthInterceptCredentials as _SchemaAuthInterceptCredentials
from app.schemas.mediaserver import ExistMediaInfo as _SchemaExistMediaInfo
from app.application.mediaserver import MusicMediaServerHelper
from app.domain.context import MediaInfo
from app.modules import _MediaServerBase, _ModuleBase, TService
from app.runtime.events import eventmanager
from app.runtime.log import logger
from app.schemas.types import ChainEventType, MediaType


class _MediaServerModuleBase(_ModuleBase, _MediaServerBase[TService]):
    """
    媒体服务器模块业务样板基类。
    """

    # 媒体库标识（用于 ExistMediaInfo.server_type，如 "emby"），子类覆写
    _server_type_value: str = ""

    def user_authenticate(
            self,
            credentials: _SchemaAuthCredentials,
            service_name: Optional[str] = None,
    ) -> Optional[_SchemaAuthCredentials]:
        """
        使用媒体服务器用户辅助完成用户认证

        :param credentials: 认证数据
        :param service_name: 指定要认证的媒体服务器名称，若为 None 则认证所有服务器
        :return: 认证数据
        """
        if not credentials or credentials.grant_type != "password":
            return None
        # 确定要认证的服务器列表
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
            # 触发认证拦截事件
            intercept_event = eventmanager.send_event(
                etype=ChainEventType.AuthIntercept,
                data=_SchemaAuthInterceptCredentials(
                    username=credentials.username,
                    channel=self.get_name(),
                    service=name,
                    status="triggered",
                ),
            )
            if intercept_event and intercept_event.event_data:
                intercept_data: _SchemaAuthInterceptCredentials = intercept_event.event_data
                if intercept_data.cancel:
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
    ) -> Optional[_SchemaExistMediaInfo]:
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
        for name, s in servers:
            if not s:
                continue
            if mediainfo.type == MediaType.MUSIC:
                # 部分服务器未实现音乐查询，退化为空列表
                matches = getattr(s, "get_music", lambda **_: [])(
                    **MusicMediaServerHelper.search_params(mediainfo)
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
            if mediainfo.type == MediaType.MOVIE:
                if itemid:
                    movie = s.get_iteminfo(itemid)
                    if movie:
                        logger.info(f"媒体库 {name} 中找到了 {movie}")
                        return _SchemaExistMediaInfo(
                            type=MediaType.MOVIE,
                            server_type=self._server_type_value,
                            server=name,
                            itemid=movie.item_id
                        )
                movies = s.get_movies(title=mediainfo.title,
                                      year=mediainfo.year,
                                      media_source=mediainfo.media_source,
                                      media_id=mediainfo.media_id)
                if not movies:
                    logger.info(f"{mediainfo.title_year} 没有在媒体库 {name} 中")
                    continue
                else:
                    logger.info(f"媒体库 {name} 中找到了 {movies}")
                    return _SchemaExistMediaInfo(
                        type=MediaType.MOVIE,
                        server_type=self._server_type_value,
                        server=name,
                        itemid=movies[0].item_id
                    )
            else:
                itemid, tvs = s.get_tv_episodes(title=mediainfo.title,
                                                year=mediainfo.year,
                                                media_source=mediainfo.media_source,
                                                media_id=mediainfo.media_id,
                                                item_id=itemid)
                if not tvs:
                    logger.info(f"{mediainfo.title_year} 没有在媒体库 {name} 中")
                    continue
                else:
                    logger.info(f"{mediainfo.title_year} 在媒体库 {name} 中找到 了这些季集：{tvs}")
                    return _SchemaExistMediaInfo(
                        type=MediaType.TV,
                        seasons=tvs,
                        server_type=self._server_type_value,
                        server=name,
                        itemid=itemid
                    )
        return None

    def scheduler_job(self) -> None:
        """
        定时任务，每10分钟调用一次
        """
        # 定时重连
        for name, server in self.get_instances().items():
            if self._is_inactive(server):
                logger.info(f"{self.get_name()}服务器 {name} 连接断开，尝试重连 ...")
                server.reconnect()

    def _is_inactive(self, server) -> bool:
        """
        定时重连的失活判断钩子，子类可覆写（如增加配置完整性检查）。
        """
        return server.is_inactive()

    def test(self) -> Optional[Tuple[bool, str]]:
        """
        测试模块连接性
        """
        if not self.get_instances():
            return None
        for name, server in self.get_instances().items():
            error = self._test_server(server, name)
            if error:
                return False, error
        return True, ""

    def _test_server(self, server, name: str) -> Optional[str]:
        """
        连接测试钩子，返回失败信息，None 表示就绪，子类可覆写。
        """
        if server.is_inactive():
            server.reconnect()
        if not server.get_user():
            return f"无法连接{self.get_name()}服务器：{name}"
        return None
