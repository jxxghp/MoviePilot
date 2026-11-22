"""
Plex 媒体服务器浏览、详情、三层剧集、音轨字幕轨道与播放进度辅助实现。
"""

from __future__ import annotations

import re
from typing import Any, Callable, Optional
from urllib.parse import urlencode

from app.application.mediaserver import MediaServerIdentityHelper
from app.foundation.url import UrlUtils
from app.runtime.log import logger
from app.schemas.mediaserver import (
    MediaServerBrowseItem,
    MediaServerBrowsePage,
    MediaServerFilterOptions,
    MediaServerItemDetail,
    MediaServerMediaSource,
    MediaServerMediaStream,
    MediaServerPerson,
)
from app.schemas.types import MediaType

_PLEX_SORT_MAP = {
    "date_created": "addedAt",
    "premiere_date": "originallyAvailableAt",
    "rating": "rating",
    "name": "titleSort",
    "random": "random",
}
_PLEX_TYPE_MAP = {
    "movie": "Movie",
    "show": "Series",
    "season": "Season",
    "episode": "Episode",
    "album": "MusicAlbum",
    "track": "Audio",
}
_PLEX_SUBTITLE_ID_RE = re.compile(r"^ms:plex:(\d+)\.([a-z0-9]{2,6})$", re.IGNORECASE)
_PLEX_SUBTITLE_EXT_MAP = {"srt": "srt", "ass": "ass", "ssa": "ass", "vtt": "vtt", "smi": "smi"}


def _plex_image_url(host: str, token: str, path: Optional[str]) -> Optional[str]:
    """
    将 Plex 相对或绝对图片路径规范化为带令牌的内网图片地址。

    :param host: Plex 服务根地址
    :param token: Plex 访问令牌
    :param path: 条目 thumb 或 art 字段
    :return: 可由 MoviePilot 代理签名的图片地址
    """
    if not path or not host or not token:
        return None
    if str(path).startswith(("http://", "https://")):
        return str(path)
    return UrlUtils.combine_url(host=host, path=str(path), query={"X-Plex-Token": token})


def _plex_media_type(plex_type: str) -> str:
    """
    将 Plex 原始类型映射为 MoviePilot 业务媒体类型。

    :param plex_type: Plex 条目 type 字段
    :return: `电影`、`电视剧` 或 `音乐`
    """
    if plex_type in {"show", "season", "episode"}:
        return MediaType.TV.value
    if plex_type in {"artist", "album", "track"}:
        return MediaType.MUSIC.value
    return MediaType.MOVIE.value


def _plex_item_id(item: Any) -> str:
    """
    读取 Plex 条目的稳定标识，优先使用 ratingKey。

    :param item: Plex SDK 条目对象
    :return: 字符串标识
    """
    rating_key = getattr(item, "ratingKey", None)
    if rating_key is not None:
        return str(rating_key)
    return str(getattr(item, "key", "") or "")


def _build_plex_browse_item(host: str, token: str, item: Any) -> Optional[MediaServerBrowseItem]:
    """
    将 Plex SDK 条目转换为统一 `MediaServerBrowseItem`。

    :param host: Plex 服务根地址
    :param token: Plex 访问令牌
    :param item: Plex SDK 条目对象
    :return: 轻量卡片模型；缺少标识或名称时返回 None
    """
    item_id = _plex_item_id(item)
    title = str(getattr(item, "title", "") or "").strip()
    if not item_id or not title:
        return None
    raw_type = str(getattr(item, "type", "") or "movie").casefold()
    norm_type = _PLEX_TYPE_MAP.get(raw_type, "Movie")
    thumb_url = _plex_image_url(host, token, getattr(item, "thumb", None))
    art_url = _plex_image_url(host, token, getattr(item, "art", None))
    parent_thumb = _plex_image_url(host, token, getattr(item, "parentThumb", None))
    grandparent_thumb = _plex_image_url(host, token, getattr(item, "grandparentThumb", None))
    duration_ms = getattr(item, "duration", None)
    view_offset = getattr(item, "viewOffset", None) or 0
    percentage = round(min(100.0, view_offset / duration_ms * 100.0), 1) if duration_ms and view_offset else None
    season_num = getattr(item, "parentIndex", None) if raw_type == "episode" else (
        getattr(item, "index", None) if raw_type == "season" else None
    )
    ep_num = getattr(item, "index", None) if raw_type == "episode" else None
    subtitle: Optional[str] = None
    if raw_type == "episode" and season_num is not None and ep_num is not None:
        subtitle = f"S{int(season_num):02d}E{int(ep_num):02d}"
    elif raw_type == "track":
        parts = [p for p in (getattr(item, "grandparentTitle", None), getattr(item, "parentTitle", None)) if p]
        subtitle = " · ".join(str(p) for p in parts) if parts else None
    elif getattr(item, "year", None):
        subtitle = str(item.year)
    avail = getattr(item, "originallyAvailableAt", None)
    premiere = avail.strftime("%Y-%m-%d") if avail is not None and hasattr(avail, "strftime") else None
    rating = getattr(item, "audienceRating", None) or getattr(item, "rating", None)
    poster = (grandparent_thumb or parent_thumb or thumb_url) if raw_type == "episode" else (thumb_url or grandparent_thumb)
    still = thumb_url if raw_type == "episode" else art_url
    return MediaServerBrowseItem(
        item_id=item_id,
        server_type="plex",
        item_type=norm_type,
        media_type=_plex_media_type(raw_type),
        title=title,
        original_title=getattr(item, "originalTitle", None),
        subtitle=subtitle,
        overview=getattr(item, "summary", None),
        year=getattr(item, "year", None),
        premiere_date=premiere,
        rating=round(float(rating), 1) if isinstance(rating, (int, float)) else None,
        duration_ms=int(duration_ms) if isinstance(duration_ms, (int, float)) and duration_ms > 0 else None,
        poster=poster,
        backdrop=art_url or still,
        thumb=still or art_url,
        series_id=str(getattr(item, "grandparentRatingKey", None) or getattr(item, "parentRatingKey", "") or "") or None
        if raw_type in {"episode", "season"}
        else None,
        series_name=getattr(item, "grandparentTitle", None) if raw_type == "episode" else (
            getattr(item, "parentTitle", None) if raw_type == "season" else None
        ),
        season_id=str(getattr(item, "parentRatingKey", "") or "") or None if raw_type == "episode" else None,
        season_number=int(season_num) if isinstance(season_num, int) else None,
        episode_number=int(ep_num) if isinstance(ep_num, int) else None,
        child_count=getattr(item, "childCount", None) or getattr(item, "leafCount", None),
        played=bool(getattr(item, "isPlayed", False)),
        played_percentage=percentage,
        resume_position_ms=int(view_offset) if view_offset > 0 else None,
    )


def _parse_plex_stream(stream: Any) -> MediaServerMediaStream:
    """
    将 Plex 轨道对象转换为统一 `MediaServerMediaStream`。

    :param stream: Plex SDK 轨道对象
    :return: 统一轨道模型
    """
    raw_type = getattr(stream, "streamType", 0)
    stream_type: Any = {1: "video", 2: "audio", 3: "subtitle"}.get(raw_type, "unknown")
    codec = str(getattr(stream, "codec", "") or "").lower() or None
    is_external = bool(stream_type == "subtitle" and getattr(stream, "key", None))
    subtitle_id: Optional[str] = None
    stream_id = getattr(stream, "id", None)
    if is_external and stream_id is not None:
        ext = _PLEX_SUBTITLE_EXT_MAP.get(codec or "", "srt")
        subtitle_id = f"ms:plex:{stream_id}.{ext}"
    display = getattr(stream, "extendedDisplayTitle", None) or getattr(stream, "displayTitle", None)
    return MediaServerMediaStream(
        index=getattr(stream, "index", None) if isinstance(getattr(stream, "index", None), int) else None,
        type=stream_type,
        codec=codec,
        language=getattr(stream, "languageCode", None) or getattr(stream, "language", None),
        title=getattr(stream, "title", None),
        display_title=str(display) if display else (codec.upper() if codec else "轨道"),
        is_default=bool(getattr(stream, "default", False) or getattr(stream, "selected", False)),
        is_forced=bool(getattr(stream, "forced", False)),
        is_external=is_external,
        subtitle_id=subtitle_id,
        channels=getattr(stream, "channels", None) if isinstance(getattr(stream, "channels", None), int) else None,
        channel_layout=getattr(stream, "audioChannelLayout", None),
        bitrate=getattr(stream, "bitrate", None) if isinstance(getattr(stream, "bitrate", None), int) else None,
        width=getattr(stream, "width", None) if isinstance(getattr(stream, "width", None), int) else None,
        height=getattr(stream, "height", None) if isinstance(getattr(stream, "height", None), int) else None,
    )


def _parse_plex_sources(item: Any) -> list[MediaServerMediaSource]:
    """
    从 Plex 条目的 media/parts 提取多版本及全部音视频和字幕轨道。

    :param item: Plex SDK 媒体对象
    :return: 统一媒体源列表
    """
    sources: list[MediaServerMediaSource] = []
    for media in getattr(item, "media", None) or []:
        parts = getattr(media, "parts", None) or []
        for part in parts:
            part_id = str(getattr(part, "id", "") or "")
            if not part_id:
                continue
            streams = [_parse_plex_stream(s) for s in (getattr(part, "streams", None) or [])]
            res_label = getattr(media, "videoResolution", None)
            container = getattr(part, "container", None) or getattr(media, "container", None)
            name_parts = [str(p).upper() for p in (res_label, container) if p]
            sources.append(
                MediaServerMediaSource(
                    id=part_id,
                    name=" · ".join(name_parts) or f"版本 {part_id}",
                    container=container,
                    size=getattr(part, "size", None) if isinstance(getattr(part, "size", None), int) else None,
                    bitrate=getattr(media, "bitrate", None) if isinstance(getattr(media, "bitrate", None), int) else None,
                    duration_ms=getattr(part, "duration", None) or getattr(media, "duration", None),
                    streams=streams,
                )
            )
    return sources


def plex_browse_items(
    plex: Any,
    host: str,
    token: str,
    *,
    library_id: Optional[str] = None,
    item_type: Optional[str] = None,
    genre: Optional[str] = None,
    year: Optional[int] = None,
    played_filter: Optional[str] = None,
    sort_by: Optional[str] = None,
    sort_order: Optional[str] = None,
    keyword: Optional[str] = None,
    start_index: int = 0,
    limit: int = 30,
) -> Optional[MediaServerBrowsePage]:
    """
    使用 Plex Section 搜索接口实现分页浏览、筛选、排序与关键词搜索。

    :param plex: 已连接的 PlexServer 实例
    :param host: Plex 服务根地址
    :param token: Plex 访问令牌
    :param library_id: 目标媒体库数字标识
    :param item_type: 可选维度过滤
    :param genre: 可选风格过滤
    :param year: 可选年份过滤
    :param played_filter: 观看状态过滤：played / unplayed
    :param sort_by: 规范化排序键
    :param sort_order: 排序方向：asc / desc
    :param keyword: 关键词搜索
    :param start_index: 零基分页偏移
    :param limit: 单页数量上限
    :return: 分页浏览结果；失败时返回 None
    """
    if not plex:
        return None
    safe_start = max(0, int(start_index))
    safe_limit = max(1, min(int(limit), 100))
    sort_key = (sort_by or "date_created").strip().casefold()
    plex_sort = _PLEX_SORT_MAP.get(sort_key, "addedAt")
    default_order = "asc" if sort_key == "name" else "desc"
    order_suffix = "asc" if (sort_order or default_order).casefold() == "asc" else "desc"
    filters: dict[str, Any] = {}
    if genre:
        filters["genre"] = genre
    if year:
        filters["year"] = int(year)
    if played_filter == "unplayed":
        filters["unwatched"] = True
    elif played_filter == "played":
        filters["unwatched"] = False
    try:
        section = plex.library.sectionByID(int(library_id)) if library_id and str(library_id).isdigit() else None
        if section is None:
            sections = plex.library.sections()
            section = sections[0] if sections else None
        if section is None:
            return MediaServerBrowsePage(items=[], total_count=0, start_index=safe_start, limit=safe_limit, has_more=False)
        libtype = section.TYPE
        if item_type:
            mapped = {"movie": "movie", "series": "show", "musicalbum": "album", "audio": "track"}.get(item_type.casefold())
            if mapped:
                libtype = mapped
        results = section.search(
            title=keyword.strip() if keyword and keyword.strip() else None,
            sort=f"{plex_sort}:{order_suffix}",
            libtype=libtype,
            container_start=safe_start,
            container_size=safe_limit,
            maxresults=safe_limit,
            **filters,
        )
        items = [built for raw in results if (built := _build_plex_browse_item(host, token, raw))]
        total = getattr(section, "totalSize", None) if not (filters or keyword) else None
        total_count = int(total) if isinstance(total, int) else (safe_start + len(items) + (1 if len(items) == safe_limit else 0))
        return MediaServerBrowsePage(
            items=items,
            total_count=total_count,
            start_index=safe_start,
            limit=safe_limit,
            has_more=(safe_start + len(items)) < total_count if isinstance(total, int) else len(items) == safe_limit,
        )
    except Exception as err:
        logger.error(f"浏览 Plex 媒体库失败：{err}")
        return None


def plex_filter_options(plex: Any, library_id: Optional[str] = None) -> MediaServerFilterOptions:
    """
    获取 Plex 指定媒体库可用的风格和年份筛选项。

    :param plex: 已连接的 PlexServer 实例
    :param library_id: 媒体库数字标识
    :return: 可用筛选项模型
    """
    genres: list[str] = []
    years: list[int] = []
    try:
        if plex and library_id and str(library_id).isdigit():
            section = plex.library.sectionByID(int(library_id))
            if section:
                genres = [str(choice.title) for choice in section.listFilterChoices("genre") if getattr(choice, "title", None)]
                years = sorted(
                    {int(choice.title) for choice in section.listFilterChoices("year") if str(getattr(choice, "title", "")).isdigit()},
                    reverse=True,
                )
    except Exception as err:
        logger.debug(f"获取 Plex 筛选项失败：{err}")
    return MediaServerFilterOptions(
        genres=genres,
        years=years,
        item_types=["Movie", "Series", "MusicAlbum"],
        sort_options=["date_created", "premiere_date", "rating", "name", "random"],
    )


def plex_item_detail(
    plex: Any,
    host: str,
    token: str,
    item_id: str,
    play_url_builder: Optional[Callable[[str], Optional[str]]] = None,
) -> Optional[MediaServerItemDetail]:
    """
    读取单个 Plex 条目完整详情，含演职员、多版本及音轨字幕轨道。

    :param plex: 已连接的 PlexServer 实例
    :param host: Plex 服务根地址
    :param token: Plex 访问令牌
    :param item_id: Plex 数字标识或 metadata key
    :param play_url_builder: 网页播放链接构造器
    :return: 统一详情模型；失败时返回 None
    """
    if not plex or not item_id:
        return None
    try:
        lookup: Any = int(item_id) if str(item_id).isdigit() else item_id
        item = plex.fetchItem(lookup)
        if hasattr(item, "reload"):
            item.reload()
    except Exception as err:
        logger.error(f"获取 Plex 详情 {item_id} 失败：{err}")
        return None
    base = _build_plex_browse_item(host, token, item)
    if not base:
        return None
    guids = _extract_plex_guids(getattr(item, "guids", None) or [])
    media_source, media_id = MediaServerIdentityHelper.from_provider_ids(guids)
    people: list[MediaServerPerson] = []
    for role in (getattr(item, "roles", None) or [])[:20]:
        if getattr(role, "tag", None):
            people.append(
                MediaServerPerson(
                    id=str(getattr(role, "id", "") or "") or None,
                    name=str(role.tag),
                    role=getattr(role, "role", None),
                    type="Actor",
                    image=_plex_image_url(host, token, getattr(role, "thumb", None)),
                )
            )
    for director in (getattr(item, "directors", None) or [])[:4]:
        if getattr(director, "tag", None):
            people.append(MediaServerPerson(name=str(director.tag), role="导演", type="Director"))
    sources = _parse_plex_sources(item)
    genres = [str(g.tag) for g in (getattr(item, "genres", None) or []) if getattr(g, "tag", None)]
    studio = getattr(item, "studio", None)
    return MediaServerItemDetail(
        **base.model_dump(),
        tagline=getattr(item, "tagline", None),
        official_rating=getattr(item, "contentRating", None),
        genres=genres,
        studios=[str(studio)] if studio else [],
        people=people,
        media_source=media_source,
        media_id=media_id,
        imdb_id=guids.get("imdb_id"),
        tmdb_id=guids.get("tmdb_id"),
        tvdb_id=guids.get("tvdb_id"),
        parent_id=str(getattr(item, "parentRatingKey", "") or "") or None,
        season_name=getattr(item, "parentTitle", None) if base.item_type == "Episode" else None,
        season_count=getattr(item, "childCount", None) if base.item_type == "Series" else None,
        episode_count=getattr(item, "leafCount", None) if base.item_type in {"Series", "Season"} else None,
        media_sources=sources,
        play_url=play_url_builder(str(getattr(item, "key", "") or item_id)) if play_url_builder else None,
        can_play=base.item_type in {"Movie", "Episode", "Audio"} or bool(sources),
    )


def _extract_plex_guids(guids: list[Any]) -> dict[str, Optional[str]]:
    """
    从 Plex Guid 列表中解析 imdb/tmdb/tvdb 标识。

    :param guids: Plex Guid 对象列表
    :return: 标识字典
    """
    result: dict[str, Optional[str]] = {}
    for guid in guids:
        raw = str(guid.get("id") if isinstance(guid, dict) else getattr(guid, "id", "") or "")
        for prefix, key in (("imdb://", "imdb_id"), ("tmdb://", "tmdb_id"), ("tvdb://", "tvdb_id")):
            if raw.startswith(prefix):
                result[key] = raw[len(prefix) :]
    return result


def plex_seasons(plex: Any, host: str, token: str, series_id: str) -> Optional[list[MediaServerBrowseItem]]:
    """
    获取 Plex 电视剧的全部季卡片。

    :param plex: PlexServer 实例
    :param host: 服务根地址
    :param token: 访问令牌
    :param series_id: 剧集标识
    :return: 按季号排序的季列表
    """
    if not plex or not series_id:
        return None
    try:
        show = plex.fetchItem(int(series_id) if str(series_id).isdigit() else series_id)
        seasons = [built for s in show.seasons() if (built := _build_plex_browse_item(host, token, s))]
        seasons.sort(key=lambda item: (item.season_number is None, item.season_number or 0))
        return seasons
    except Exception as err:
        logger.error(f"获取 Plex 剧集 {series_id} 季列表失败：{err}")
        return None


def plex_season_episodes(
    plex: Any,
    host: str,
    token: str,
    series_id: str,
    *,
    season_id: Optional[str] = None,
    season: Optional[int] = None,
) -> Optional[list[MediaServerBrowseItem]]:
    """
    获取 Plex 指定季的全部单集详情卡片。

    :param plex: PlexServer 实例
    :param host: 服务根地址
    :param token: 访问令牌
    :param series_id: 剧集标识
    :param season_id: 可选季标识
    :param season: 可选季号
    :return: 按集号排序的单集卡片列表
    """
    if not plex:
        return None
    try:
        if season_id:
            container = plex.fetchItem(int(season_id) if str(season_id).isdigit() else season_id)
            raw_eps = container.episodes()
        else:
            show = plex.fetchItem(int(series_id) if str(series_id).isdigit() else series_id)
            raw_eps = show.season(season).episodes() if season is not None else show.episodes()
        episodes = [built for ep in raw_eps if (built := _build_plex_browse_item(host, token, ep))]
        episodes.sort(key=lambda item: (item.season_number or 0, item.episode_number or 0))
        return episodes
    except Exception as err:
        logger.error(f"获取 Plex 剧集 {series_id} 分集失败：{err}")
        return None


def plex_subtitle_url(host: str, token: str, subtitle_id: str) -> Optional[str]:
    """
    将 Plex 外挂字幕代理标识 `ms:plex:<stream_id>.<ext>` 解析为原始字幕下载流地址。

    :param host: 服务根地址
    :param token: 访问令牌
    :param subtitle_id: 代理字幕标识
    :return: 上游字幕流地址；格式不符时返回 None
    """
    if not host or not token:
        return None
    match = _PLEX_SUBTITLE_ID_RE.match(subtitle_id.strip())
    if not match:
        return None
    stream_id, _ = match.groups()
    return f"{host.rstrip('/')}/library/streams/{stream_id}?{urlencode({'X-Plex-Token': token})}"


def plex_report_progress(
    plex: Any,
    item_id: str,
    position_ms: int,
    *,
    duration_ms: Optional[int] = None,
    is_paused: bool = False,
    state: str = "playing",
) -> bool:
    """
    将客户端播放进度回传给 Plex 服务器。

    :param plex: PlexServer 实例
    :param item_id: 条目标识
    :param position_ms: 当前播放位置（毫秒）
    :param duration_ms: 可选总时长（毫秒）
    :param is_paused: 是否暂停
    :param state: 事件状态
    :return: 回传成功返回 True
    """
    if not plex or not item_id:
        return False
    try:
        item = plex.fetchItem(int(item_id) if str(item_id).isdigit() else item_id)
        plex_state = "stopped" if state == "stopped" else ("paused" if (is_paused or state == "paused") else "playing")
        item.updateTimeline(max(0, int(position_ms)), state=plex_state, duration=duration_ms or getattr(item, "duration", None))
        return True
    except Exception as err:
        logger.debug(f"回传 Plex 播放进度失败：{err}")
        return False
