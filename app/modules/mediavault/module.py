from typing import Any, Dict, Generator, List, Optional, Tuple, Union

from app.modules._base.mediaserver import _MediaServerModuleBase
from app.modules.mediavault.mediavault import MediaVault
from app.runtime.log import logger
from app.schemas.dashboard import Statistic as _SchemaStatistic
from app.schemas.mediaserver import MediaServerItem as _SchemaMediaServerItem
from app.schemas.mediaserver import MediaServerLibrary as _SchemaMediaServerLibrary
from app.schemas.mediaserver import MediaServerPlayItem as _SchemaMediaServerPlayItem
from app.schemas.mediaserver import MediaServerSeasonInfo as _SchemaMediaServerSeasonInfo
from app.schemas.types import MediaServerType, ModuleType


class MediaVaultModule(_MediaServerModuleBase[MediaVault]):
    """MediaVault 自建媒体库模块。"""

    # 媒体库标识（ExistMediaInfo.server_type）
    _server_type_value = "mediavault"

    def init_module(self) -> None:
        """
        初始化模块
        """
        super().init_service(
            service_name=MediaVault.__name__.lower(),
            service_type=lambda conf: MediaVault(
                **conf.config, sync_libraries=conf.sync_libraries
            ),
        )

    @staticmethod
    def get_name() -> str:
        return "MediaVault"

    @staticmethod
    def get_type() -> ModuleType:
        """
        获取模块类型
        """
        return ModuleType.MediaServer

    @staticmethod
    def get_subtype() -> MediaServerType:
        """
        获取模块子类型
        """
        return MediaServerType.MediaVault

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效
        """
        return 7

    def init_setting(self) -> Optional[Tuple[str, Union[str, bool]]]:
        """本模块不使用开关设置。"""
        return None

    def _is_inactive(self, server: MediaVault) -> bool:
        """未配置的实例不参与定时重连。"""
        return server.is_configured() and server.is_inactive()

    def stop(self) -> None:
        """停止模块"""
        for server in self.get_instances().values():
            try:
                server.disconnect()
            except Exception as err:
                logger.error(f"停止 MediaVault 模块实例失败：{err}")

    def _test_server(self, server: MediaVault, name: str) -> Optional[str]:
        """用配置完整性与 API Key 探测结果判断连接状态。"""
        if not server.is_configured():
            return f"{self.get_name()}配置不完整：{name}"
        if server.is_inactive() and not server.reconnect():
            return f"无法连接{self.get_name()}：{name}"
        return None

    def media_statistic(
        self, server: Optional[str] = None
    ) -> Optional[List[_SchemaStatistic]]:
        """
        媒体数量统计
        """
        if server:
            server_obj: Optional[MediaVault] = self.get_instance(server)
            servers = [server_obj] if server_obj else []
        else:
            servers = list(self.get_instances().values())
        statistics = []
        for s in servers:
            statistic = s.get_medias_count()
            if not statistic:
                continue
            statistic.user_count = s.get_user_count()
            statistics.append(statistic)
        return statistics

    def mediaserver_librarys(
        self, server: Optional[str] = None, hidden: Optional[bool] = False, **kwargs: Any
    ) -> Optional[List[_SchemaMediaServerLibrary]]:
        """
        媒体库列表
        """
        server_obj: Optional[MediaVault] = self.get_instance(server)
        if server_obj:
            return server_obj.get_librarys(hidden=hidden)
        return None

    def mediaserver_items(
        self,
        server: str,
        library_id: Union[str, int],
        start_index: Optional[int] = 0,
        limit: Optional[int] = -1,
    ) -> Optional[Generator[Optional[_SchemaMediaServerItem], Any, None]]:
        """
        获取媒体服务器项目列表，支持分页和不分页逻辑，默认不分页获取所有数据

        :param server: 媒体服务器名称
        :param library_id: 媒体库ID
        :param start_index: 起始索引
        :param limit: 每次请求的最大项目数，None 或 -1 表示一次性获取所有数据
        """
        server_obj: Optional[MediaVault] = self.get_instance(server)
        if server_obj:
            return server_obj.get_items(library_id, start_index, limit)
        return None

    def mediaserver_items_count(
        self, server: str, library_id: Union[str, int]
    ) -> Optional[int]:
        """
        获取指定媒体库可同步的媒体条目总数
        """
        server_obj: Optional[MediaVault] = self.get_instance(server)
        if server_obj:
            return server_obj.get_items_count(library_id)
        return None

    def mediaserver_iteminfo(
        self, server: str, item_id: str
    ) -> Optional[_SchemaMediaServerItem]:
        """
        媒体库项目详情
        """
        server_obj: Optional[MediaVault] = self.get_instance(server)
        if server_obj:
            return server_obj.get_iteminfo(str(item_id))
        return None

    def mediaserver_tv_episodes(
        self, server: str, item_id: Union[str, int]
    ) -> Optional[List[_SchemaMediaServerSeasonInfo]]:
        """
        获取剧集信息
        """
        server_obj: Optional[MediaVault] = self.get_instance(server)
        if not server_obj:
            return None
        _, seasoninfo = server_obj.get_tv_episodes(item_id=str(item_id))
        if not seasoninfo:
            return []
        return [
            _SchemaMediaServerSeasonInfo(season=season, episodes=episodes)
            for season, episodes in seasoninfo.items()
        ]

    def mediaserver_season_episode_ids(
        self, server: str, item_id: Union[str, int], season: int
    ) -> Optional[Dict[int, str]]:
        """
        获取指定季的集号到条目 ID 映射
        """
        server_obj: Optional[MediaVault] = self.get_instance(server)
        if not server_obj:
            return None
        return server_obj.get_season_episode_ids(str(item_id), season)

    def mediaserver_playing(
        self, server: str, count: Optional[int] = 20, **kwargs: Any
    ) -> Optional[List[_SchemaMediaServerPlayItem]]:
        """
        获取媒体服务器正在播放信息
        """
        server_obj: Optional[MediaVault] = self.get_instance(server)
        if not server_obj:
            return None
        return server_obj.get_resume(num=count)

    def mediaserver_play_url(
        self, server: str, item_id: Union[str, int]
    ) -> Optional[str]:
        """
        获取媒体库播放地址
        """
        server_obj: Optional[MediaVault] = self.get_instance(server)
        if not server_obj:
            return None
        return server_obj.get_play_url(str(item_id))

    def mediaserver_latest(
        self, server: Optional[str] = None, count: Optional[int] = 20, **kwargs: Any
    ) -> Optional[List[_SchemaMediaServerPlayItem]]:
        """
        获取媒体服务器最新入库条目
        """
        server_obj: Optional[MediaVault] = self.get_instance(server)
        if not server_obj:
            return None
        return server_obj.get_latest(num=count)

    def mediaserver_latest_images(
        self,
        server: Optional[str] = None,
        count: Optional[int] = 20,
        remote: Optional[bool] = False,
        **kwargs: Any,
    ) -> List[str]:
        """
        获取媒体服务器最新入库条目的图片

        :param server: 媒体服务器名称
        :param count: 获取数量
        :param remote: True为外网链接，False为内网链接
        """
        server_obj: Optional[MediaVault] = self.get_instance(server)
        if not server_obj:
            return []
        return server_obj.get_latest_backdrops(num=count, remote=remote) or []
