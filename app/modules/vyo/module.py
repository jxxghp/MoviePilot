"""
Vyo 模块，沿用 mediavault 配置身份提供媒体服务器统一接口。
"""

from typing import Any, Dict, Generator, List, Optional, Tuple, Union

from app.modules._base.mediaserver import _MediaServerModuleBase
from app.modules.vyo.vyo import Vyo
from app.runtime.log import logger
from app.schemas.dashboard import Statistic as _SchemaStatistic
from app.schemas.mediaserver import MediaServerItem as _SchemaMediaServerItem
from app.schemas.mediaserver import MediaServerLibrary as _SchemaMediaServerLibrary
from app.schemas.mediaserver import MediaServerPlayItem as _SchemaMediaServerPlayItem
from app.schemas.mediaserver import MediaServerSeasonInfo as _SchemaMediaServerSeasonInfo
from app.schemas.types import MediaServerType, ModuleType


class VyoModule(_MediaServerModuleBase[Vyo]):
    """
    Vyo 媒体服务模块。
    """

    # 媒体库标识（ExistMediaInfo.server_type）
    # 配置类型值沿用 mediavault，兼容已保存的媒体服务器配置
    _server_type_value = "mediavault"

    def init_module(self) -> None:
        """
        初始化模块。
        """
        # 展示名更改为 Vyo，持久化配置类型仍为 mediavault，不能从类名推导。
        super().init_service(
            service_name=self._server_type_value,
            service_type=lambda conf: Vyo(
                **conf.config, sync_libraries=conf.sync_libraries
            ),
        )

    @staticmethod
    def get_name() -> str:
        """
        返回媒体服务器模块名称。
        """
        return "Vyo"

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
        return MediaServerType.Vyo

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效。
        """
        return 7

    def init_setting(self) -> Optional[Tuple[str, Union[str, bool]]]:
        """
        本模块不使用开关设置。
        """
        return None

    def _is_inactive(self, server: Vyo) -> bool:
        """
        未配置的实例不参与定时重连。
        """
        return server.is_configured() and server.is_inactive()

    def stop(self) -> None:
        """
        逐个断开 Vyo 实例，单个失败不阻止其他实例关闭。
        """
        for server in self.get_instances().values():
            try:
                server.disconnect()
            except Exception as err:
                logger.error(f"停止 Vyo 模块实例失败：{err}")

    def _test_server(self, server: Vyo, name: Optional[str]) -> Optional[str]:
        """
        用配置完整性与 API Key 探测结果判断连接状态。
        """
        if not server.is_configured():
            return f"{self.get_name()}配置不完整：{name}"
        if server.is_inactive() and not server.reconnect():
            return f"无法连接{self.get_name()}：{name}"
        return None

    def media_statistic(
        self, server: Optional[str] = None
    ) -> Optional[List[_SchemaStatistic]]:
        """
        汇总指定实例或全部 Vyo 实例的媒体与用户数量。

        :param server: 指定实例名称，省略时汇总全部实例
        :return: 统计列表；无实例或全部统计请求失败时返回空列表
        """
        if server:
            server_obj: Optional[Vyo] = self.get_instance(server)
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
        读取实例媒体库列表。

        :param server: 媒体服务器配置名称，空值使用默认实例
        :param hidden: 是否隐藏同步范围外的库
        :param kwargs: 兼容统一媒体服务器接口的额外参数，当前不使用
        :return: 统一媒体库列表；实例不存在或查询失败时返回 None
        """
        server_obj: Optional[Vyo] = self.get_instance(server)
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
        返回电影和电视剧同步条目的迭代器。

        :param server: 媒体服务器配置名称，空值使用默认实例
        :param library_id: 媒体库标识
        :param start_index: 零基条目偏移，负值按 0 处理
        :param limit: 最多检查的条目数量，None 或 -1 表示不限
        :return: 统一条目生成器；实例不存在时返回 None，底层请求失败可能提前结束迭代
        """
        server_obj: Optional[Vyo] = self.get_instance(server)
        if server_obj:
            return server_obj.get_items(library_id, start_index, limit)
        return None

    def mediaserver_items_count(
        self, server: str, library_id: Union[str, int]
    ) -> Optional[int]:
        """
        查询媒体库中的电影和电视剧总数。

        :param server: 媒体服务器配置名称，空值使用默认实例
        :param library_id: 媒体库标识
        :return: 条目总数；实例不存在或查询失败时返回 None
        """
        server_obj: Optional[Vyo] = self.get_instance(server)
        if server_obj:
            return server_obj.get_items_count(library_id)
        return None

    def mediaserver_iteminfo(
        self, server: str, item_id: str
    ) -> Optional[_SchemaMediaServerItem]:
        """
        查询单个媒体条目详情。

        :param server: 媒体服务器配置名称，空值使用默认实例
        :param item_id: Vyo 条目标识
        :return: 统一媒体模型；实例不存在或查询失败时返回 None
        """
        server_obj: Optional[Vyo] = self.get_instance(server)
        if server_obj:
            return server_obj.get_iteminfo(str(item_id))
        return None

    def mediaserver_tv_episodes(
        self, server: str, item_id: Union[str, int]
    ) -> Optional[List[_SchemaMediaServerSeasonInfo]]:
        """
        查询电视剧已入库的季集编号。

        :param server: 媒体服务器配置名称，空值使用默认实例
        :param item_id: Vyo 条目标识
        :return: 季集列表；实例不存在时返回 None，无可用数据时返回空列表
        """
        server_obj: Optional[Vyo] = self.get_instance(server)
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
        查询指定季中集号对应的条目标识。

        :param server: 媒体服务器配置名称，空值使用默认实例
        :param item_id: Vyo 条目标识
        :param season: 目标季号
        :return: 集号到条目标识的映射；实例不存在时返回 None，客户端中途失败可能返回部分映射
        """
        server_obj: Optional[Vyo] = self.get_instance(server)
        if not server_obj:
            return None
        return server_obj.get_season_episode_ids(str(item_id), season)

    def mediaserver_playing(
        self, server: str, count: Optional[int] = 20, **kwargs: Any
    ) -> Optional[List[_SchemaMediaServerPlayItem]]:
        """
        查询当前账号的继续观看列表。

        :param server: 媒体服务器配置名称，空值使用默认实例
        :param count: 目标数量，最终限制为 1 至 100，空值或 0 使用 20
        :param kwargs: 兼容统一媒体服务器接口的额外参数，当前不使用
        :return: 播放条目列表；实例不存在或查询失败时返回 None
        """
        server_obj: Optional[Vyo] = self.get_instance(server)
        if not server_obj:
            return None
        return server_obj.get_resume(num=count)

    def mediaserver_play_url(
        self, server: str, item_id: Union[str, int]
    ) -> Optional[str]:
        """
        构造媒体条目的网页播放链接。

        :param server: 媒体服务器配置名称，空值使用默认实例
        :param item_id: Vyo 条目标识
        :return: 网页 URL；实例或必要配置不可用时返回 None
        """
        server_obj: Optional[Vyo] = self.get_instance(server)
        if not server_obj:
            return None
        return server_obj.get_play_url(str(item_id))

    def mediaserver_latest(
        self, server: Optional[str] = None, count: Optional[int] = 20, **kwargs: Any
    ) -> Optional[List[_SchemaMediaServerPlayItem]]:
        """
        查询最近入库的电影和电视剧。

        :param server: 媒体服务器配置名称，空值使用默认实例
        :param count: 目标数量，最终限制为 1 至 100，空值或 0 使用 20
        :param kwargs: 兼容统一媒体服务器接口的额外参数，当前不使用
        :return: 最新媒体列表；实例不存在或查询失败时返回 None
        """
        server_obj: Optional[Vyo] = self.get_instance(server)
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
        获取最新电影和电视剧的公开背景图。

        :param server: 媒体服务器配置名称，空值使用默认实例
        :param count: 目标图片数量，最终限制为 1 至 100
        :param remote: 是否优先使用外网图片根地址
        :param kwargs: 兼容统一媒体服务器接口的额外参数，当前不使用
        :return: 无管理密钥的图片 URL 列表；实例不可用或没有图片时返回空列表
        """
        server_obj: Optional[Vyo] = self.get_instance(server)
        if not server_obj:
            return []
        return server_obj.get_latest_backdrops(num=count, remote=remote) or []
