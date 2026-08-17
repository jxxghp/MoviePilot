"""Navidrome 媒体服务器模块。"""

from typing import Any, Dict, Generator, List, Optional, Tuple, Union

from app.schemas.dashboard import Statistic as _SchemaStatistic
from app.schemas.mediaserver import ExistMediaInfo as _SchemaExistMediaInfo
from app.schemas.mediaserver import MediaServerItem as _SchemaMediaServerItem
from app.schemas.mediaserver import MediaServerLibrary as _SchemaMediaServerLibrary
from app.schemas.mediaserver import MediaServerPlayItem as _SchemaMediaServerPlayItem
from app.schemas.mediaserver import MediaServerSeasonInfo as _SchemaMediaServerSeasonInfo
from app.domain.context import MediaInfo
from app.runtime.events import eventmanager
from app.application.mediaserver import MusicMediaServerHelper
from app.runtime.log import logger
from app.modules import _MediaServerBase, _ModuleBase
from app.modules.navidrome.navidrome import Navidrome
from app.schemas.event import AuthCredentials
from app.schemas.event import AuthInterceptCredentials
from app.schemas.types import ChainEventType, MediaServerType, MediaType, ModuleType


class NavidromeModule(_ModuleBase, _MediaServerBase[Navidrome]):
    """将 Navidrome 接入 MoviePilot 媒体服务器统一接口。"""

    def init_module(self) -> None:
        """读取配置并初始化启用的 Navidrome 实例。"""
        super().init_service(
            service_name=Navidrome.__name__.lower(),
            service_type=lambda conf: Navidrome(**conf.config, sync_libraries=conf.sync_libraries),
        )

    @staticmethod
    def get_name() -> str:
        """返回模块显示名称。"""
        return "Navidrome"

    @staticmethod
    def get_type() -> ModuleType:
        """返回模块类型。"""
        return ModuleType.MediaServer

    @staticmethod
    def get_subtype() -> MediaServerType:
        """返回媒体服务器子类型。"""
        return MediaServerType.Navidrome

    @staticmethod
    def get_priority() -> int:
        """返回模块优先级。"""
        return 7

    def stop(self) -> None:
        """释放 Navidrome 模块资源。"""

    def init_setting(self) -> Optional[Tuple[str, Union[str, bool]]]:
        """Navidrome 与其它媒体服务器一致，由服务配置控制启用，无需系统开关。"""
        return None

    def scheduler_job(self) -> None:
        """定时检查 Navidrome 连接状态并尝试重连。"""
        for name, server in self.get_instances().items():
            if server.is_inactive():
                logger.info(f"Navidrome {name} 服务器连接断开，尝试重连 ...")
                server.reconnect()

    def test(self) -> Optional[Tuple[bool, str]]:
        """测试所有已启用 Navidrome 配置的连通性。"""
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
        """使用 Navidrome 用户凭据完成辅助认证。"""
        if not credentials or credentials.grant_type != "password":
            return None
        servers = (
            [(service_name, self.get_instance(service_name))]
            if service_name and self.get_instance(service_name)
            else list(self.get_instances().items())
        )
        for name, server in servers:
            if not server:
                continue
            intercept_event = eventmanager.send_event(
                etype=ChainEventType.AuthIntercept,
                data=AuthInterceptCredentials(
                    username=credentials.username,
                    channel=self.get_name(),
                    service=name,
                    status="triggered",
                ),
            )
            if intercept_event and intercept_event.event_data and intercept_event.event_data.cancel:
                continue
            token = server.authenticate(credentials.username, credentials.password)
            if token:
                credentials.channel = self.get_name()
                credentials.service = name
                credentials.token = token
                return credentials
        return None

    def media_exists(
        self, mediainfo: MediaInfo, itemid: Optional[str] = None, server: Optional[str] = None
    ) -> Optional[_SchemaExistMediaInfo]:
        """判断音乐是否已存在于 Navidrome 音乐库。"""
        if mediainfo.type != MediaType.MUSIC:
            return None
        servers = (
            [(server, self.get_instance(server))]
            if server
            else list(self.get_instances().items())
        )
        for name, service in servers:
            if not service:
                continue
            item = service.get_iteminfo(str(itemid)) if itemid else None
            if item and MusicMediaServerHelper.item_matches(mediainfo, item):
                return _SchemaExistMediaInfo(
                    type=MediaType.MUSIC,
                    server_type="navidrome",
                    server=name,
                    itemid=itemid,
                )
            matches = service.search_music(**MusicMediaServerHelper.search_params(mediainfo))
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
        """返回 Navidrome 音乐数量统计。"""
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
        self, server: str, username: Optional[str] = None, hidden: Optional[bool] = False
    ) -> Optional[List[_SchemaMediaServerLibrary]]:
        """返回 Navidrome 的虚拟音乐库。"""
        service = self.get_instance(server)
        return service.get_librarys(hidden=hidden) if service else None

    def mediaserver_items(
        self, server: str, library_id: Union[str, int], start_index: Optional[int] = 0,
        limit: Optional[int] = -1,
    ) -> Optional[Generator]:
        """获取 Navidrome 专辑条目。"""
        service = self.get_instance(server)
        return service.get_items(start_index, limit) if service else None

    def mediaserver_items_count(self, server: str, library_id: Union[str, int]) -> Optional[int]:
        """获取 Navidrome 音乐库条目数量。"""
        service = self.get_instance(server)
        return service.get_items_count(str(library_id)) if service else None

    def mediaserver_iteminfo(self, server: str, item_id: str) -> Optional[_SchemaMediaServerItem]:
        """获取 Navidrome 专辑详情。"""
        service = self.get_instance(server)
        return service.get_iteminfo(item_id) if service else None

    def mediaserver_tv_episodes(self, server: str, item_id: Union[str, int]) -> List[_SchemaMediaServerSeasonInfo]:
        """音乐服务器没有剧集信息，返回空列表。"""
        return []

    def mediaserver_playing(
        self, server: str, count: Optional[int] = 20, username: Optional[str] = None
    ) -> Optional[List[_SchemaMediaServerPlayItem]]:
        """获取 Navidrome 当前播放条目。"""
        service = self.get_instance(server)
        return service.get_resume(count) if service else None

    def mediaserver_play_url(self, server: str, item_id: Union[str, int]) -> Optional[str]:
        """生成 Navidrome 音频流地址。"""
        service = self.get_instance(server)
        return service.get_play_url(str(item_id)) if service else None

    def mediaserver_latest(
        self, server: Optional[str] = None, count: Optional[int] = 20,
        username: Optional[str] = None,
    ) -> Optional[List[_SchemaMediaServerPlayItem]]:
        """获取 Navidrome 最近新增专辑。"""
        service = self.get_instance(server)
        return service.get_latest(count) if service else None

    def mediaserver_latest_images(
        self, server: Optional[str] = None, count: Optional[int] = 10,
        username: Optional[str] = None, remote: Optional[bool] = False,
    ) -> List[str]:
        """获取最近新增专辑封面。"""
        return [item.image for item in (self.mediaserver_latest(server, count, username) or []) if item.image]
