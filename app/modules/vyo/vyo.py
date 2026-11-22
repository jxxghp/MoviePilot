"""
Vyo 媒体服务器客户端，负责分页检索、媒体身份、播放列表和扫描排队。
"""

from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Tuple, Union

from app.application.mediaserver import MediaServerIdentityHelper
from app.foundation.url import UrlUtils
from app.modules.vyo.api import Api
from app.runtime.log import logger
from app.schemas.dashboard import Statistic as _SchemaStatistic
from app.schemas.mediaserver import (
    MediaServerBrowseItem as _SchemaMediaServerBrowseItem,
)
from app.schemas.mediaserver import (
    MediaServerBrowsePage as _SchemaMediaServerBrowsePage,
)
from app.schemas.mediaserver import (
    MediaServerFilterOptions as _SchemaMediaServerFilterOptions,
)
from app.schemas.mediaserver import MediaServerItem as _SchemaMediaServerItem
from app.schemas.mediaserver import (
    MediaServerItemDetail as _SchemaMediaServerItemDetail,
)
from app.schemas.mediaserver import (
    MediaServerMediaSource as _SchemaMediaServerMediaSource,
)
from app.schemas.mediaserver import MediaServerItemUserState as _SchemaMediaServerItemUserState
from app.schemas.mediaserver import MediaServerLibrary as _SchemaMediaServerLibrary
from app.schemas.mediaserver import MediaServerPlayItem as _SchemaMediaServerPlayItem
from app.schemas.mediaserver import RefreshMediaItem as _SchemaRefreshMediaItem
from app.schemas.types import MediaSource, MediaType


class _SearchInterrupted(Exception):
    """
    关键字翻页中途请求失败。

    与「查完了」区分开：调用方据此返回 None（服务不可达），而不是空结果
    （确定不在库中）——后者会让上层误判成需要重新下载。
    """


class Vyo:
    """
    Vyo 媒体服务客户端。

    只使用 Muvyo 管理端口的原生接口，凭据是 Muvyo 管理面板的长效 API Key；
    Vyo 没有音乐库，也不主动外发 Webhook，相关能力在模块层显式留空。
    """

    # Vyo 单次列表请求的最大条数，与服务端 page_size 上限一致
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
        """
        保存服务配置并通过媒体库列表探测 API 密钥是否有效。

        :param host: Vyo 管理端地址
        :param apikey: 管理面板的长期 API 密钥
        :param play_host: 生成网页或远程图片链接的地址，省略时使用管理端地址
        :param sync_libraries: 同步媒体库标识，空列表或包含 all 表示不限制
        :param kwargs: 通用服务配置的额外参数，当前不读取
        """
        self._host = UrlUtils.standardize_base_url(host).rstrip("/") if host else None
        self._playhost = (
            UrlUtils.standardize_base_url(play_host).rstrip("/") if play_host else None
        )
        self._apikey = apikey
        self._sync_libraries = sync_libraries or []
        self._api = Api(host=host, apikey=apikey)
        self._active = False
        if not self.is_configured():
            logger.error("Vyo 配置不完整！")
            return
        self.reconnect()


    def is_configured(self) -> bool:
        """
        配置是否完整到可以发起请求。
        """
        return bool(self._host and self._apikey)

    def is_authenticated(self) -> bool:
        """
        当前 API Key 是否已探测通过。
        """
        return self._active

    def is_inactive(self) -> bool:
        """
        是否需要重连。
        """
        if not self.is_configured():
            return False
        return not self._active

    def reconnect(self) -> bool:
        """
        用媒体库列表接口探测连通性与凭据有效性。
        """
        if not self.is_configured():
            return False
        result = self._api.request("/libraries", suppress_log=True)
        self._active = bool(result and result.success)
        return self._active

    def disconnect(self) -> None:
        """
        释放底层会话。
        """
        self._active = False
        self._api.close()

    def authenticate(self, username: str, password: str) -> Optional[str]:
        """
        通过 Vyo 账号认证接口获取访问令牌。

        :param username: 用户账号
        :param password: 用户密码
        :return: 认证成功的访问令牌；配置、凭据不完整或响应无有效令牌时返回 None，不更新管理 API 密钥
        """
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


    def get_librarys(
        self, hidden: Optional[bool] = False
    ) -> Optional[List[_SchemaMediaServerLibrary]]:
        """
        查询媒体库并按需筛选同步范围。

        :param hidden: 是否隐藏未选入同步范围的库，默认不筛选
        :return: 统一媒体库列表；请求失败时返回 None
        """
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
        """
        媒体库原始记录，连接失败返回 None。
        """
        result = self._api.request("/libraries")
        if not result or not result.success or not isinstance(result.data, dict):
            return None
        items = result.data.get("items")
        return items if isinstance(items, list) else []

    def get_items_count(self, parent: Union[str, int]) -> Optional[int]:
        """
        查询媒体库内电影和电视剧的总数。

        :param parent: 媒体库标识
        :return: 服务端 total 计数；标识为空、请求失败或缺少计数时返回 None
        """
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
        """
        使用固定页大小遍历媒体库中的电影和电视剧。

        按 100 条分页将偏移换算为页号，并在本地丢弃页内偏移。
        配置不完整或读取失败时抛出异常；无法转换的条目仍占用数量配额。

        :param parent: 媒体库标识
        :param start_index: 起始条目偏移，空值或负数按 0 处理
        :param limit: 最多检查的原始条目数；None 或 -1 不限，其他非正值不产生条目
        :yields: 转换成功的统一媒体条目
        :raises ConnectionError: 配置不完整、分页失败或列表响应结构错误
        """
        if not parent or not self.is_configured():
            raise ConnectionError("Vyo 服务配置不完整或媒体库标识为空")
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
                raise ConnectionError("读取 Vyo 媒体库失败")
            rows = result.get("items")
            if not isinstance(rows, list):
                raise ConnectionError("Vyo 媒体库列表响应格式错误")
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
        """
        调用条目列表接口；Vyo 只接受页码，偏移量由调用方换算。
        """
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
        """
        按关键字翻页产出条目原始记录。

        Vyo 的关键字查询是模糊匹配，命中数可能超过单页上限；只读第一页会让
        存在性判断把排在后面的目标误判成「未入库」，因此这里翻页直到取完。
        连接失败返回 None，与「查得到但没有」区分开。
        """
        first = self.__query_items(keyword=keyword, kinds=kinds, page=1, page_size=self.PAGE_LIMIT)
        if first is None:
            return None

        def rows() -> Generator[Dict[str, Any], Any, None]:
            """
            从已获取的首页继续逐页产出搜索结果，后续页失败时抛出中断信号。

            :yields: 搜索响应中的原始媒体条目
            :raises _SearchInterrupted: 后续分页请求失败，调用方不可将已有部分结果视为完整查询
            """
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


    def get_iteminfo(self, itemid: str) -> Optional[_SchemaMediaServerItem]:
        """
        读取单个媒体条目并转换为统一模型。

        :param itemid: Vyo 条目标识
        :return: 媒体详情；配置不完整、标识为空、请求或转换失败时返回 None
        """
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
        """
        完整搜索电影候选，并按标题、年份和来源身份过滤。

        只有完整读取全部搜索页后才返回匹配项，避免把部分结果误判成未入库。

        :param title: 必须精确匹配的标题
        :param year: 可选年份，空值不限制
        :param media_source: 用于排除身份冲突的媒体来源
        :param media_id: 对应来源内的标识
        :return: 匹配的电影列表，无匹配时为空；配置不完整或任意搜索页失败时返回 None
        """
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

    def _build_browse_item(self, row: Dict[str, Any]) -> Optional[_SchemaMediaServerBrowseItem]:
        """将 Vyo 条目行转换为统一浏览卡片模型。"""
        row_id = str(row.get("id") or "")
        title = str(row.get("title") or "").strip()
        if not row_id or not title:
            return None
        kind = str(row.get("kind") or "Movie")
        is_tv = kind in {"Series", "Season", "Episode"}
        user_data = row.get("user_data") or {}
        duration_ticks = row.get("duration_ticks") or 0
        position_ticks = user_data.get("position_ticks") or 0
        duration_ms = int(duration_ticks // 10_000) if duration_ticks > 0 else None
        resume_ms = int(position_ticks // 10_000) if position_ticks > 0 else None
        percentage = round(min(100.0, position_ticks / duration_ticks * 100.0), 1) if duration_ticks and position_ticks else None
        season_num = row.get("season") if isinstance(row.get("season"), int) else None
        ep_num = row.get("episode") if isinstance(row.get("episode"), int) else None
        subtitle = (
            f"S{season_num:02d}E{ep_num:02d}"
            if kind == "Episode" and season_num is not None and ep_num is not None
            else (str(row["year"]) if row.get("year") else None)
        )
        metadata = row.get("metadata_info") or {}
        return _SchemaMediaServerBrowseItem(
            item_id=row_id,
            server_type="mediavault",
            item_type=kind,
            media_type=MediaType.TV.value if is_tv else MediaType.MOVIE.value,
            title=title,
            original_title=metadata.get("original_title"),
            subtitle=subtitle,
            overview=metadata.get("overview") or row.get("overview"),
            year=row.get("year"),
            rating=float(metadata["rating"]) if isinstance(metadata.get("rating"), (int, float)) else None,
            duration_ms=duration_ms,
            poster=self._api.image_url(str(row.get("series_id") or row_id), "primary") if kind == "Episode" else self._api.image_url(row_id, "primary"),
            backdrop=self._api.image_url(row_id, "backdrop") if row.get("has_backdrop") else None,
            thumb=self._api.image_url(row_id, "primary"),
            series_id=str(row["series_id"]) if row.get("series_id") else None,
            series_name=row.get("series_name"),
            season_id=str(row["season_id"]) if row.get("season_id") else None,
            season_number=season_num,
            episode_number=ep_num,
            played=user_data.get("played") if "played" in user_data else None,
            played_percentage=percentage,
            resume_position_ms=resume_ms,
        )

    def browse_items(
        self,
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
    ) -> Optional[_SchemaMediaServerBrowsePage]:
        """分页浏览 Vyo 媒体库条目。"""
        _ = genre, year, username
        if not self.is_configured():
            return None
        safe_start = max(0, int(start_index))
        safe_limit = max(1, min(int(limit), self.PAGE_LIMIT))
        page = (safe_start // safe_limit) + 1
        sort_map = {"date_created": "added", "premiere_date": "year", "rating": "rating", "name": "title"}
        kinds = {"movie": "Movie", "series": "Series", "电影": "Movie", "电视剧": "Series"}.get(
            str(item_type or "").casefold(), "Movie,Series"
        )
        result = self.__query_items(
            library_id="" if parent_id else (library_id or ""),
            parent_id=parent_id or "",
            keyword=keyword.strip() if keyword and keyword.strip() else "",
            kinds=kinds,
            filter_by=played_filter or "",
            sort_by=sort_map.get((sort_by or "date_created").casefold(), "added"),
            sort_order=(sort_order or "desc").casefold(),
            page=page,
            page_size=safe_limit,
        )
        if result is None:
            return None
        items = [built for row in (result.get("items") or []) if isinstance(row, dict) and (built := self._build_browse_item(row))]
        total = int(result.get("total") or (safe_start + len(items)))
        return _SchemaMediaServerBrowsePage(
            items=items,
            total_count=total,
            start_index=safe_start,
            limit=safe_limit,
            has_more=(safe_start + len(items)) < total,
        )

    def get_filter_options(
        self, library_id: Optional[str] = None, username: Optional[str] = None
    ) -> _SchemaMediaServerFilterOptions:
        """返回 Vyo 支持的筛选与排序选项。"""
        _ = library_id, username
        return _SchemaMediaServerFilterOptions(
            genres=[],
            years=[],
            item_types=["Movie", "Series"],
            sort_options=["date_created", "premiere_date", "rating", "name"],
        )

    def get_item_detail(
        self, item_id: str, username: Optional[str] = None
    ) -> Optional[_SchemaMediaServerItemDetail]:
        """获取 Vyo 单个条目完整详情。"""
        _ = username
        if not item_id or not self.is_configured():
            return None
        result = self._api.request(f"/items/{item_id}", suppress_log=True)
        if not result or not result.success or not isinstance(result.data, dict):
            return None
        row = result.data
        base = self._build_browse_item(row)
        if not base:
            return None
        metadata = row.get("metadata_info") or {}
        ext_ids = metadata.get("external_ids") or {}
        provider_ids: dict[str, Any] = {}
        if row.get("tmdb_id"):
            provider_ids["Tmdb"] = str(row["tmdb_id"])
        if ext_ids.get("imdb_id"):
            provider_ids["Imdb"] = str(ext_ids["imdb_id"])
        if ext_ids.get("tvdb_id"):
            provider_ids["Tvdb"] = str(ext_ids["tvdb_id"])
        media_source, media_id = MediaServerIdentityHelper.from_provider_ids(provider_ids)
        sources = [
            _SchemaMediaServerMediaSource(
                id=str(src.get("id") or idx),
                name=src.get("name") or Path(str(src.get("path") or "")).name or None,
                size=src.get("size") if isinstance(src.get("size"), int) else None,
            )
            for idx, src in enumerate(row.get("sources") or [])
            if isinstance(src, dict)
        ]
        return _SchemaMediaServerItemDetail(
            **base.model_dump(),
            genres=[str(g) for g in (metadata.get("genres") or []) if g],
            media_source=media_source,
            media_id=media_id,
            tmdb_id=str(row["tmdb_id"]) if row.get("tmdb_id") else None,
            imdb_id=str(ext_ids["imdb_id"]) if ext_ids.get("imdb_id") else None,
            tvdb_id=str(ext_ids["tvdb_id"]) if ext_ids.get("tvdb_id") else None,
            parent_id=str(row["parent_id"]) if row.get("parent_id") else None,
            media_sources=sources,
            play_url=self.get_play_url(base.item_id),
            can_play=base.item_type in {"Movie", "Episode"},
        )

    def get_seasons(
        self, series_id: str, username: Optional[str] = None
    ) -> Optional[List[_SchemaMediaServerBrowseItem]]:
        """获取 Vyo 电视剧的全部季卡片。"""
        _ = username
        if not series_id or not self.is_configured():
            return None
        result = self.__query_items(parent_id=series_id, kinds="Season", page=1, page_size=self.PAGE_LIMIT)
        if result is None:
            return None
        seasons = [built for row in (result.get("items") or []) if isinstance(row, dict) and (built := self._build_browse_item(row))]
        seasons.sort(key=lambda item: (item.season_number is None, item.season_number or 0))
        return seasons

    def get_season_episodes(
        self,
        series_id: str,
        *,
        season_id: Optional[str] = None,
        season: Optional[int] = None,
        username: Optional[str] = None,
    ) -> Optional[List[_SchemaMediaServerBrowseItem]]:
        """获取 Vyo 指定季的全部单集卡片。"""
        _ = username
        if not self.is_configured():
            return None
        target_parent = season_id or (self.__find_season_id(series_id, season) if season is not None else "")
        if not target_parent:
            return []
        result = self.__query_items(parent_id=target_parent, kinds="Episode", page=1, page_size=self.PAGE_LIMIT)
        if result is None:
            return None
        episodes = [built for row in (result.get("items") or []) if isinstance(row, dict) and (built := self._build_browse_item(row))]
        episodes.sort(key=lambda item: (item.season_number or 0, item.episode_number or 0))
        return episodes

    def search_library(
        self,
        keyword: str,
        types: list[str],
        limit: int,
        library_id: Optional[str] = None,
    ) -> List[_SchemaMediaServerPlayItem]:
        """调用 Vyo 搜索接口返回电影与电视剧。"""
        _ = library_id
        kinds_list = []
        if MediaType.MOVIE.value in types:
            kinds_list.append("Movie")
        if MediaType.TV.value in types:
            kinds_list.append("Series")
        if not kinds_list or not keyword.strip() or not self.is_configured():
            return []
        rows = self.__search_rows(keyword=keyword.strip(), kinds=",".join(kinds_list))
        if rows is None:
            return []
        results: List[_SchemaMediaServerPlayItem] = []
        try:
            for row in rows:
                item = self.__format_item_info(row)
                if not item or not item.item_id or not item.title or item.item_type not in kinds_list:
                    continue
                results.append(
                    _SchemaMediaServerPlayItem(
                        id=item.item_id,
                        item_id=item.item_id,
                        title=item.title,
                        subtitle=str(item.year) if item.year else None,
                        year=item.year,
                        type=MediaType.TV.value if item.item_type == "Series" else MediaType.MOVIE.value,
                        image=self._api.image_url(item.item_id, "primary"),
                        server_type="vyo",
                    )
                )
                if len(results) >= max(1, limit):
                    break
        except _SearchInterrupted:
            return []
        return results

    def get_tv_episodes(
        self,
        item_id: Optional[str] = None,
        title: Optional[str] = None,
        year: Optional[str] = None,
        media_source: Optional[MediaSource] = None,
        media_id: Optional[str] = None,
        season: Optional[int] = None,
    ) -> Tuple[Optional[str], Optional[Dict[int, List[int]]]]:
        """
        定位电视剧并返回各季已入库的集号。

        :param item_id: 优先使用的缓存条目标识，失效或身份冲突时回退标题搜索
        :param title: 回退搜索使用的剧名
        :param year: 可选年份，空值不限制
        :param media_source: 目标媒体来源
        :param media_id: 来源内的目标标识
        :param season: 指定季号，None 表示全部季
        :return: 电视剧标识和排序去重后的季集映射；请求失败返回两项 None，确定未找到时返回 None 和空映射
        """
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
        """
        按标题定位剧集条目 ID；连接失败返回 None，未找到返回空串。
        """
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
        """
        先定位季条目，再分页读取分集条目标识。

        :param item_id: 电视剧条目标识
        :param season: 目标季号
        :return: 集号到条目标识的映射；配置或输入不完整、季未找到时为空，中途请求失败时保留已收集部分
        """
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
        """
        在剧集下定位指定季的条目 ID。
        """
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
        """
        把 Vyo 条目转换为统一媒体服务器模型。
        """
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
            logger.error(f"解析 Vyo 条目失败：{err}")
            return None

    @staticmethod
    def __item_path(row: Dict[str, Any]) -> Optional[str]:
        """
        条目详情带媒体源时取第一个文件路径，列表接口没有路径字段。
        """
        sources = row.get("sources")
        if isinstance(sources, list):
            for source in sources:
                if isinstance(source, dict) and source.get("path"):
                    return str(source["path"])
        return None

    @staticmethod
    def __local_time(value: Optional[str]) -> Optional[str]:
        """
        将时间文本去掉小数秒，并把日期时间分隔符改为空格。

        :param value: 服务端时间文本
        :return: 规整后的文本；空值返回 None；本方法不解析或转换时区
        """
        if not value:
            return None
        return str(value).split(".")[0].replace("T", " ")


    def get_medias_count(self) -> Optional[_SchemaStatistic]:
        """
        查询电影、电视剧和分集数量统计。

        :return: 统一统计对象；请求失败或业务数据结构不符时返回 None
        """
        result = self._api.request("/statistics")
        if not result or not result.success or not isinstance(result.data, dict):
            return None
        return _SchemaStatistic(
            movie_count=result.data.get("movie_count") or 0,
            tv_count=result.data.get("series_count") or 0,
            episode_count=result.data.get("episode_count") or 0,
        )

    def get_user_count(self) -> int:
        """
        查询媒体库接口可见的用户数。

        :return: 用户列表长度；请求失败或返回值不是列表时返回 0
        """
        result = self._api.request("/users", suppress_log=True)
        if not result or not result.success or not isinstance(result.data, list):
            return 0
        return len(result.data)

    def get_resume(self, num: Optional[int] = 12) -> Optional[List[_SchemaMediaServerPlayItem]]:
        """
        查询用户可继续观看的电影和分集。

        :param num: 目标数量，空值或 0 使用 20，最终限制在 1 至 100 条
        :return: 统一播放列表；配置不完整或查询失败时返回 None
        """
        return self.__play_items(filter_by="resume", num=num, resume=True)

    def get_latest(self, num: Optional[int] = 20) -> Optional[List[_SchemaMediaServerPlayItem]]:
        """
        按入库时间降序查询最新电影和电视剧。

        :param num: 目标数量，空值或 0 使用 20，最终限制在 1 至 100 条
        :return: 统一播放列表；配置不完整或查询失败时返回 None
        """
        return self.__play_items(sort_by="added", sort_order="desc", num=num, resume=False)

    def __play_items(
        self,
        num: Optional[int],
        resume: bool,
        filter_by: str = "",
        sort_by: str = "",
        sort_order: str = "",
    ) -> Optional[List[_SchemaMediaServerPlayItem]]:
        """
        把条目列表转换为可播放展示项。
        """
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
        """
        从最新 100 个电影和电视剧中筛选有背景图的条目。

        :param num: 目标图片数量，空值或 0 使用 20，限制在 1 至 100
        :param remote: 是否优先使用外网播放地址作为图片根地址
        :return: 不含管理密钥的图片 URL 列表；配置不完整或请求失败时返回 None
        """
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
        """
        构造 Vyo 媒体库网页中的条目链接。

        :param item_id: 媒体条目标识
        :return: 网页 URL；标识为空或配置不完整时返回 None，不验证条目是否存在
        """
        if not item_id or not self.is_configured():
            return None
        return f"{self._playhost or self._host}/library/item/{item_id}"


    def refresh_root_library(self) -> Optional[bool]:
        """
        把所有媒体库扫描请求加入后台队列。

        :return: 全部入队成功时返回 True；无可扫描媒体库或任一入队失败时返回 False，库列表请求失败时返回 None
        """
        rows = self.__library_rows()
        if rows is None:
            return None
        results = [self.__queue_scan(str(row.get("id"))) for row in rows if row.get("id")]
        return all(results) if results else False

    def refresh_library_by_items(
        self, items: List[_SchemaRefreshMediaItem]
    ) -> Optional[bool]:
        """
        按目标路径确定媒体库并排队扫描，无法定位时退回全部媒体库。

        定位成功的媒体库去重后逐个发送请求，个别请求失败不会阻止其余媒体库入队。

        :param items: 包含目标路径的媒体刷新请求列表
        :return: 全部扫描入队结果；空输入返回 False，库列表查询失败返回 None
        """
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
                logger.info(f"Vyo 中未找到 {item.title} 对应的媒体库，将扫描全部媒体库")
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
        """
        按目录归属把入库路径映射到媒体库。
        """
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
        """
        把媒体库扫描排进 Vyo 的后台队列，不阻塞入库流程。
        """
        if not library_id:
            return False
        result = self._api.request(f"/libraries/{library_id}/scan-task", method="post")
        return bool(result and result.success)
