import threading
from datetime import datetime
from typing import Callable, Dict, List, Union, Optional, Generator, Any

from app.chain import ChainBase
from app.core.config import global_vars
from app.db.mediaserver_oper import MediaServerOper
from app.helper.service import ServiceConfigHelper
from app.log import logger
from app.schemas import MediaServerLibrary, MediaServerItem, MediaServerSeasonInfo, MediaServerPlayItem
from app.utils.security import SecurityUtils

lock = threading.Lock()


class MediaServerChain(ChainBase):
    """
    媒体服务器处理链
    """

    @staticmethod
    def _sign_image_url(url: Optional[str]) -> Optional[str]:
        """
        为返回前端的媒体服务器图片 URL 添加代理签名。
        """
        return SecurityUtils.sign_url(url) if url else url

    def _sign_library_images(
        self, libraries: Optional[List[MediaServerLibrary]]
    ) -> List[MediaServerLibrary]:
        """
        给媒体库列表中的封面和封面组添加代理签名。
        """
        for library in libraries or []:
            if library.image:
                library.image = self._sign_image_url(library.image)
            if library.image_list:
                library.image_list = [
                    self._sign_image_url(image)
                    for image in library.image_list
                    if image
                ]
        return libraries or []

    def _sign_play_item_images(
        self, items: Optional[List[MediaServerPlayItem]]
    ) -> List[MediaServerPlayItem]:
        """
        给媒体服务器播放条目中的图片 URL 添加代理签名。
        """
        for item in items or []:
            if item.image:
                item.image = self._sign_image_url(item.image)
        return items or []

    def librarys(self, server: str, username: Optional[str] = None,
                 hidden: bool = False) -> List[MediaServerLibrary]:
        """
        获取媒体服务器所有媒体库
        """
        return self._sign_library_images(
            self.run_module(
                "mediaserver_librarys",
                server=server,
                username=username,
                hidden=hidden,
            )
        )

    def items(self, server: str, library_id: Union[str, int],
              start_index: Optional[int] = 0, limit: Optional[int] = -1) -> Generator[Any, None, None]:
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

    def items_count(self, server: str, library_id: Union[str, int]) -> Optional[int]:
        """
        获取指定媒体库可同步的媒体条目总数

        :param server: 媒体服务器名称
        :param library_id: 媒体库ID
        :return: 媒体条目总数，无法获取时返回None
        """
        return self.run_module(
            "mediaserver_items_count",
            server=server,
            library_id=library_id,
        )

    def media_count(self, server: str) -> Optional[int]:
        """
        获取指定媒体服务器可同步的电影和电视剧总数

        :param server: 媒体服务器名称
        :return: 电影和电视剧总数，无法获取时返回None
        """
        statistics = self.run_module("media_statistic", server=server)
        if not statistics:
            return None
        return sum(
            (statistic.movie_count or 0) + (statistic.tv_count or 0)
            for statistic in statistics
        )

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

    def playing(self, server: str, count: Optional[int] = 20,
                username: Optional[str] = None) -> List[MediaServerPlayItem]:
        """
        获取媒体服务器正在播放信息
        """
        return self._sign_play_item_images(
            self.run_module(
                "mediaserver_playing",
                count=count,
                server=server,
                username=username,
            )
        )

    def latest(self, server: str, count: Optional[int] = 20,
               username: Optional[str] = None) -> List[MediaServerPlayItem]:
        """
        获取媒体服务器最新入库条目
        """
        return self._sign_play_item_images(
            self.run_module(
                "mediaserver_latest",
                count=count,
                server=server,
                username=username,
            )
        )

    def get_latest_wallpapers(self, server: Optional[str] = None, count: Optional[int] = 10,
                              remote: bool = True, username: Optional[str] = None) -> List[str]:
        """
        获取最新最新入库条目海报作为壁纸，缓存1小时
        """
        wallpapers = self.run_module(
            "mediaserver_latest_images",
            server=server,
            count=count,
            remote=remote,
            username=username,
        )
        return [
            self._sign_image_url(wallpaper)
            for wallpaper in wallpapers or []
            if wallpaper
        ]

    def get_latest_wallpaper(self, server: Optional[str] = None,
                             remote: bool = True, username: Optional[str] = None) -> Optional[str]:
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

    def get_season_episode_ids(self, server: str, item_id: Union[str, int],
                               season: int) -> Dict[int, str]:
        """
        获取指定季的集号到媒体服务器条目 ID 映射

        :param server: 媒体服务器名称
        :param item_id: 剧集在媒体服务器中的条目 ID
        :param season: 季号
        :return: 集号到条目 ID 的映射，无数据时返回空字典
        """
        result = self.run_module(
            "mediaserver_season_episode_ids",
            server=server,
            item_id=item_id,
            season=season,
        )
        return result or {}

    def get_image_cookies(
        self, server: Optional[str], image_url: str
    ) -> Optional[str | dict]:
        """
        获取图片的Cookies
        """
        return self.run_module(
            "mediaserver_image_cookies", server=server, image_url=image_url
        )

    def sync(self, progress_callback: Optional[Callable[..., None]] = None) -> None:
        """
        同步媒体库所有数据到本地数据库

        :param progress_callback: 定时服务进度更新回调
        """
        # 设置的媒体服务器
        mediaservers = ServiceConfigHelper.get_mediaserver_configs()
        if not mediaservers:
            if progress_callback:
                progress_callback(value=100, text="未配置媒体服务器，跳过同步")
            return
        with lock:
            # 汇总统计
            total_count = 0
            dboper = MediaServerOper()
            enabled_servers = [mediaserver.name for mediaserver in mediaservers
                               if mediaserver and mediaserver.enabled and mediaserver.name]
            dboper.delete_excluded_servers(enabled_servers)
            total_servers = len(enabled_servers)
            if progress_callback:
                progress_callback(
                    value=0,
                    text=f"开始同步媒体服务器，共 {total_servers} 个 ...",
                    data={"total": total_servers, "finished": 0},
                )
            if not total_servers:
                if progress_callback:
                    progress_callback(value=100, text="没有已启用的媒体服务器")
                return

            server_sync_contexts = {}
            global_media_total = 0
            global_counts_available = True
            for mediaserver in mediaservers:
                if not mediaserver or not mediaserver.enabled:
                    continue
                server_name = mediaserver.name
                logger.info(f"正在统计媒体服务器 {server_name} 的待同步媒体数量")
                libraries = self.librarys(server_name)
                if not libraries:
                    server_sync_contexts[server_name] = None
                    continue

                sync_libraries = mediaserver.sync_libraries or []
                selected_libraries = []
                for library in libraries:
                    if sync_libraries \
                            and "all" not in sync_libraries \
                            and str(library.id) not in sync_libraries:
                        logger.info(f"{library.name} 未在 {server_name} 同步媒体库列表中，跳过")
                        continue
                    selected_libraries.append(library)

                library_media_counts = {
                    str(library.id): None for library in selected_libraries
                }
                sync_all_libraries = (
                    not sync_libraries or "all" in sync_libraries
                )
                server_media_count = (
                    self.media_count(server_name)
                    if sync_all_libraries else None
                )
                if server_media_count:
                    global_media_total += server_media_count
                    logger.info(
                        f"媒体服务器 {server_name} 共 {server_media_count} 个媒体待同步"
                    )
                else:
                    for library in selected_libraries:
                        media_count = self.items_count(
                            server=server_name,
                            library_id=library.id,
                        )
                        library_media_counts[str(library.id)] = media_count
                        if media_count is None:
                            global_counts_available = False
                            logger.warning(
                                f"未获取到 {server_name} 媒体库 {library.name} 的媒体总数，"
                                f"同步进度将按媒体库完成度计算"
                            )
                        else:
                            global_media_total += media_count
                            logger.info(
                                f"{server_name} 媒体库 {library.name}"
                                f"共 {media_count} 个媒体待同步"
                            )
                server_sync_contexts[server_name] = (
                    selected_libraries,
                    library_media_counts,
                )

            if not global_counts_available:
                global_media_total = None

            # 遍历媒体服务器
            server_index = 0
            global_media_finished = 0
            for mediaserver in mediaservers:
                if not mediaserver:
                    continue
                logger.info(f"正在准备同步媒体服务器 {mediaserver.name} 的数据")
                if not mediaserver.enabled:
                    logger.info(f"媒体服务器 {mediaserver.name} 未启用，跳过")
                    continue
                server_index += 1
                server_name = mediaserver.name
                if progress_callback:
                    progress_value = (
                        global_media_finished / global_media_total * 100
                        if global_media_total else
                        (server_index - 1) / total_servers * 100
                    )
                    progress_callback(
                        value=progress_value,
                        text=(
                            f"正在同步媒体服务器"
                            f"（{server_index}/{total_servers}）{server_name} ..."
                        ),
                        data={
                            "total": total_servers,
                            "finished": server_index - 1,
                            "current": server_name,
                            "media_total": global_media_total,
                            "media_finished": global_media_finished,
                        },
                    )
                logger.info(f"开始同步媒体服务器 {server_name} 的数据 ...")
                sync_context = server_sync_contexts.get(server_name)
                if sync_context is None:
                    logger.info(f"没有获取到媒体服务器 {server_name} 的媒体库，跳过")
                    if progress_callback:
                        progress_value = (
                            global_media_finished / global_media_total * 100
                            if global_media_total else
                            server_index / total_servers * 100
                        )
                        progress_callback(
                            value=progress_value,
                            text=f"媒体服务器 {server_name} 无可同步媒体库",
                            data={
                                "total": total_servers,
                                "finished": server_index,
                                "media_total": global_media_total,
                                "media_finished": global_media_finished,
                            },
                        )
                    continue
                sync_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
                selected_libraries, library_media_counts = sync_context
                total_libraries = len(selected_libraries)
                for library_index, library in enumerate(selected_libraries, start=1):
                    logger.info(f"正在同步 {server_name} 媒体库 {library.name} ...")
                    library_media_total = library_media_counts.get(str(library.id))
                    library_count = 0
                    for item in self.items(server=server_name, library_id=library.id):
                        if global_vars.is_system_stopped:
                            return
                        if not item or not item.item_id:
                            continue
                        logger.debug(f"正在同步 {item.title} ...")
                        # 计数
                        library_count += 1
                        global_media_finished += 1
                        seasoninfo = {}
                        # 类型
                        item_type = "电视剧" if item.item_type in ["Series", "show"] else "电影"
                        if item_type == "电视剧":
                            # 查询剧集信息
                            espisodes_info = self.episodes(server_name, item.item_id) or []
                            for episode in espisodes_info:
                                seasoninfo[episode.season] = episode.episodes
                        # 插入数据
                        item_dict = item.model_dump()
                        item_dict["seasoninfo"] = seasoninfo
                        item_dict["item_type"] = item_type
                        item_dict["lst_mod_date"] = sync_time
                        dboper.upsert(**item_dict)
                        if progress_callback:
                            if global_media_total:
                                progress_value = min(
                                    global_media_finished / global_media_total,
                                    1,
                                ) * 100
                            else:
                                library_progress = (
                                    min(library_count / library_media_total, 1)
                                    if library_media_total else 0
                                )
                                server_progress = (
                                    library_index - 1 + library_progress
                                ) / total_libraries
                                progress_value = (
                                    server_index - 1 + server_progress
                                ) / total_servers * 100
                            progress_callback(
                                value=progress_value,
                                text=(
                                    f"正在同步 {server_name} 媒体库 {library.name}"
                                    f"（{library_count}/{library_media_total}）"
                                    if library_media_total is not None
                                    else f"正在同步 {server_name} 媒体库 {library.name}"
                                ),
                                data={
                                    "total": total_servers,
                                    "finished": server_index - 1,
                                    "current": server_name,
                                    "library_total": total_libraries,
                                    "library_finished": library_index - 1,
                                    "current_library": library.name,
                                    "library_media_total": library_media_total,
                                    "library_media_finished": library_count,
                                    "media_total": global_media_total,
                                    "media_finished": global_media_finished,
                                },
                            )
                    logger.info(f"{server_name} 媒体库 {library.name} 同步完成，共同步数量：{library_count}")
                    # 总数累加
                    total_count += library_count
                    if progress_callback:
                        if global_media_total:
                            progress_value = min(
                                global_media_finished / global_media_total,
                                1,
                            ) * 100
                        else:
                            server_progress = library_index / total_libraries
                            progress_value = (
                                server_index - 1 + server_progress
                            ) / total_servers * 100
                        progress_callback(
                            value=progress_value,
                            text=(
                                f"{server_name} 媒体库"
                                f"（{library_index}/{total_libraries}）{library.name} 同步完成"
                            ),
                            data={
                                "total": total_servers,
                                "finished": server_index - 1,
                                "current": server_name,
                                "library_total": total_libraries,
                                "library_finished": library_index,
                                "current_library": library.name,
                                "library_media_total": library_media_total,
                                "library_media_finished": library_count,
                                "media_total": global_media_total,
                                "media_finished": global_media_finished,
                            },
                        )
                stale_count = dboper.delete_stale(server=server_name, sync_time=sync_time)
                logger.info(f"媒体服务器 {server_name} 清理陈旧数据完成，删除数量：{stale_count}")
                logger.info(f"媒体服务器 {server_name} 数据同步完成，总同步数量：{total_count}")
                if progress_callback:
                    progress_value = (
                        min(global_media_finished / global_media_total, 1) * 100
                        if global_media_total else
                        server_index / total_servers * 100
                    )
                    progress_callback(
                        value=progress_value,
                        text=(
                            f"媒体服务器（{server_index}/{total_servers}）"
                            f"{server_name} 同步完成"
                        ),
                        data={
                            "total": total_servers,
                            "finished": server_index,
                            "media_total": global_media_total,
                            "media_finished": global_media_finished,
                        },
                    )
            if progress_callback:
                progress_callback(value=100, text="媒体服务器同步完成")
