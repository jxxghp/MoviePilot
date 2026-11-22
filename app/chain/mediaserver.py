"""
媒体服务器目录、条目、播放状态、图片访问与本地数据同步。
"""

import threading
from datetime import datetime
from typing import Any, Callable, Dict, Generator, List, Optional, Tuple, Union, cast

from app.application.mediaserver import (
    MediaServerRepository,
    MediaServerSyncItem,
    get_mediaserver_configs,
)
from app.application.security.url import SecurityUtils
from app.chain.base import ChainBase
from app.runtime.log import logger
from app.runtime.stop import runtime_stop_state
from app.schemas.mediaserver import (
    MediaServerBrowseItem,
    MediaServerBrowsePage,
    MediaServerField,
    MediaServerFilterOptions,
    MediaServerItem,
    MediaServerItemDetail,
    MediaServerLibrary,
    MediaServerPlayItem,
    MediaServerQueryOptions,
    MediaServerSeasonInfo,
)
from app.schemas.types import MediaType

lock = threading.Lock()

# 同步只保存本地缓存实际使用的业务内容；播放状态和展示资源不进入同步写端。
_SYNC_QUERY = MediaServerQueryOptions(fields=frozenset({
    MediaServerField.LIBRARY, MediaServerField.ORIGINAL_TITLE, MediaServerField.YEAR,
    MediaServerField.IDENTITY, MediaServerField.PATH, MediaServerField.MUSIC,
}))
_LIBRARY_SYNC_QUERY = MediaServerQueryOptions(fields=frozenset({MediaServerField.PATH, MediaServerField.COUNTS}))
_EPISODE_SYNC_QUERY = MediaServerQueryOptions(fields=frozenset())


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

    @staticmethod
    def _normalize_item_type(item_type: Optional[str]) -> str:
        """
        把不同媒体服务器的原始条目类型归一为电影、电视剧或音乐。
        """
        normalized = str(item_type or "").strip().casefold()
        if normalized in {
            MediaType.MUSIC.value.casefold(),
            "music",
            "musicalbum",
            "artist", "album", "track",
            "audio",
            "song",
        }:
            return MediaType.MUSIC.value
        if normalized in {
            MediaType.TV.value.casefold(),
            "tv",
            "series",
            "show",
        }:
            return MediaType.TV.value
        return MediaType.MOVIE.value

    def _sign_library_images(
        self, libraries: Optional[List[MediaServerLibrary]]
    ) -> Optional[List[MediaServerLibrary]]:
        """
        给媒体库列表中的封面和封面组添加代理签名，并保留提供方失败状态。
        """
        if libraries is None:
            return None
        for library in libraries:
            if library.image:
                library.image = self._sign_image_url(library.image)
            if library.image_list:
                signed_images: list[str] = []
                for image in library.image_list:
                    signed = self._sign_image_url(image)
                    if signed:
                        signed_images.append(signed)
                library.image_list = signed_images
        return libraries

    def _sign_play_item_images(
        self, items: Optional[List[MediaServerPlayItem]], *, server: Optional[str] = None
    ) -> Optional[List[MediaServerPlayItem]]:
        """
        给媒体服务器播放条目中的图片 URL 添加代理签名，并补充服务器配置名。

        :param items: 媒体服务器播放条目列表
        :param server: 可选服务器配置名称，补全缺失的 `item.server`
        :return: 签名后的条目列表，输入为 None 时原样返回
        """
        if items is None:
            return None
        for item in items:
            if server and not item.server:
                item.server = server
            if item.image:
                item.image = self._sign_image_url(item.image)
        return items

    def _sign_browse_item_images(self, item: MediaServerBrowseItem) -> MediaServerBrowseItem:
        """
        为媒体服务器浏览卡片或详情条目中的图片 URL 添加代理签名。

        :param item: 浏览卡片或详情对象
        :return: 原地签名后的对象
        """
        if item.poster:
            item.poster = self._sign_image_url(item.poster)
        if item.backdrop:
            item.backdrop = self._sign_image_url(item.backdrop)
        if item.thumb:
            item.thumb = self._sign_image_url(item.thumb)
        if isinstance(item, MediaServerItemDetail):
            for person in item.people:
                if person.image:
                    person.image = self._sign_image_url(person.image)
        return item

    def librarys(
        self,
        server: str,
        username: Optional[str] = None,
        hidden: bool = False,
        *,
        query: Optional[MediaServerQueryOptions] = None,
    ) -> Optional[List[MediaServerLibrary]]:
        """
        获取媒体服务器所有媒体库

        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        """
        return self._sign_library_images(cast(
            Optional[List[MediaServerLibrary]],
            self.run_module(
                "mediaserver_librarys",
                server=server,
                username=username,
                hidden=hidden,
                **({"query": query} if query is not None else {}),
            ),
        ))

    def items(
        self,
        server: str,
        library_id: Union[str, int],
        start_index: Optional[int] = 0,
        limit: Optional[int] = -1,
        *,
        query: Optional[MediaServerQueryOptions] = None,
    ) -> Generator[Any, None, None]:
        """
        委托具体客户端枚举媒体条目，由客户端处理协议分页、类型过滤与目录展开。

        :param server: 媒体服务器名称
        :param library_id: 媒体库ID，用于标识要获取的媒体库
        :param start_index: 服务端原始条目的起始索引，默认为 0
        :param limit: 单页请求上限；None 或 -1 表示读取全部，由支持内部分页的客户端分批请求
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :yields: 统一媒体条目；结果数量可能因过滤或子目录展开与原始页长不同
        """
        yield from self.run_module(
            "mediaserver_items",
            server=server,
            library_id=library_id,
            start_index=start_index,
            limit=limit,
            **({"query": query} if query is not None else {}),
        )

    def items_count(self, server: str, library_id: Union[str, int]) -> Optional[int]:
        """
        获取指定媒体库可同步的媒体条目总数

        :param server: 媒体服务器名称
        :param library_id: 媒体库ID
        :return: 媒体条目总数，无法获取时返回None
        """
        return cast(Optional[int], self.run_module(
            "mediaserver_items_count",
            server=server,
            library_id=library_id,
        ))

    def media_count(self, server: str) -> Optional[int]:
        """
        获取指定媒体服务器可同步的电影、电视剧和音乐总数

        :param server: 媒体服务器名称
        :return: 电影、电视剧和音乐总数，无法获取时返回None
        """
        statistics = self.run_module("media_statistic", server=server)
        if not statistics:
            return None
        return sum(
            (statistic.movie_count or 0)
            + (statistic.tv_count or 0)
            + (statistic.music_count or 0)
            for statistic in statistics
        )

    def iteminfo(
        self, server: str, item_id: Union[str, int], *, query: Optional[MediaServerQueryOptions] = None
    ) -> MediaServerItem:
        """
        获取媒体服务器项目信息

        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        """
        return cast(MediaServerItem, self.run_module(
            "mediaserver_iteminfo", server=server, item_id=item_id, **({"query": query} if query is not None else {})
        ))

    def find_item(
        self,
        item_id: str,
        *,
        server: Optional[str] = None,
        query: Optional[MediaServerQueryOptions] = None,
    ) -> Optional[Tuple[str, MediaServerItem]]:
        """
        在已启用的媒体服务器中按条目标识查找首个匹配项。

        :param item_id: 媒体服务器条目标识
        :param server: 可选的媒体服务器配置名称，用于避免跨服务器条目标识冲突
        :param query: 可选查询内容投影
        :return: 配置名称和匹配条目；未找到时返回 None
        """
        if not item_id:
            return None
        servers = [server] if server else [config.name for config in get_mediaserver_configs() if config.name]
        for server_name in servers:
            if not server_name:
                continue
            item = self.iteminfo(server=server_name, item_id=item_id, query=query)
            if item and str(item.item_id or item.id or "") == item_id:
                return server_name, item
        return None

    def search_library(
        self,
        keyword: str,
        types: Optional[List[str]] = None,
        limit: int = 30,
        *,
        server: Optional[str] = None,
        library_id: Optional[str] = None,
    ) -> List[MediaServerPlayItem]:
        """
        在已配置媒体服务器中搜索电影、电视剧和音乐单曲，并代理签名海报地址。

        :param keyword: 用户输入的搜索词
        :param types: 允许的业务类型；省略时搜索电影、电视剧和音乐
        :param limit: 返回的最大结果数，服务端适配器也会按此限制请求
        :param server: 可选媒体服务器配置名，省略时跨所有已启用实例搜索
        :param library_id: 可选媒体库标识，仅在指定媒体库内检索
        :return: 带媒体服务器配置名的海报结果
        """
        query = keyword.strip()
        if not query:
            return []
        default_types = [MediaType.MOVIE.value, MediaType.TV.value, MediaType.MUSIC.value]
        selected_types = [value for value in (types or default_types) if value]
        extra_kwargs: Dict[str, Any] = {}
        if server:
            extra_kwargs["server"] = server
        if library_id:
            extra_kwargs["library_id"] = library_id
        items = cast(
            List[MediaServerPlayItem],
            self.run_module(
                "mediaserver_search",
                keyword=query,
                types=selected_types,
                limit=max(1, min(limit, 50)),
                **extra_kwargs,
            ),
        )
        filtered = [item for item in items or [] if item.type in selected_types and item.item_id]
        seen: set[tuple[Optional[str], str]] = set()
        results: List[MediaServerPlayItem] = []
        for item in filtered:
            key = (item.server, str(item.item_id))
            if key in seen:
                continue
            seen.add(key)
            if item.image:
                item.image = self._sign_image_url(item.image)
            results.append(item)
        return results[:max(1, min(limit, 50))]

    def browse_items(
        self,
        server: str,
        *,
        library_id: Optional[str] = None,
        parent_id: Optional[str] = None,
        item_type: Optional[str] = None,
        genre: Optional[str] = None,
        year: Optional[int] = None,
        played_filter: Optional[str] = None,
        sort_by: Optional[str] = None,
        sort_order: Optional[str] = None,
        keyword: Optional[str] = None,
        start_index: int = 0,
        limit: int = 30,
        username: Optional[str] = None,
    ) -> Optional[MediaServerBrowsePage]:
        """
        分页浏览指定媒体服务器媒体库，并签名返回图片地址。

        :param server: 媒体服务器配置名称
        :param library_id: 可选媒体库标识
        :param parent_id: 可选父目录标识
        :param item_type: 可选条目维度过滤
        :param genre: 可选风格过滤
        :param year: 可选年份过滤
        :param played_filter: 可选观看状态过滤
        :param sort_by: 排序键
        :param sort_order: 排序方向
        :param keyword: 库内关键词搜索
        :param start_index: 零基分页偏移
        :param limit: 单页数量上限
        :param username: 当前请求用户名
        :return: 分页浏览结果；服务器不可用时返回 None
        """
        page = cast(
            Optional[MediaServerBrowsePage],
            self.run_module(
                "mediaserver_browse_items",
                server=server,
                library_id=library_id,
                parent_id=parent_id,
                item_type=item_type,
                genre=genre,
                year=year,
                played_filter=played_filter,
                sort_by=sort_by,
                sort_order=sort_order,
                keyword=keyword,
                start_index=start_index,
                limit=limit,
                username=username,
            ),
        )
        if page:
            for item in page.items:
                if not item.server:
                    item.server = server
                self._sign_browse_item_images(item)
        return page

    def filter_options(
        self,
        server: str,
        *,
        library_id: Optional[str] = None,
        username: Optional[str] = None,
    ) -> Optional[MediaServerFilterOptions]:
        """
        查询指定媒体服务器媒体库可用的风格、年份与排序选项。

        :param server: 媒体服务器配置名称
        :param library_id: 可选媒体库标识
        :param username: 当前请求用户名
        :return: 筛选项模型；不支持时返回 None
        """
        return cast(
            Optional[MediaServerFilterOptions],
            self.run_module(
                "mediaserver_filter_options",
                server=server,
                library_id=library_id,
                username=username,
            ),
        )

    def item_detail(
        self,
        server: str,
        item_id: Union[str, int],
        *,
        username: Optional[str] = None,
    ) -> Optional[MediaServerItemDetail]:
        """
        获取单个媒体服务器条目的完整详情并签名图片。

        :param server: 媒体服务器配置名称
        :param item_id: 媒体服务器条目标识
        :param username: 当前请求用户名
        :return: 完整详情模型；未找到时返回 None
        """
        detail = cast(
            Optional[MediaServerItemDetail],
            self.run_module(
                "mediaserver_item_detail",
                server=server,
                item_id=str(item_id),
                username=username,
            ),
        )
        if detail:
            if not detail.server:
                detail.server = server
            self._sign_browse_item_images(detail)
        return detail

    def seasons(
        self,
        server: str,
        item_id: Union[str, int],
        *,
        username: Optional[str] = None,
    ) -> Optional[List[MediaServerBrowseItem]]:
        """
        查询电视剧的全部季卡片信息并签名图片。

        :param server: 媒体服务器配置名称
        :param item_id: 电视剧条目标识
        :param username: 当前请求用户名
        :return: 季卡片列表；失败时返回 None
        """
        items = cast(
            Optional[List[MediaServerBrowseItem]],
            self.run_module(
                "mediaserver_seasons",
                server=server,
                item_id=str(item_id),
                username=username,
            ),
        )
        if items is not None:
            for item in items:
                if not item.server:
                    item.server = server
                self._sign_browse_item_images(item)
        return items

    def season_episodes(
        self,
        server: str,
        item_id: Union[str, int],
        *,
        season_id: Optional[str] = None,
        season: Optional[int] = None,
        username: Optional[str] = None,
    ) -> Optional[List[MediaServerBrowseItem]]:
        """
        查询指定季的全部单集详情卡片并签名图片。

        :param server: 媒体服务器配置名称
        :param item_id: 电视剧或专辑条目标识
        :param season_id: 可选季条目标识
        :param season: 可选季号
        :param username: 当前请求用户名
        :return: 单集卡片列表；失败时返回 None
        """
        items = cast(
            Optional[List[MediaServerBrowseItem]],
            self.run_module(
                "mediaserver_season_episodes",
                server=server,
                item_id=str(item_id),
                season_id=season_id,
                season=season,
                username=username,
            ),
        )
        if items is not None:
            for item in items:
                if not item.server:
                    item.server = server
                self._sign_browse_item_images(item)
        return items

    def get_subtitle_url(self, server: str, item_id: Union[str, int], subtitle_id: str) -> Optional[str]:
        """
        获取媒体服务器托管的外挂或可提取字幕上游流地址。

        :param server: 媒体服务器配置名称
        :param item_id: 媒体条目标识
        :param subtitle_id: 代理字幕标识
        :return: 上游字幕流地址；不支持或未匹配时返回 None
        """
        return cast(
            Optional[str],
            self.run_module(
                "mediaserver_subtitle_url",
                server=server,
                item_id=str(item_id),
                subtitle_id=subtitle_id,
            ),
        )

    def report_progress(
        self,
        server: str,
        item_id: Union[str, int],
        position_ms: int,
        *,
        media_source_id: Optional[str] = None,
        duration_ms: Optional[int] = None,
        is_paused: bool = False,
        state: str = "playing",
        username: Optional[str] = None,
    ) -> bool:
        """
        将客户端播放进度回传给媒体服务器。

        :param server: 媒体服务器配置名称
        :param item_id: 媒体条目标识
        :param position_ms: 当前播放位置（毫秒）
        :param media_source_id: 可选媒体源标识
        :param duration_ms: 可选总时长（毫秒）
        :param is_paused: 是否暂停
        :param state: 播放状态
        :param username: 当前用户名
        :return: 回传成功返回 True
        """
        result = self.run_module(
            "mediaserver_report_progress",
            server=server,
            item_id=str(item_id),
            position_ms=position_ms,
            media_source_id=media_source_id,
            duration_ms=duration_ms,
            is_paused=is_paused,
            state=state,
            username=username,
        )
        return bool(result)

    def episodes(
        self, server: str, item_id: Union[str, int], *, query: Optional[MediaServerQueryOptions] = None
    ) -> List[MediaServerSeasonInfo]:
        """
        获取媒体服务器剧集信息

        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        """
        return cast(List[MediaServerSeasonInfo], self.run_module(
            "mediaserver_tv_episodes", server=server, item_id=item_id, **({"query": query} if query is not None else {})
        ))

    def playing(
        self,
        server: str,
        count: Optional[int] = 20,
        username: Optional[str] = None,
        *,
        query: Optional[MediaServerQueryOptions] = None,
    ) -> Optional[List[MediaServerPlayItem]]:
        """
        获取媒体服务器正在播放信息

        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        """
        return self._sign_play_item_images(
            self.run_module(
                "mediaserver_playing",
                count=count,
                server=server,
                username=username,
                **({"query": query} if query is not None else {}),
            ),
            server=server,
        )

    def latest(
        self,
        server: str,
        count: Optional[int] = 20,
        username: Optional[str] = None,
        *,
        query: Optional[MediaServerQueryOptions] = None,
    ) -> Optional[List[MediaServerPlayItem]]:
        """
        获取媒体服务器最新入库条目

        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        """
        return self._sign_play_item_images(
            self.run_module(
                "mediaserver_latest",
                count=count,
                server=server,
                username=username,
                **({"query": query} if query is not None else {}),
            ),
            server=server,
        )

    def get_latest_wallpapers(
        self,
        server: Optional[str] = None,
        count: Optional[int] = 10,
        remote: bool = True,
        username: Optional[str] = None,
        *,
        query: Optional[MediaServerQueryOptions] = None,
    ) -> List[str]:
        """
        获取最新最新入库条目海报作为壁纸，缓存1小时

        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        """
        wallpapers = self.run_module(
            "mediaserver_latest_images",
            server=server,
            count=count,
            remote=remote,
            username=username,
            **({"query": query} if query is not None else {}),
        )
        signed_wallpapers: list[str] = []
        for wallpaper in wallpapers or []:
            signed = self._sign_image_url(wallpaper)
            if signed:
                signed_wallpapers.append(signed)
        return signed_wallpapers

    def get_latest_wallpaper(
        self,
        server: Optional[str] = None,
        remote: bool = True,
        username: Optional[str] = None,
        *,
        query: Optional[MediaServerQueryOptions] = None,
    ) -> Optional[str]:
        """
        获取最新最新入库条目海报作为壁纸，缓存1小时

        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        """
        wallpapers = self.get_latest_wallpapers(
            server=server,
            count=1,
            remote=remote,
            username=username,
            **({"query": query} if query is not None else {}),
        )
        return wallpapers[0] if wallpapers else None

    def get_play_url(self, server: str, item_id: Union[str, int]) -> Optional[str]:
        """
        获取播放地址
        """
        return cast(Optional[str], self.run_module("mediaserver_play_url", server=server, item_id=item_id))

    def get_stream_url(
        self,
        server: str,
        item_id: Union[str, int],
        item_type: Optional[str],
        media_source_id: Optional[str] = None,
    ) -> Optional[str]:
        """
        获取媒体服务器支持 HTTP Range 的原始媒体流地址，支持指定多版本媒体源。

        :param server: 媒体服务器配置名称
        :param item_id: 媒体服务器条目标识
        :param item_type: 媒体服务器返回的条目类型
        :param media_source_id: 可选指定的多版本媒体源标识
        :return: 原始媒体流地址；当前适配器不支持时返回 None
        """
        extra = {"media_source_id": media_source_id} if media_source_id else {}
        return cast(
            Optional[str],
            self.run_module(
                "mediaserver_stream_url",
                server=server,
                item_id=item_id,
                item_type=item_type,
                **extra,
            ),
        )

    def get_season_episode_ids(
        self, server: str, item_id: Union[str, int], season: int, *, query: Optional[MediaServerQueryOptions] = None
    ) -> Dict[int, str]:
        """
        获取指定季的集号到媒体服务器条目 ID 映射

        :param server: 媒体服务器名称
        :param item_id: 剧集在媒体服务器中的条目 ID
        :param season: 季号
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :return: 集号到条目 ID 的映射，无数据时返回空字典
        """
        result = self.run_module(
            "mediaserver_season_episode_ids",
            server=server,
            item_id=item_id,
            season=season,
            **({"query": query} if query is not None else {}),
        )
        return cast(Dict[int, str], result or {})

    def get_image_cookies(
        self, server: Optional[str], image_url: str
    ) -> Optional[str | dict[str, Any]]:
        """
        获取图片的Cookies
        """
        return cast(str | dict[str, Any] | None, self.run_module(
            "mediaserver_image_cookies", server=server, image_url=image_url
        ))

    def _prepare_sync_contexts(
        self,
        mediaservers: List[Any],
        server: Optional[str],
        repository: MediaServerRepository,
    ) -> Tuple[List[Any], int, Dict[str, Any], Optional[int]]:
        """
        准备启用媒体服务器的同步库和进度总量。

        :return: (待同步服务器、服务器数量、服务器同步上下文、全局媒体总数)
        """
        enabled_servers = [
            item.name for item in mediaservers
            if item and item.enabled and isinstance(item.name, str) and item.name
        ]
        repository.delete_excluded_servers(enabled_servers)
        selected_servers = [
            item for item in mediaservers
            if item and item.enabled and isinstance(item.name, str)
            and (not server or item.name == server)
        ]
        total_servers = len(selected_servers)
        contexts: Dict[str, Any] = {}
        global_media_total = 0
        global_counts_available = True
        for mediaserver in selected_servers:
            server_name = str(mediaserver.name or "")
            logger.info(f"正在统计媒体服务器 {server_name} 的待同步媒体数量")
            libraries = self.librarys(server_name, hidden=True, query=_LIBRARY_SYNC_QUERY)
            if not libraries:
                contexts[server_name] = None
                continue
            sync_libraries = mediaserver.sync_libraries or []
            selected_libraries = [
                library
                for library in libraries
                if not sync_libraries or "all" in sync_libraries or str(library.id) in sync_libraries
            ]
            library_media_counts = {}
            for library in selected_libraries:
                if library.id is None:
                    continue
                media_count = getattr(library, "item_count", None)
                if media_count is None:
                    media_count = self.items_count(server=server_name, library_id=library.id)
                library_media_counts[str(library.id)] = media_count
                if media_count is None:
                    global_counts_available = False
                    logger.warning(
                        f"未获取到 {server_name} 媒体库 {library.name} 的媒体总数，同步进度将按媒体库完成度计算"
                    )
                else:
                    global_media_total += media_count
            contexts[server_name] = (selected_libraries, library_media_counts)
        return selected_servers, total_servers, contexts, global_media_total if global_counts_available else None

    def _sync_server_libraries(
        self,
        server_name: str,
        selected_libraries: List[Any],
        library_media_counts: Dict[str, Optional[int]],
        repository: MediaServerRepository,
        sync_time: str,
        progress_callback: Optional[Callable[..., None]],
        server_index: int,
        total_servers: int,
        global_media_total: Optional[int],
        global_media_finished: int,
    ) -> Tuple[int, int]:
        """
        同步单个媒体服务器的媒体库并清理陈旧条目。

        :return: (本服务器写入数量、更新后的全局已完成媒体数)
        """
        total_count = 0
        total_libraries = len(selected_libraries)
        for library_index, library in enumerate(selected_libraries, start=1):
            logger.info(f"正在同步 {server_name} 媒体库 {library.name} ...")
            library_media_total = library_media_counts.get(str(library.id))
            library_count = 0
            if library.id is None:
                continue
            for item in self.items(server=server_name, library_id=library.id, query=_SYNC_QUERY):
                if runtime_stop_state.is_system_stopped:
                    return total_count, global_media_finished
                if not item or not item.item_id:
                    continue
                logger.debug(f"正在同步 {item.title} ...")
                library_count += 1
                global_media_finished += 1
                seasoninfo = {}
                item_type = self._normalize_item_type(item.item_type)
                if item_type == MediaType.TV.value:
                    for episode in self.episodes(server_name, item.item_id, query=_EPISODE_SYNC_QUERY) or []:
                        if episode.season is None:
                            continue
                        seasoninfo[episode.season] = episode.episodes or []
                repository.upsert(
                    MediaServerSyncItem.from_item(
                        item,
                        item_type=item_type,
                        seasoninfo=seasoninfo,
                        sync_time=sync_time,
                        server=server_name,
                    )
                )
                if progress_callback:
                    if global_media_total:
                        progress_value = min(global_media_finished / global_media_total, 1) * 100
                    else:
                        library_progress = min(library_count / library_media_total, 1) if library_media_total else 0
                        server_progress = (library_index - 1 + library_progress) / total_libraries
                        progress_value = (server_index - 1 + server_progress) / total_servers * 100
                    progress_callback(
                        value=progress_value,
                        text=(
                            f"正在同步 {server_name} 媒体库 {library.name}（{library_count}/{library_media_total}）"
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
            total_count += library_count
            if progress_callback:
                progress_value = (
                    min(global_media_finished / global_media_total, 1) * 100
                    if global_media_total
                    else (server_index - 1 + library_index / total_libraries) / total_servers * 100
                )
                progress_callback(
                    value=progress_value,
                    text=f"{server_name} 媒体库（{library_index}/{total_libraries}）{library.name} 同步完成",
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
        stale_count = repository.delete_stale(
            server=server_name,
            sync_time=sync_time,
        )
        logger.info(f"媒体服务器 {server_name} 清理陈旧数据完成，删除数量：{stale_count}")
        return total_count, global_media_finished

    def sync(
            self,
            progress_callback: Optional[Callable[..., None]] = None,
            server: Optional[str] = None,
    ) -> None:
        """
        兼容媒体库同步公开入口，委托给内部同步执行阶段。

        定时器、API 和插件使用该稳定入口；内部阶段负责统计、逐库读取、写入和陈旧清理，
        以便后续独立演进进度策略而不改变调用方参数。
        """
        return self._execute_sync(
            progress_callback=progress_callback,
            server=server,
        )

    def _execute_sync(
            self,
            progress_callback: Optional[Callable[..., None]] = None,
            server: Optional[str] = None,
    ) -> None:
        """
        同步全部或指定媒体服务器的媒体库数据到本地数据库

        :param progress_callback: 定时服务进度更新回调
        :param server: 指定媒体服务器名称，为空时同步全部已启用服务器
        """
        mediaservers = get_mediaserver_configs(include_disabled=True)
        if not mediaservers:
            if progress_callback:
                progress_callback(value=100, text="未配置媒体服务器，跳过同步")
            return
        with lock:
            total_count = 0
            repository = self.media_server_repository
            mediaservers, total_servers, server_sync_contexts, global_media_total = (
                self._prepare_sync_contexts(mediaservers, server, repository)
            )
            if progress_callback:
                progress_callback(
                    value=0,
                    text=f"开始同步媒体服务器，共 {total_servers} 个 ...",
                    data={"total": total_servers, "finished": 0},
                )
            if not total_servers:
                if progress_callback:
                    progress_callback(
                        value=100,
                        text=(
                            f"媒体服务器 {server} 未启用或不存在"
                            if server else "没有已启用的媒体服务器"
                        ),
                    )
                return

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
                server_name = str(mediaserver.name or "")
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
                server_count, global_media_finished = self._sync_server_libraries(
                    server_name=server_name,
                    selected_libraries=selected_libraries,
                    library_media_counts=library_media_counts,
                    repository=repository,
                    sync_time=sync_time,
                    progress_callback=progress_callback,
                    server_index=server_index,
                    total_servers=total_servers,
                    global_media_total=global_media_total,
                    global_media_finished=global_media_finished,
                )
                if runtime_stop_state.is_system_stopped:
                    return
                total_count += server_count
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
