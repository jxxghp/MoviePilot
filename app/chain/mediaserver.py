import threading
from typing import List, Union, Optional, Generator, Any

from app.chain import ChainBase
from app.core.cache import cached
from app.core.config import global_vars
from app.db.mediaserver_oper import MediaServerOper
from app.helper.service import ServiceConfigHelper
from app.log import logger
from app.schemas import MediaServerLibrary, MediaServerItem, MediaServerSeasonInfo, MediaServerPlayItem

lock = threading.Lock()


class MediaServerChain(ChainBase):
    """
    媒体服务器处理链
    """

    def __init__(self):
        super().__init__()
        self.dboper = MediaServerOper()

    def librarys(self, server: str, username: str = None, hidden: bool = False) -> List[MediaServerLibrary]:
        """
        获取媒体服务器所有媒体库
        """
        return self.run_module("mediaserver_librarys", server=server, username=username, hidden=hidden)

    def items(self, server: str, library_id: Union[str, int],
              start_index: int = 0, limit: Optional[int] = -1) -> Generator[Any, None, None]:
        """
        获取媒体服务器项目列表，支持分页和不分页逻辑，默认不分页获取所有数据

        :param server: 媒体服务器名称
        :param library_id: 媒体库ID，用于标识要获取的媒体库
        :param start_index: 起始索引，用于分页获取数据。默认为 0，即从第一个项目开始获取
        :param limit: 每次请求的最大项目数，用于分页。如果为 None 或 -1，则表示一次性获取所有数据，默认为 -1

        :return: 返回一个生成器对象，用于逐步获取媒体服务器中的项目

        说明：
        - 特别注意的是，这里使用yield from返回迭代器，避免同时使用return与yield导致Python生成器解析异常
        - 如果 `limit` 为 None 或 -1 时，表示一次性获取所有数据，分页处理将不再生效
        - 在这种情况下，内存消耗可能会较大，特别是在数据量非常大的场景下
        - 如果未来评估结果显示，不分页场景下的内存消耗远大于分页处理时的网络请求开销，可以考虑在此方法中实现自分页的处理
        - 即通过 `while` 循环在上层进行分页控制，逐步获取所有数据，避免内存爆炸，当前该逻辑由具体实例来实现不分页的处理
        - Plex 实际上已默认支持内部分页处理，Jellyfin 与 Emby 获取数据时存在内部过滤场景，如排除合集等，分页数据可能是错误的
        if limit is not None and limit != -1:
            yield from self.run_module("mediaserver_items", server=server, library_id=library_id,
                                   start_index=start_index, limit=limit)
        else:
            # 自分页逻辑，通过循环逐步获取所有数据
            page_size = 10
            while True:
                data_generator = self.run_module("mediaserver_items", server=server, library_id=library_id,
                                                 start_index=start_index, limit=page_size)
                if not data_generator:
                    break
                count = 0
                for item in data_generator:
                    if item:
                        count += 1
                        yield item
                if count < page_size:
                    break
                start_index += page_size
        """
        yield from self.run_module("mediaserver_items", server=server, library_id=library_id,
                                   start_index=start_index, limit=limit)

    def iteminfo(self, server: str, item_id: Union[str, int]) -> MediaServerItem:
        """
        获取媒体服务器项目信息
        """
        return self.run_module("mediaserver_iteminfo", server=server, item_id=item_id)

    def episodes(self, server: str, item_id: Union[str, int]) -> List[MediaServerSeasonInfo]:
        """
        获取媒体服务器剧集信息
        """
        return self.run_module("mediaserver_tv_episodes", server=server, item_id=item_id)

    def playing(self, server: str, count: int = 20, username: str = None) -> List[MediaServerPlayItem]:
        """
        获取媒体服务器正在播放信息
        """
        return self.run_module("mediaserver_playing", count=count, server=server, username=username)

    def latest(self, server: str, count: int = 20, username: str = None) -> List[MediaServerPlayItem]:
        """
        获取媒体服务器最新入库条目
        """
        return self.run_module("mediaserver_latest", count=count, server=server, username=username)

    @cached(maxsize=1, ttl=3600)
    def get_latest_wallpapers(self, server: str = None, count: int = 10,
                              remote: bool = True, username: str = None) -> List[str]:
        """
        获取最新最新入库条目海报作为壁纸，缓存1小时
        """
        return self.run_module("mediaserver_latest_images", server=server, count=count,
                               remote=remote, username=username)

    def get_latest_wallpaper(self, server: str = None, remote: bool = True, username: str = None) -> Optional[str]:
        """
        获取最新最新入库条目海报作为壁纸，缓存1小时
        """
        wallpapers = self.get_latest_wallpapers(server=server, count=1, remote=remote, username=username)
        return wallpapers[0] if wallpapers else None

    def get_play_url(self, server: str, item_id: Union[str, int]) -> Optional[str]:
        """
        获取播放地址
        """
        return self.run_module("mediaserver_play_url", server=server, item_id=item_id)

    def sync(self):
        """
        同步媒体库所有数据到本地数据库
        """
        # 设置的媒体服务器
        mediaservers = ServiceConfigHelper.get_mediaserver_configs()
        if not mediaservers:
            return
        with lock:
            # 汇总统计
            total_count = 0
            # 清空登记薄
            self.dboper.empty()
            # 遍历媒体服务器
            for mediaserver in mediaservers:
                if not mediaserver:
                    continue
                logger.info(f"正在准备同步媒体服务器 {mediaserver.name} 的数据")
                if not mediaserver.enabled:
                    logger.info(f"媒体服务器 {mediaserver.name} 未启用，跳过")
                    continue
                server_name = mediaserver.name
                sync_libraries = mediaserver.sync_libraries or []
                logger.info(f"开始同步媒体服务器 {server_name} 的数据 ...")
                libraries = self.librarys(server_name)
                if not libraries:
                    logger.info(f"没有获取到媒体服务器 {server_name} 的媒体库，跳过")
                    continue
                for library in libraries:
                    if sync_libraries \
                            and "all" not in sync_libraries \
                            and str(library.id) not in sync_libraries:
                        logger.info(f"{library.name} 未在 {server_name} 同步媒体库列表中，跳过")
                        continue
                    logger.info(f"正在同步 {server_name} 媒体库 {library.name} ...")
                    library_count = 0
                    for item in self.items(server=server_name, library_id=library.id):
                        if global_vars.is_system_stopped:
                            return
                        if not item or not item.item_id:
                            continue
                        logger.debug(f"正在同步 {item.title} ...")
                        # 计数
                        library_count += 1
                        seasoninfo = {}
                        # 类型
                        item_type = "电视剧" if item.item_type in ["Series", "show"] else "电影"
                        if item_type == "电视剧":
                            # 查询剧集信息
                            espisodes_info = self.episodes(server_name, item.item_id) or []
                            for episode in espisodes_info:
                                seasoninfo[episode.season] = episode.episodes
                        # 插入数据
                        item_dict = item.dict()
                        item_dict["seasoninfo"] = seasoninfo
                        item_dict["item_type"] = item_type
                        self.dboper.add(**item_dict)
                    logger.info(f"{server_name} 媒体库 {library.name} 同步完成，共同步数量：{library_count}")
                    # 总数累加
                    total_count += library_count
                logger.info(f"媒体服务器 {server_name} 数据同步完成，总同步数量：{total_count}")
