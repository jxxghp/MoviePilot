from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Tuple, Union

from app.application.mediaserver import MediaServerIdentityHelper
from app.foundation.url import UrlUtils
from app.modules.mediavault.api import Api
from app.runtime.log import logger
from app.schemas.dashboard import Statistic as _SchemaStatistic
from app.schemas.mediaserver import MediaServerItem as _SchemaMediaServerItem
from app.schemas.mediaserver import MediaServerItemUserState as _SchemaMediaServerItemUserState
from app.schemas.mediaserver import MediaServerLibrary as _SchemaMediaServerLibrary
from app.schemas.mediaserver import MediaServerPlayItem as _SchemaMediaServerPlayItem
from app.schemas.mediaserver import RefreshMediaItem as _SchemaRefreshMediaItem
from app.schemas.types import MediaSource, MediaType


class _SearchInterrupted(Exception):
    """关键字翻页中途请求失败。

    与「查完了」区分开：调用方据此返回 None（服务不可达），而不是空结果
    （确定不在库中）——后者会让上层误判成需要重新下载。
    """


class MediaVault:
    """MediaVault 自建媒体库客户端。

    只使用 MediaVault 管理端口的原生接口，凭据是管理面板的长效 API Key；
    自建媒体库没有音乐库，也不主动外发 Webhook，相关能力在模块层显式留空。
    """

    # MediaVault 单次列表请求的最大条数，与服务端 page_size 上限一致
    PAGE_LIMIT = 100
    # 媒体库类型到 MoviePilot 媒体类型的映射
    LIBRARY_TYPES = {"movies": MediaType.MOVIE.value, "tvshows": MediaType.TV.value}

    def __init__(
        self,
        host: Optional[str] = None,
        apikey: Optional[str] = None,
        play_host: Optional[str] = None,
        sync_libraries: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> None:
        self._host = UrlUtils.standardize_base_url(host).rstrip("/") if host else None
        self._playhost = (
            UrlUtils.standardize_base_url(play_host).rstrip("/") if play_host else None
        )
        self._apikey = apikey
        self._sync_libraries = sync_libraries or []
        self._api = Api(host=host, apikey=apikey)
        self._active = False
        if not self.is_configured():
            logger.error("MediaVault 配置不完整！")
            return
        self.reconnect()

    # ── 连接状态 ────────────────────────────────────────────────

    def is_configured(self) -> bool:
        """配置是否完整到可以发起请求。"""
        return bool(self._host and self._apikey)

    def is_authenticated(self) -> bool:
        """当前 API Key 是否已探测通过。"""
        return self._active

    def is_inactive(self) -> bool:
        """是否需要重连。"""
        if not self.is_configured():
            return False
        return not self._active

    def reconnect(self) -> bool:
        """用媒体库列表接口探测连通性与凭据有效性。"""
        if not self.is_configured():
            return False
        result = self._api.request("/libraries", suppress_log=True)
        self._active = bool(result and result.success)
        return self._active

    def disconnect(self) -> None:
        """释放底层会话。"""
        self._active = False
        self._api.close()

    def authenticate(self, username: str, password: str) -> Optional[str]:
        """用 MediaVault 账号完成用户认证，返回访问令牌。"""
        if not self.is_configured() or not username or not password:
            return None
        result = self._api.request(
            "/login",
            method="post",
            data={"username": username, "password": password},
            base_path="/api/v1/user-auth",
            suppress_log=True,
        )
        if not result or not result.success or not isinstance(result.data, dict):
            return None
        token = result.data.get("access_token") or result.data.get("token")
        return str(token) if token else None

    # ── 媒体库 ──────────────────────────────────────────────────

    def get_librarys(
        self, hidden: Optional[bool] = False
    ) -> Optional[List[_SchemaMediaServerLibrary]]:
        """获取媒体库列表。"""
        rows = self.__library_rows()
        if rows is None:
            return None
        libraries = []
        for row in rows:
            library_id = str(row.get("id") or "")
            if not library_id:
                continue
            if (
                hidden
                and self._sync_libraries
                and "all" not in self._sync_libraries
                and library_id not in self._sync_libraries
            ):
                continue
            library_type = self.LIBRARY_TYPES.get(
                str(row.get("library_type") or ""), MediaType.UNKNOWN.value
            )
            libraries.append(
                _SchemaMediaServerLibrary(
                    server="mediavault",
                    id=library_id,
                    item_id=library_id,
                    name=row.get("name"),
                    path=row.get("root_paths") or row.get("root_path"),
                    type=library_type,
                    item_count=self.get_items_count(library_id),
                    image=self._api.image_url(library_id, "primary"),
                    link=f"{self._playhost or self._host}/library",
                    server_type="mediavault",
                )
            )
        return libraries

    def __library_rows(self) -> Optional[List[Dict[str, Any]]]:
        """媒体库原始记录，连接失败返回 None。"""
        result = self._api.request("/libraries")
        if not result or not result.success or not isinstance(result.data, dict):
            return None
        items = result.data.get("items")
        return items if isinstance(items, list) else []

    def get_items_count(self, parent: Union[str, int]) -> Optional[int]:
        """获取指定媒体库可同步的媒体条目总数。"""
        if not parent:
            return None
        result = self.__query_items(
            library_id=str(parent), kinds="Movie,Series", page=1, page_size=1
        )
        if result is None:
            return None
        total = result.get("total")
        return int(total) if total is not None else None

    def get_items(
        self,
        parent: Union[str, int],
        start_index: Optional[int] = 0,
        limit: Optional[int] = -1,
    ) -> Generator[Optional[_SchemaMediaServerItem], Any, None]:
        """遍历媒体库中的电影与剧集条目，limit 为 None 或 -1 时取全部。"""
        if not parent or not self.is_configured():
            return
        # 页大小固定，页码才能稳定映射到偏移量；条数上限在产出侧裁剪
        skip = max(0, start_index or 0)
        remaining = None if limit is None or limit == -1 else max(0, limit)
        page = skip // self.PAGE_LIMIT + 1
        drop = skip % self.PAGE_LIMIT
        while remaining is None or remaining > 0:
            result = self.__query_items(
                library_id=str(parent),
                kinds="Movie,Series",
                page=page,
                page_size=self.PAGE_LIMIT,
            )
            if result is None:
                return
            rows = result.get("items") or []
            for row in rows[drop:]:
                if remaining is not None and remaining <= 0:
                    return
                item = self.__format_item_info(row)
                if item:
                    yield item
                if remaining is not None:
                    remaining -= 1
            if len(rows) < self.PAGE_LIMIT:
                return
            drop = 0
            page += 1

    def __query_items(
        self,
        library_id: str = "",
        parent_id: Optional[str] = None,
        keyword: str = "",
        kinds: str = "",
        page: int = 1,
        page_size: int = 40,
        filter_by: str = "",
        sort_by: str = "",
        sort_order: str = "",
    ) -> Optional[Dict[str, Any]]:
        """调用条目列表接口；MediaVault 只接受页码，偏移量由调用方换算。"""
        params: Dict[str, Any] = {"page": max(1, page), "page_size": page_size}
        if library_id:
            params["library_id"] = library_id
        if parent_id is not None:
            params["parent_id"] = parent_id
        if keyword:
            params["keyword"] = keyword
        if kinds:
            params["kinds"] = kinds
        if filter_by:
            params["filter_by"] = filter_by
        if sort_by:
            params["sort_by"] = sort_by
        if sort_order:
            params["sort_order"] = sort_order
        result = self._api.request("/items", params=params)
        if not result or not result.success or not isinstance(result.data, dict):
            return None
        return result.data

    def __search_rows(self, keyword: str, kinds: str) -> Optional[Generator[Dict[str, Any], Any, None]]:
        """按关键字翻页产出条目原始记录。

        MediaVault 的关键字查询是模糊匹配，命中数可能超过单页上限；只读第一页会让
        存在性判断把排在后面的目标误判成「未入库」，因此这里翻页直到取完。
        连接失败返回 None，与「查得到但没有」区分开。
        """
        first = self.__query_items(keyword=keyword, kinds=kinds, page=1, page_size=self.PAGE_LIMIT)
        if first is None:
            return None

        def rows() -> Generator[Dict[str, Any], Any, None]:
            result: Dict[str, Any] = first
            page = 1
            seen = 0
            while True:
                batch = result.get("items") or []
                seen += len(batch)
                yield from batch
                total = result.get("total")
                # 有可靠总数时以它为准：末页恰好满额也不再多发一次请求，
                # 那次多余请求一旦失败会把完整结果误报成服务不可达
                if isinstance(total, int):
                    if seen >= total:
                        return
                elif len(batch) < self.PAGE_LIMIT:
                    return
                page += 1
                following = self.__query_items(
                    keyword=keyword, kinds=kinds, page=page, page_size=self.PAGE_LIMIT
                )
                if following is None:
                    raise _SearchInterrupted
                result = following

        return rows()

    # ── 条目 ────────────────────────────────────────────────────

    def get_iteminfo(self, itemid: str) -> Optional[_SchemaMediaServerItem]:
        """获取单个条目详情。"""
        if not itemid or not self.is_configured():
            return None
        result = self._api.request(f"/items/{itemid}", suppress_log=True)
        if not result or not result.success or not isinstance(result.data, dict):
            return None
        return self.__format_item_info(result.data)

    def get_movies(
        self,
        title: str,
        year: Optional[str] = None,
        media_source: Optional[MediaSource] = None,
        media_id: Optional[str] = None,
    ) -> Optional[List[_SchemaMediaServerItem]]:
        """按标题和年份检查电影是否存在。"""
        if not title or not self.is_configured():
            return None
        rows = self.__search_rows(keyword=title, kinds="Movie")
        if rows is None:
            return None
        movies = []
        try:
            search_rows = list(rows)
        except _SearchInterrupted:
            return None
        for row in search_rows:
            item = self.__format_item_info(row)
            if not item or item.title != title:
                continue
            if year and str(item.year) != str(year):
                continue
            if not MediaServerIdentityHelper.is_compatible(item, media_source, media_id):
                continue
            movies.append(item)
        return movies

    def get_tv_episodes(
        self,
        item_id: Optional[str] = None,
        title: Optional[str] = None,
        year: Optional[str] = None,
        media_source: Optional[MediaSource] = None,
        media_id: Optional[str] = None,
        season: Optional[int] = None,
    ) -> Tuple[Optional[str], Optional[Dict[int, List[int]]]]:
        """返回剧集在媒体库中每季已有的集号。"""
        if not self.is_configured():
            return None, None
        series_id = item_id
        if series_id:
            info = self.get_iteminfo(series_id)
            if not info or not MediaServerIdentityHelper.is_compatible(
                info, media_source, media_id
            ):
                # 缓存的条目 ID 失效或指向了别的剧，退回按标题重新定位
                series_id = None
        if not series_id:
            if not title:
                return None, {}
            series_id = self.__find_series_id(title, year, media_source, media_id)
            if series_id is None:
                return None, None
            if not series_id:
                return None, {}
        result = self._api.request(f"/items/{series_id}/episodes")
        if not result or not result.success or not isinstance(result.data, dict):
            return None, None
        seasons: Dict[int, List[int]] = {}
        for raw_season, episodes in (result.data.get("seasons") or {}).items():
            try:
                season_index = int(raw_season)
            except (TypeError, ValueError):
                continue
            if season is not None and season_index != season:
                continue
            seasons[season_index] = sorted(
                {int(episode) for episode in episodes if episode is not None}
            )
        return series_id, seasons

    def __find_series_id(
        self,
        title: str,
        year: Optional[str],
        media_source: Optional[MediaSource],
        media_id: Optional[str],
    ) -> Optional[str]:
        """按标题定位剧集条目 ID；连接失败返回 None，未找到返回空串。"""
        rows = self.__search_rows(keyword=title, kinds="Series")
        if rows is None:
            return None
        try:
            # 逐行检查、命中即返回：预取全部页会让第一页已命中的目标
            # 因后续页请求失败被误报成服务不可达
            for row in rows:
                item = self.__format_item_info(row)
                if not item or item.title != title:
                    continue
                if year and str(item.year) != str(year):
                    continue
                if not MediaServerIdentityHelper.is_compatible(item, media_source, media_id):
                    continue
                return str(item.item_id)
        except _SearchInterrupted:
            return None
        return ""

    def get_season_episode_ids(self, item_id: str, season: int) -> Dict[int, str]:
        """获取指定季的集号到条目 ID 映射。"""
        if not item_id or not self.is_configured():
            return {}
        season_id = self.__find_season_id(item_id, season)
        if not season_id:
            return {}
        episode_ids: Dict[int, str] = {}
        page = 1
        while True:
            result = self.__query_items(
                parent_id=season_id, kinds="Episode", page=page, page_size=self.PAGE_LIMIT
            )
            if result is None:
                return episode_ids
            rows = result.get("items") or []
            for row in rows:
                episode = row.get("episode")
                row_id = row.get("id")
                if episode is None or not row_id:
                    continue
                episode_ids[int(episode)] = str(row_id)
            if len(rows) < self.PAGE_LIMIT:
                return episode_ids
            page += 1

    def __find_season_id(self, series_id: str, season: int) -> str:
        """在剧集下定位指定季的条目 ID。"""
        page = 1
        while True:
            result = self.__query_items(
                parent_id=series_id, kinds="Season", page=page, page_size=self.PAGE_LIMIT
            )
            if result is None:
                return ""
            rows = result.get("items") or []
            for row in rows:
                if row.get("season") == season and row.get("id"):
                    return str(row["id"])
            if len(rows) < self.PAGE_LIMIT:
                return ""
            page += 1

    def __format_item_info(self, row: Dict[str, Any]) -> Optional[_SchemaMediaServerItem]:
        """把 MediaVault 条目转换为统一媒体服务器模型。"""
        try:
            metadata = row.get("metadata_info") or {}
            provider_ids: Dict[str, Any] = {}
            if row.get("tmdb_id"):
                provider_ids["Tmdb"] = str(row["tmdb_id"])
            for source, target in (("imdb_id", "Imdb"), ("tvdb_id", "Tvdb")):
                value = (metadata.get("external_ids") or {}).get(source)
                if value:
                    provider_ids[target] = str(value)
            media_source, media_id = MediaServerIdentityHelper.from_provider_ids(provider_ids)
            user_data = row.get("user_data") or {}
            position = user_data.get("position_ticks") or 0
            return _SchemaMediaServerItem(
                server="mediavault",
                library=row.get("library_id"),
                item_id=str(row.get("id") or ""),
                item_type=row.get("kind"),
                title=row.get("title"),
                original_title=metadata.get("original_title"),
                year=row.get("year") or None,
                media_source=media_source,
                media_id=media_id,
                path=self.__item_path(row),
                user_state=_SchemaMediaServerItemUserState(
                    played=user_data.get("played"),
                    resume=position > 0,
                    last_played_date=self.__local_time(user_data.get("last_played_at")),
                    play_count=int(bool(user_data.get("played"))),
                ) if user_data else None,
            )
        except Exception as err:
            logger.error(f"解析 MediaVault 条目失败：{err}")
            return None

    @staticmethod
    def __item_path(row: Dict[str, Any]) -> Optional[str]:
        """条目详情带媒体源时取第一个文件路径，列表接口没有路径字段。"""
        sources = row.get("sources")
        if isinstance(sources, list):
            for source in sources:
                if isinstance(source, dict) and source.get("path"):
                    return str(source["path"])
        return None

    @staticmethod
    def __local_time(value: Optional[str]) -> Optional[str]:
        """把 ISO 时间截断为统一模型使用的秒级本地时间文本。"""
        if not value:
            return None
        return str(value).split(".")[0].replace("T", " ")

    # ── 统计与展示 ──────────────────────────────────────────────

    def get_medias_count(self) -> Optional[_SchemaStatistic]:
        """媒体数量统计。"""
        result = self._api.request("/statistics")
        if not result or not result.success or not isinstance(result.data, dict):
            return None
        return _SchemaStatistic(
            movie_count=result.data.get("movie_count") or 0,
            tv_count=result.data.get("series_count") or 0,
            episode_count=result.data.get("episode_count") or 0,
        )

    def get_user_count(self) -> int:
        """媒体库可见用户数。"""
        result = self._api.request("/users", suppress_log=True)
        if not result or not result.success or not isinstance(result.data, list):
            return 0
        return len(result.data)

    def get_resume(self, num: Optional[int] = 12) -> Optional[List[_SchemaMediaServerPlayItem]]:
        """继续观看列表。"""
        return self.__play_items(filter_by="resume", num=num, resume=True)

    def get_latest(self, num: Optional[int] = 20) -> Optional[List[_SchemaMediaServerPlayItem]]:
        """最新入库列表。"""
        return self.__play_items(sort_by="added", sort_order="desc", num=num, resume=False)

    def __play_items(
        self,
        num: Optional[int],
        resume: bool,
        filter_by: str = "",
        sort_by: str = "",
        sort_order: str = "",
    ) -> Optional[List[_SchemaMediaServerPlayItem]]:
        """把条目列表转换为可播放展示项。"""
        if not self.is_configured():
            return None
        count = max(1, min(self.PAGE_LIMIT, num or 20))
        result = self.__query_items(
            kinds="Movie,Episode" if resume else "Movie,Series",
            filter_by=filter_by,
            sort_by=sort_by,
            sort_order=sort_order,
            page_size=count,
        )
        if result is None:
            return None
        items = []
        for row in (result.get("items") or [])[:count]:
            row_id = str(row.get("id") or "")
            if not row_id:
                continue
            is_episode = row.get("kind") == "Episode"
            # Series 与 Episode 都是电视剧；只有分集才拼「季:集 - 分集名」副标题
            is_tv = is_episode or row.get("kind") == "Series"
            title: Optional[str] = row.get("title")
            subtitle: Optional[str] = None
            if is_episode:
                title = row.get("series_name") or row.get("title")
                subtitle = f'S{row.get("season")}:{row.get("episode")} - {row.get("title")}'
            elif row.get("year"):
                subtitle = str(row["year"])
            image_id = row.get("series_id") if is_episode else row_id
            percent = None
            duration = row.get("duration_ticks") or 0
            position = (row.get("user_data") or {}).get("position_ticks") or 0
            if duration > 0 and position > 0:
                percent = round(position / duration * 100, 2)
            items.append(
                _SchemaMediaServerPlayItem(
                    id=row_id,
                    item_id=row_id,
                    title=title,
                    subtitle=subtitle,
                    type=MediaType.TV.value if is_tv else MediaType.MOVIE.value,
                    image=self._api.image_url(str(image_id or row_id), "primary"),
                    link=self.get_play_url(row_id),
                    percent=percent,
                    server_type="mediavault",
                )
            )
        return items

    def get_latest_backdrops(
        self, num: Optional[int] = 20, remote: Optional[bool] = False
    ) -> Optional[List[str]]:
        """最新入库条目的背景图地址。"""
        if not self.is_configured():
            return None
        count = max(1, min(self.PAGE_LIMIT, num or 20))
        # 没有背景图的条目会白占名额，多取一批再按实际有图的截断
        result = self.__query_items(
            kinds="Movie,Series", sort_by="added", sort_order="desc", page_size=self.PAGE_LIMIT
        )
        if result is None:
            return None
        host = (self._playhost or self._host) if remote else self._host
        images = []
        for row in result.get("items") or []:
            if not row.get("has_backdrop") or not row.get("id"):
                continue
            images.append(self._api.image_url(str(row["id"]), "backdrop", host=host))
            if len(images) == count:
                break
        return images

    def get_play_url(self, item_id: str) -> Optional[str]:
        """媒体库网页播放地址。"""
        if not item_id or not self.is_configured():
            return None
        return f"{self._playhost or self._host}/library/item/{item_id}"

    # ── 入库刷新 ────────────────────────────────────────────────

    def refresh_root_library(self) -> Optional[bool]:
        """触发全部媒体库扫描。"""
        rows = self.__library_rows()
        if rows is None:
            return None
        results = [self.__queue_scan(str(row.get("id"))) for row in rows if row.get("id")]
        return all(results) if results else False

    def refresh_library_by_items(
        self, items: List[_SchemaRefreshMediaItem]
    ) -> Optional[bool]:
        """按入库路径定位媒体库并触发扫描；定位不到时退回全库扫描。"""
        if not items:
            return False
        rows = self.__library_rows()
        if rows is None:
            return None
        matched = set()
        unmatched = False
        for item in items:
            library_id = self.__match_library_by_path(rows, item.target_path)
            if library_id:
                matched.add(library_id)
            else:
                unmatched = True
                logger.info(f"MediaVault 中未找到 {item.title} 对应的媒体库，将扫描全部媒体库")
        if unmatched:
            return self.refresh_root_library()
        # 先全部发出排队请求再汇总：交给 all() 的生成器会在首个失败处短路，
        # 导致同批命中的其余媒体库收不到扫描任务
        results = [self.__queue_scan(library_id) for library_id in sorted(matched)]
        return all(results)

    @staticmethod
    def __match_library_by_path(
        rows: List[Dict[str, Any]], target_path: Optional[Path]
    ) -> str:
        """按目录归属把入库路径映射到媒体库。"""
        if not target_path:
            return ""
        for row in rows:
            roots = row.get("root_paths") or ([row["root_path"]] if row.get("root_path") else [])
            for root in roots:
                try:
                    if target_path == Path(root) or Path(root) in target_path.parents:
                        return str(row.get("id") or "")
                except (TypeError, ValueError):
                    continue
        return ""

    def __queue_scan(self, library_id: str) -> bool:
        """把媒体库扫描排进 MediaVault 的后台队列，不阻塞入库流程。"""
        if not library_id:
            return False
        result = self._api.request(f"/libraries/{library_id}/scan-task", method="post")
        return bool(result and result.success)
