"""
Emby 系媒体服务器（Emby / Jellyfin / 极影视 / 绿联媒体库服务）共用的浏览、详情、三层剧集与播放辅助。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

from app.adapters.network.http import RequestUtils
from app.application.mediaserver import MediaServerIdentityHelper
from app.runtime.log import logger
from app.schemas.mediaserver import (
    MediaServerBrowseItem,
    MediaServerBrowsePage,
    MediaServerFilterOptions,
    MediaServerItemDetail,
    MediaServerMediaSource,
    MediaServerMediaStream,
    MediaServerPerson,
    MediaServerPlayItem,
)
from app.schemas.types import MediaType

_TICKS_PER_MS = 10_000
_BROWSE_FIELDS = (
    "Overview,ProductionYear,PremiereDate,CommunityRating,OfficialRating,"
    "RunTimeTicks,ChildCount,RecursiveItemCount,UserData,ImageTags,"
    "BackdropImageTags,ParentBackdropImageTags,ParentBackdropItemId,"
    "SeriesName,SeriesId,SeasonId,SeasonName,ParentIndexNumber,IndexNumber,"
    "OriginalTitle,Genres,MediaSources"
)
_SORT_BY_MAP = {
    "date_created": "DateCreated,SortName",
    "premiere_date": "PremiereDate,ProductionYear,SortName",
    "rating": "CommunityRating,SortName",
    "name": "SortName",
    "random": "Random",
}
_DEFAULT_SORT_OPTIONS = ["date_created", "premiere_date", "rating", "name", "random"]
_SUBTITLE_EXT_MAP = {
    "subrip": "srt",
    "srt": "srt",
    "ass": "ass",
    "ssa": "ass",
    "webvtt": "vtt",
    "vtt": "vtt",
    "smi": "smi",
    "sami": "smi",
    "sub": "sub",
}
_SUBTITLE_ID_RE = re.compile(r"^ms:([^:]+):(\d+)\.([a-z0-9]{2,6})$", re.IGNORECASE)
_ITEM_TYPE_MAP = {
    "movie": "Movie",
    "电影": "Movie",
    "series": "Series",
    "tv": "Series",
    "电视剧": "Series",
    "season": "Season",
    "episode": "Episode",
    "boxset": "BoxSet",
    "collection": "BoxSet",
    "合集": "BoxSet",
    "musicalbum": "MusicAlbum",
    "album": "MusicAlbum",
    "audio": "Audio",
    "music": "MusicAlbum,Audio",
    "音乐": "MusicAlbum,Audio",
}


@dataclass(frozen=True)
class EmbyBrowseConfig:
    """
    封装 Emby 系客户端的协议差异配置，供共用浏览与详情函数调用。

    :param host: 服务端根地址（以 `/` 结尾）
    :param api_key: API 密钥或访问令牌
    :param user_id: 当前查询的用户标识
    :param server_type: 媒体服务器类型标识（如 emby、jellyfin、zspace、ugreen）
    :param api_prefix: 接口路径前缀，Emby/ZSpace 为 `"emby/"`，Jellyfin/Ugreen 为 `""`
    :param play_host: 可选外网播放地址
    :param server_id: 可选服务器标识
    :param request_factory: 构造带鉴权请求头的 `RequestUtils` 工厂函数
    :param include_api_key_param: 是否在查询参数中附加 `api_key`
    :param image_api_key: 是否在图片 URL 中附加 `api_key` 参数
    :param play_url_builder: 可选自定义网页播放地址构造器
    """

    host: str
    api_key: str
    user_id: str
    server_type: str
    api_prefix: str = "emby/"
    play_host: Optional[str] = None
    server_id: Optional[str] = None
    request_factory: Callable[[], RequestUtils] = RequestUtils
    include_api_key_param: bool = True
    image_api_key: bool = True
    play_url_builder: Optional[Callable[[str], Optional[str]]] = None


def _ticks_to_ms(value: Any) -> Optional[int]:
    """
    将 Emby 的 100 纳秒时间刻度转换为毫秒。

    :param value: 服务端原始刻度值
    :return: 非负毫秒整数，无效时返回 None
    """
    if isinstance(value, (int, float)) and value > 0:
        return int(value // _TICKS_PER_MS)
    return None


def _api_params(cfg: EmbyBrowseConfig, extra: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """
    组装 Emby 系请求参数并按配置附加 `api_key`。

    :param cfg: Emby 系协议配置
    :param extra: 业务查询参数
    :return: 过滤空值后的查询字典
    """
    params: dict[str, Any] = {}
    if cfg.include_api_key_param and cfg.api_key:
        params["api_key"] = cfg.api_key
    for key, val in (extra or {}).items():
        if val is not None and val != "":
            params[key] = val
    return params


def _image_url(
    cfg: EmbyBrowseConfig,
    item_id: Optional[str],
    image_type: str = "Primary",
    tag: Optional[str] = None,
    max_width: Optional[int] = None,
) -> Optional[str]:
    """
    构造 Emby 系条目的图片内网地址（后续由 Chain 层统一签名）。

    :param cfg: Emby 系协议配置
    :param item_id: 条目或演职员标识
    :param image_type: 图片类型，例如 Primary、Backdrop、Thumb
    :param tag: 可选图片版本标签
    :param max_width: 可选最大宽度像素，控制移动端流量
    :return: 完整图片地址，缺少标识时返回 None
    """
    if not cfg.host or not item_id:
        return None
    query: dict[str, Any] = {}
    if tag:
        query["tag"] = tag
    if max_width:
        query["maxWidth"] = max_width
    if cfg.image_api_key and cfg.api_key:
        query["api_key"] = cfg.api_key
    base = f"{cfg.host}Items/{quote(str(item_id), safe='')}/Images/{image_type}"
    return f"{base}?{urlencode(query)}" if query else base


def _resolve_images(cfg: EmbyBrowseConfig, raw: Mapping[str, Any]) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """
    为任意维度条目（电影/剧集/季/集/音乐）解析最合适的 poster、backdrop 与 thumb。

    :param cfg: Emby 系协议配置
    :param raw: 服务端原始条目字典
    :return: `(poster, backdrop, thumb)` 三元组
    """
    item_id = str(raw.get("Id") or "")
    item_type = str(raw.get("Type") or "")
    raw_tags = raw.get("ImageTags")
    image_tags: Mapping[str, Any] = raw_tags if isinstance(raw_tags, Mapping) else {}
    primary_tag = image_tags.get("Primary")
    thumb_tag = image_tags.get("Thumb")
    backdrop_tags = raw.get("BackdropImageTags") or []
    parent_backdrop_tags = raw.get("ParentBackdropImageTags") or []
    parent_backdrop_id = raw.get("ParentBackdropItemId") or raw.get("SeriesId")

    primary_url = _image_url(cfg, item_id, "Primary", primary_tag, max_width=600) if primary_tag else None
    thumb_url = _image_url(cfg, item_id, "Thumb", thumb_tag, max_width=800) if thumb_tag else None
    backdrop_url: Optional[str] = None
    if backdrop_tags:
        backdrop_url = _image_url(cfg, item_id, "Backdrop", str(backdrop_tags[0]), max_width=1280)
    elif parent_backdrop_id and parent_backdrop_tags:
        backdrop_url = _image_url(cfg, str(parent_backdrop_id), "Backdrop", str(parent_backdrop_tags[0]), max_width=1280)

    series_id = str(raw.get("SeriesId") or "")
    series_tag = raw.get("SeriesPrimaryImageTag")
    series_poster = _image_url(cfg, series_id, "Primary", series_tag, max_width=600) if series_id else None

    if item_type == "Episode":
        still = primary_url or thumb_url or backdrop_url
        return series_poster or still, backdrop_url or still, still
    if item_type == "Season":
        return primary_url or series_poster, backdrop_url, thumb_url or backdrop_url
    return primary_url or thumb_url, backdrop_url, thumb_url or backdrop_url


def _media_type_for_item(item_type: str) -> str:
    """
    将 Emby 条目维度映射到 MoviePilot 业务媒体类型。

    :param item_type: Emby 原始 Type 字段
    :return: `电影`、`电视剧`、`音乐` 或 `合集`
    """
    lowered = item_type.casefold()
    if lowered in {"series", "season", "episode"}:
        return MediaType.TV.value
    if lowered in {"audio", "musicalbum", "musicartist"}:
        return MediaType.MUSIC.value
    if lowered == "boxset":
        return "合集"
    return MediaType.MOVIE.value


def _subtitle_for_browse_item(raw: Mapping[str, Any]) -> Optional[str]:
    """
    生成浏览卡片与季集卡片的简洁副标题。

    :param raw: 服务端原始条目字典
    :return: 副标题文本或 None
    """
    item_type = str(raw.get("Type") or "")
    if item_type == "Episode":
        season_num = raw.get("ParentIndexNumber")
        ep_num = raw.get("IndexNumber")
        if season_num is not None and ep_num is not None:
            return f"S{int(season_num):02d}E{int(ep_num):02d}"
        if ep_num is not None:
            return f"第 {int(ep_num)} 集"
        return raw.get("SeriesName")
    if item_type == "Audio":
        artists = raw.get("Artists") or []
        artist = next((str(a.get("Name") if isinstance(a, Mapping) else a) for a in artists if a), None)
        album = raw.get("Album")
        parts = [part for part in (artist, album) if part]
        return " · ".join(parts) if parts else None
    if item_type == "MusicAlbum":
        return raw.get("AlbumArtist") or (str(raw.get("ProductionYear")) if raw.get("ProductionYear") else None)
    if item_type == "BoxSet":
        count = raw.get("ChildCount") or raw.get("RecursiveItemCount")
        return f"共 {int(count)} 部" if isinstance(count, int) and count > 0 else "合集"
    year = raw.get("ProductionYear")
    return str(year) if year else None


def _build_browse_item(cfg: EmbyBrowseConfig, raw: Mapping[str, Any]) -> Optional[MediaServerBrowseItem]:
    """
    将 Emby 原始 JSON 字典转换为统一 `MediaServerBrowseItem`。

    :param cfg: Emby 系协议配置
    :param raw: 服务端原始条目字典
    :return: 统一轻量卡片模型；缺少标识或名称时返回 None
    """
    item_id = str(raw.get("Id") or "").strip()
    title = str(raw.get("Name") or "").strip()
    if not item_id or not title:
        return None
    item_type = str(raw.get("Type") or "Movie")
    poster, backdrop, thumb = _resolve_images(cfg, raw)
    raw_user_data = raw.get("UserData")
    user_data: Mapping[str, Any] = raw_user_data if isinstance(raw_user_data, Mapping) else {}
    duration_ms = _ticks_to_ms(raw.get("RunTimeTicks"))
    resume_ms = _ticks_to_ms(user_data.get("PlaybackPositionTicks"))
    percentage = user_data.get("PlayedPercentage")
    if percentage is None and duration_ms and resume_ms:
        percentage = round(min(100.0, resume_ms / duration_ms * 100.0), 1)
    season_num = raw.get("ParentIndexNumber") if item_type == "Episode" else (
        raw.get("IndexNumber") if item_type == "Season" else None
    )
    episode_num = raw.get("IndexNumber") if item_type == "Episode" else None
    premiere = raw.get("PremiereDate")
    premiere_str = str(premiere)[:10] if isinstance(premiere, str) and premiere else None
    rating = raw.get("CommunityRating")
    return MediaServerBrowseItem(
        item_id=item_id,
        server_type=cfg.server_type,
        item_type=item_type,
        media_type=_media_type_for_item(item_type),
        title=title,
        original_title=raw.get("OriginalTitle"),
        subtitle=_subtitle_for_browse_item(raw),
        overview=raw.get("Overview"),
        year=raw.get("ProductionYear"),
        premiere_date=premiere_str,
        rating=round(float(rating), 1) if isinstance(rating, (int, float)) else None,
        duration_ms=duration_ms,
        poster=poster,
        backdrop=backdrop,
        thumb=thumb,
        series_id=str(raw.get("SeriesId")) if raw.get("SeriesId") else None,
        series_name=raw.get("SeriesName"),
        season_id=str(raw.get("SeasonId")) if raw.get("SeasonId") else None,
        season_number=int(season_num) if isinstance(season_num, int) else None,
        episode_number=int(episode_num) if isinstance(episode_num, int) else None,
        child_count=raw.get("ChildCount") if isinstance(raw.get("ChildCount"), int) else None,
        played=bool(user_data.get("Played")) if "Played" in user_data else None,
        played_percentage=float(percentage) if isinstance(percentage, (int, float)) else None,
        resume_position_ms=resume_ms,
    )


def _format_stream_display(stream: Mapping[str, Any], stream_type: str) -> str:
    """
    为轨道生成人类可读的展示标题，优先采用服务端 `DisplayTitle`。

    :param stream: 原始 MediaStream 字典
    :param stream_type: 归一化后的轨道类型
    :return: 展示文本
    """
    if stream.get("DisplayTitle"):
        return str(stream["DisplayTitle"])
    parts: list[str] = []
    for key in ("Title", "Language", "Codec"):
        val = stream.get(key)
        if val and str(val) not in parts:
            parts.append(str(val).upper() if key == "Codec" else str(val))
    if stream_type == "audio" and stream.get("ChannelLayout"):
        parts.append(str(stream["ChannelLayout"]))
    return " · ".join(parts) if parts else ("音轨" if stream_type == "audio" else "字幕")


def _parse_media_stream(source_id: str, raw: Mapping[str, Any]) -> MediaServerMediaStream:
    """
    将 Emby `MediaStream` 转换为统一轨道模型，并为外挂字幕生成代理标识。

    :param source_id: 所属媒体源标识
    :param raw: 原始 MediaStream 字典
    :return: 统一轨道模型
    """
    raw_type = str(raw.get("Type") or "").casefold()
    stream_type: Any = raw_type if raw_type in {"video", "audio", "subtitle"} else "unknown"
    index = raw.get("Index") if isinstance(raw.get("Index"), int) else None
    codec = str(raw.get("Codec") or "").lower() or None
    is_external = bool(raw.get("IsExternal"))
    subtitle_id: Optional[str] = None
    if stream_type == "subtitle" and index is not None and (is_external or raw.get("DeliveryUrl")):
        ext = _SUBTITLE_EXT_MAP.get(codec or "", "srt")
        subtitle_id = f"ms:{source_id}:{index}.{ext}"
    return MediaServerMediaStream(
        index=index,
        type=stream_type,
        codec=codec,
        language=raw.get("Language"),
        title=raw.get("Title"),
        display_title=_format_stream_display(raw, stream_type),
        is_default=bool(raw.get("IsDefault")),
        is_forced=bool(raw.get("IsForced")),
        is_external=is_external,
        subtitle_id=subtitle_id,
        channels=raw.get("Channels") if isinstance(raw.get("Channels"), int) else None,
        channel_layout=raw.get("ChannelLayout"),
        bitrate=raw.get("BitRate") if isinstance(raw.get("BitRate"), int) else None,
        width=raw.get("Width") if isinstance(raw.get("Width"), int) else None,
        height=raw.get("Height") if isinstance(raw.get("Height"), int) else None,
        video_range=raw.get("VideoRange") or raw.get("VideoRangeType"),
    )


def _sanitize_emby_strm_url(cfg: Optional[EmbyBrowseConfig], raw_path: Any) -> Optional[str]:
    """
    校验并规范化 Emby 系媒体源报告的远端 STRM HTTP(S) 直链，排除包含服务端密钥或内嵌凭据的地址。

    :param cfg: 可选 Emby 系协议配置，用于排除带服务端 API 密钥的地址
    :param raw_path: 媒体源 ``Path`` 字段原始值
    :return: 可供客户端直连播放的 HTTP(S) 地址；非远端直链或包含敏感凭据时返回 None
    """
    if not isinstance(raw_path, str):
        return None
    candidate = raw_path.strip().split("|", 1)[0].strip()
    if not candidate or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in candidate):
        return None
    if cfg and cfg.api_key and cfg.api_key in candidate:
        return None
    parsed = urlsplit(candidate)
    scheme = parsed.scheme.casefold()
    hostname = (parsed.hostname or "").strip().rstrip(".")
    if (
        scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        return None
    safe_path = quote(parsed.path, safe="/:@!$&'()*+,;=-._~%")
    safe_query = quote(parsed.query, safe="/?:@!$&'()*+,;=-._~%")
    return urlunsplit((scheme, parsed.netloc, safe_path, safe_query, ""))


def _fetch_playback_sources(cfg: EmbyBrowseConfig, item_id: str) -> list[Mapping[str, Any]]:
    """
    查询 Emby 系 ``PlaybackInfo`` 接口获取已展开的媒体源列表（含服务端解析后的 STRM 目标地址）。

    :param cfg: Emby 系协议配置
    :param item_id: 媒体条目标识
    :return: 原始媒体源字典列表；查询失败时返回空列表
    """
    if not cfg.host or not cfg.api_key or not item_id:
        return []
    try:
        pb_params: dict[str, Any] = {}
        if cfg.user_id:
            pb_params["UserId"] = cfg.user_id
        response = cfg.request_factory().get_res(
            f"{cfg.host}{cfg.api_prefix}Items/{quote(str(item_id), safe='')}/PlaybackInfo",
            _api_params(cfg, pb_params),
        )
        if response is None or response.status_code != 200:
            return []
        sources = (response.json() or {}).get("MediaSources") or []
        return [val for val in sources if isinstance(val, Mapping)]
    except Exception as err:
        logger.debug(f"读取 {cfg.server_type} PlaybackInfo 失败：{err}")
        return []


def _extract_emby_source_direct_url(
    source: Mapping[str, Any], cfg: Optional[EmbyBrowseConfig] = None
) -> Optional[str]:
    """
    从单个 Emby 媒体源字典中提取 STRM HTTP(S) 直链地址。

    :param source: 服务端单个 ``MediaSource`` 字典
    :param cfg: 可选 Emby 系协议配置
    :return: 校验通过的 HTTP(S) 直链；本地文件媒体源返回 None
    """
    protocol = str(source.get("Protocol") or "").strip().casefold()
    container = str(source.get("Container") or "").strip().casefold()
    raw_path = source.get("Path")
    if protocol == "http" or container == "strm" or bool(source.get("IsRemote")):
        return _sanitize_emby_strm_url(cfg, raw_path)
    return None


def _parse_media_sources(
    raw: Mapping[str, Any],
    *,
    cfg: Optional[EmbyBrowseConfig] = None,
    item_id: Optional[str] = None,
) -> list[MediaServerMediaSource]:
    """
    提取条目的全部多版本媒体源及其视频、音频和字幕轨道，并在 STRM 源未展开时补查直链。

    :param raw: 服务端原始条目字典
    :param cfg: 可选 Emby 系协议配置，用于在 STRM 未展开时请求 ``PlaybackInfo``
    :param item_id: 可选媒体条目标识
    :return: 统一媒体源列表
    """
    sources: list[MediaServerMediaSource] = []
    playback_by_id: Optional[dict[str, Mapping[str, Any]]] = None
    item_path_is_strm = str(raw.get("Path") or "").strip().casefold().endswith(".strm")
    for source in raw.get("MediaSources") or []:
        if not isinstance(source, Mapping):
            continue
        source_id = str(source.get("Id") or "").strip()
        if not source_id:
            continue
        direct_url = _extract_emby_source_direct_url(source, cfg)
        source_is_strm = (
            item_path_is_strm
            or str(source.get("Container") or "").strip().casefold() == "strm"
            or str(source.get("Path") or "").strip().casefold().endswith(".strm")
        )
        if direct_url is None and source_is_strm and cfg is not None and item_id:
            if playback_by_id is None:
                playback_by_id = {
                    str(pb.get("Id") or "").strip(): pb
                    for pb in _fetch_playback_sources(cfg, item_id)
                    if str(pb.get("Id") or "").strip()
                }
            matched_pb = playback_by_id.get(source_id) or next(iter(playback_by_id.values()), None)
            if matched_pb is not None:
                direct_url = _extract_emby_source_direct_url(matched_pb, cfg)
        streams = [
            _parse_media_stream(source_id, item)
            for item in (source.get("MediaStreams") or [])
            if isinstance(item, Mapping)
        ]
        sources.append(
            MediaServerMediaSource(
                id=source_id,
                name=source.get("Name") or source.get("Container"),
                container=source.get("Container"),
                size=source.get("Size") if isinstance(source.get("Size"), int) else None,
                bitrate=source.get("Bitrate") if isinstance(source.get("Bitrate"), int) else None,
                duration_ms=_ticks_to_ms(source.get("RunTimeTicks")),
                direct_url=direct_url,
                streams=streams,
            )
        )
    return sources


def _parse_people(cfg: EmbyBrowseConfig, raw: Mapping[str, Any]) -> list[MediaServerPerson]:
    """
    解析演职员列表并构造头像地址（最多保留 24 位避免冗余体积）。

    :param cfg: Emby 系协议配置
    :param raw: 服务端原始条目字典
    :return: 演职员列表
    """
    people: list[MediaServerPerson] = []
    for person in (raw.get("People") or [])[:24]:
        if not isinstance(person, Mapping) or not person.get("Name"):
            continue
        person_id = str(person.get("Id") or "") or None
        tag = person.get("PrimaryImageTag")
        image = _image_url(cfg, person_id, "Primary", str(tag), max_width=300) if person_id and tag else None
        people.append(
            MediaServerPerson(
                id=person_id,
                name=str(person["Name"]),
                role=person.get("Role"),
                type=person.get("Type"),
                image=image,
            )
        )
    return people


def emby_browse_items(
    cfg: EmbyBrowseConfig,
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
) -> Optional[MediaServerBrowsePage]:
    """
    在 Emby 系服务器中按分页、筛选、排序与搜索浏览媒体库条目。

    :param cfg: Emby 系协议配置
    :param library_id: 媒体库标识
    :param parent_id: 可选父目录标识，优先级高于 library_id
    :param item_type: 维度或业务媒体类型过滤
    :param genre: 风格标签过滤
    :param year: 年份过滤
    :param played_filter: 观看状态过滤：played / unplayed
    :param sort_by: 规范化排序键
    :param sort_order: 排序方向：asc / desc
    :param keyword: 库内关键词搜索
    :param start_index: 零基分页偏移
    :param limit: 单页数量上限
    :return: 分页浏览结果；服务请求失败时返回 None
    """
    if not cfg.host or not cfg.user_id:
        return None
    default_types = "Movie,Series,MusicAlbum,BoxSet,Episode" if parent_id else "Movie,Series,MusicAlbum,BoxSet"
    resolved_types = _ITEM_TYPE_MAP.get(str(item_type or "").strip().casefold()) or (
        item_type if item_type else default_types
    )
    sort_key = (sort_by or "date_created").strip().casefold()
    emby_sort = _SORT_BY_MAP.get(sort_key, _SORT_BY_MAP["date_created"])
    default_order = "asc" if sort_key == "name" else "desc"
    resolved_order = "Ascending" if (sort_order or default_order).casefold() == "asc" else "Descending"
    filters = "IsPlayed" if played_filter == "played" else ("IsUnplayed" if played_filter == "unplayed" else None)
    safe_start = max(0, int(start_index))
    safe_limit = max(1, min(int(limit), 100))

    url = f"{cfg.host}{cfg.api_prefix}Users/{cfg.user_id}/Items"
    params = _api_params(
        cfg,
        {
            "ParentId": parent_id or library_id,
            "Recursive": "false" if parent_id else "true",
            "IncludeItemTypes": resolved_types,
            "Genres": genre,
            "Years": str(year) if year else None,
            "Filters": filters,
            "SearchTerm": keyword.strip() if keyword and keyword.strip() else None,
            "SortBy": emby_sort,
            "SortOrder": resolved_order,
            "Fields": _BROWSE_FIELDS,
            "StartIndex": safe_start,
            "Limit": safe_limit,
        },
    )
    try:
        res = cfg.request_factory().get_res(url, params)
        if not res or res.status_code != 200:
            return None
        payload = res.json() or {}
        raw_items = payload.get("Items") or []
        items = [built for raw in raw_items if isinstance(raw, Mapping) and (built := _build_browse_item(cfg, raw))]
        total_raw = payload.get("TotalRecordCount")
        total = int(total_raw) if isinstance(total_raw, int) else (safe_start + len(items))
        return MediaServerBrowsePage(
            items=items,
            total_count=total,
            start_index=safe_start,
            limit=safe_limit,
            has_more=(safe_start + len(items)) < total,
        )
    except Exception as err:
        logger.error(f"浏览 {cfg.server_type} 媒体库失败：{err}")
        return None


def emby_filter_options(cfg: EmbyBrowseConfig, library_id: Optional[str] = None) -> MediaServerFilterOptions:
    """
    获取 Emby 系指定媒体库可用的风格、年份和排序筛选选项。

    :param cfg: Emby 系协议配置
    :param library_id: 可选媒体库标识
    :return: 可用筛选项模型
    """
    genres: list[str] = []
    years: list[int] = []
    if cfg.host and cfg.user_id:
        url = f"{cfg.host}{cfg.api_prefix}Items/Filters"
        params = _api_params(cfg, {"UserId": cfg.user_id, "ParentId": library_id, "Recursive": "true"})
        try:
            res = cfg.request_factory().get_res(url, params)
            if res and res.status_code == 200:
                data = res.json() or {}
                genres = [str(g) for g in (data.get("Genres") or []) if g]
                years = sorted({int(y) for y in (data.get("Years") or []) if str(y).isdigit()}, reverse=True)
        except Exception as err:
            logger.debug(f"读取 {cfg.server_type} 筛选项失败：{err}")
        if not genres:
            genres = _fallback_emby_genres(cfg, library_id)
    return MediaServerFilterOptions(
        genres=genres,
        years=years,
        item_types=["Movie", "Series", "MusicAlbum"],
        sort_options=list(_DEFAULT_SORT_OPTIONS),
    )


def _fallback_emby_genres(cfg: EmbyBrowseConfig, library_id: Optional[str]) -> list[str]:
    """
    当 `Items/Filters` 未返回风格时回退读取 `Genres` 端点。

    :param cfg: Emby 系协议配置
    :param library_id: 可选媒体库标识
    :return: 风格名称列表
    """
    try:
        res = cfg.request_factory().get_res(
            f"{cfg.host}{cfg.api_prefix}Genres",
            _api_params(cfg, {"UserId": cfg.user_id, "ParentId": library_id, "Recursive": "true"}),
        )
        if res and res.status_code == 200:
            return [str(item["Name"]) for item in (res.json().get("Items") or []) if isinstance(item, Mapping) and item.get("Name")]
    except Exception as err:
        logger.debug(f"回退读取 {cfg.server_type} Genres 失败：{err}")
    return []


def emby_item_detail(cfg: EmbyBrowseConfig, item_id: str) -> Optional[MediaServerItemDetail]:
    """
    查询 Emby 系单个条目的完整详情，含演职员、外部 ID、多版本及音轨字幕轨道。

    :param cfg: Emby 系协议配置
    :param item_id: 媒体服务器条目标识
    :return: 完整详情模型；条目不存在或请求失败时返回 None
    """
    if not cfg.host or not cfg.user_id or not item_id:
        return None
    url = f"{cfg.host}{cfg.api_prefix}Users/{cfg.user_id}/Items/{quote(str(item_id), safe='')}"
    try:
        res = cfg.request_factory().get_res(url, _api_params(cfg))
        if not res or res.status_code != 200:
            return None
        raw = res.json()
        if not isinstance(raw, Mapping):
            return None
    except Exception as err:
        logger.error(f"查询 {cfg.server_type} 条目详情 {item_id} 失败：{err}")
        return None

    base = _build_browse_item(cfg, raw)
    if not base:
        return None
    raw_provider_ids = raw.get("ProviderIds")
    provider_ids: Mapping[str, Any] = raw_provider_ids if isinstance(raw_provider_ids, Mapping) else {}
    media_source, media_id = MediaServerIdentityHelper.from_provider_ids(provider_ids)
    taglines = raw.get("Taglines") or []
    studios = [
        str(studio["Name"]) if isinstance(studio, Mapping) and studio.get("Name") else str(studio)
        for studio in (raw.get("Studios") or [])
        if studio
    ]
    media_sources = _parse_media_sources(raw, cfg=cfg, item_id=base.item_id)
    play_url = cfg.play_url_builder(base.item_id) if cfg.play_url_builder else None
    item_type = base.item_type
    season_count = raw.get("ChildCount") if item_type == "Series" and isinstance(raw.get("ChildCount"), int) else None
    episode_count = (
        raw.get("ChildCount") if item_type == "Season" and isinstance(raw.get("ChildCount"), int)
        else (raw.get("RecursiveItemCount") if item_type == "Series" and isinstance(raw.get("RecursiveItemCount"), int) else None)
    )
    return MediaServerItemDetail(
        **base.model_dump(),
        tagline=str(taglines[0]) if taglines else None,
        official_rating=raw.get("OfficialRating"),
        genres=[str(g) for g in (raw.get("Genres") or []) if g],
        studios=studios,
        people=_parse_people(cfg, raw),
        media_source=media_source,
        media_id=media_id,
        imdb_id=str(provider_ids.get("Imdb") or "") or None,
        tmdb_id=str(provider_ids.get("Tmdb") or "") or None,
        tvdb_id=str(provider_ids.get("Tvdb") or "") or None,
        douban_id=str(provider_ids.get("Douban") or "") or None,
        parent_id=str(raw.get("ParentId")) if raw.get("ParentId") else None,
        season_name=raw.get("SeasonName"),
        season_count=season_count,
        episode_count=episode_count,
        media_sources=media_sources,
        play_url=play_url,
        can_play=item_type in {"Movie", "Episode", "Audio"} or bool(media_sources),
    )


def emby_seasons(cfg: EmbyBrowseConfig, series_id: str) -> Optional[list[MediaServerBrowseItem]]:
    """
    查询 Emby 系电视剧的全部季卡片信息。

    :param cfg: Emby 系协议配置
    :param series_id: 电视剧条目标识
    :return: 按季号升序排列的季列表；失败时返回 None
    """
    if not cfg.host or not cfg.user_id or not series_id:
        return None
    url = f"{cfg.host}{cfg.api_prefix}Shows/{quote(str(series_id), safe='')}/Seasons"
    params = _api_params(cfg, {"UserId": cfg.user_id, "Fields": _BROWSE_FIELDS})
    try:
        res = cfg.request_factory().get_res(url, params)
        if not res or res.status_code != 200:
            return None
        raw_items = (res.json() or {}).get("Items") or []
        seasons = [built for raw in raw_items if isinstance(raw, Mapping) and (built := _build_browse_item(cfg, raw))]
        seasons.sort(key=lambda item: (item.season_number is None, item.season_number or 0))
        return seasons
    except Exception as err:
        logger.error(f"查询 {cfg.server_type} 剧集 {series_id} 季列表失败：{err}")
        return None


def emby_season_episodes(
    cfg: EmbyBrowseConfig,
    series_id: str,
    *,
    season_id: Optional[str] = None,
    season: Optional[int] = None,
) -> Optional[list[MediaServerBrowseItem]]:
    """
    查询 Emby 系指定季的全部单集详情卡片（含剧照、时长、简介与播放状态）。

    :param cfg: Emby 系协议配置
    :param series_id: 电视剧条目标识
    :param season_id: 可选季条目标识，优先级高于季号
    :param season: 可选季号（0 表示特别篇）
    :return: 按集号升序排列的单集列表；失败时返回 None
    """
    if not cfg.host or not cfg.user_id or not series_id:
        return None
    url = f"{cfg.host}{cfg.api_prefix}Shows/{quote(str(series_id), safe='')}/Episodes"
    params = _api_params(
        cfg,
        {
            "UserId": cfg.user_id,
            "SeasonId": season_id,
            "Season": season if season_id is None and season is not None else None,
            "Fields": _BROWSE_FIELDS,
        },
    )
    try:
        res = cfg.request_factory().get_res(url, params)
        if not res or res.status_code != 200:
            return None
        raw_items = (res.json() or {}).get("Items") or []
        episodes = [built for raw in raw_items if isinstance(raw, Mapping) and (built := _build_browse_item(cfg, raw))]
        episodes.sort(key=lambda item: (item.season_number or 0, item.episode_number or 0))
        return episodes
    except Exception as err:
        logger.error(f"查询 {cfg.server_type} 剧集 {series_id} 分集列表失败：{err}")
        return None


def emby_stream_url(
    cfg: EmbyBrowseConfig,
    item_id: str,
    item_type: Optional[str],
    media_source_id: Optional[str] = None,
) -> Optional[str]:
    """
    为 Emby 系服务器构造支持 HTTP Range 的原始静态媒体流地址，支持指定多版本 `media_source_id`。

    :param cfg: Emby 系协议配置
    :param item_id: 媒体条目标识
    :param item_type: 媒体类型，用于选择 Audio 或 Videos 端点
    :param media_source_id: 可选指定媒体源标识
    :return: 上游原始媒体流地址；不可用时返回 None
    """
    if not cfg.host or not cfg.api_key or not item_id:
        return None
    selected_source_id = media_source_id
    if not selected_source_id:
        sources = _fetch_playback_sources(cfg, item_id)
        source = next(
            (val for val in sources if val.get("Id") and not val.get("RequiresOpening")),
            None,
        )
        if not source:
            return None
        direct_url = _extract_emby_source_direct_url(source, cfg)
        if direct_url:
            return direct_url
        selected_source_id = str(source["Id"])

    route = "Audio" if (item_type or "").casefold() in {"audio", "music", MediaType.MUSIC.value.casefold()} else "Videos"
    play_session_id = hashlib.sha256(
        f"{cfg.host}:{cfg.user_id}:{item_id}".encode("utf-8")
        if not media_source_id
        else f"{cfg.host}:{cfg.user_id}:{item_id}:{selected_source_id}".encode("utf-8")
    ).hexdigest()[:32]
    query = urlencode({
        "static": "true",
        "MediaSourceId": selected_source_id,
        "PlaySessionId": play_session_id,
        "api_key": cfg.api_key,
    })
    return f"{cfg.host}{cfg.api_prefix}{route}/{quote(str(item_id), safe='')}/stream?{query}"


def emby_subtitle_url(cfg: EmbyBrowseConfig, item_id: str, subtitle_id: str) -> Optional[str]:
    """
    将代理字幕标识 `ms:<media_source_id>:<index>.<ext>` 解析为 Emby 系上游字幕下载流地址。

    :param cfg: Emby 系协议配置
    :param item_id: 媒体条目标识
    :param subtitle_id: 代理字幕标识
    :return: 上游字幕下载地址；格式不匹配或未配置时返回 None
    """
    if not cfg.host or not cfg.api_key or not item_id:
        return None
    match = _SUBTITLE_ID_RE.match(subtitle_id.strip())
    if not match:
        return None
    source_id, stream_index, ext = match.groups()
    query = urlencode({"api_key": cfg.api_key})
    return (
        f"{cfg.host}{cfg.api_prefix}Videos/{quote(str(item_id), safe='')}/"
        f"{quote(source_id, safe='')}/Subtitles/{stream_index}/Stream.{ext}?{query}"
    )


def emby_report_progress(
    cfg: EmbyBrowseConfig,
    item_id: str,
    position_ms: int,
    *,
    media_source_id: Optional[str] = None,
    is_paused: bool = False,
    state: str = "playing",
) -> bool:
    """
    将客户端播放进度回传给 Emby 系服务器，同步继续观看和已看状态。

    :param cfg: Emby 系协议配置
    :param item_id: 正在播放的条目标识
    :param position_ms: 当前播放位置（毫秒）
    :param media_source_id: 可选媒体源标识
    :param is_paused: 是否处于暂停状态
    :param state: 事件状态：playing / paused / stopped
    :return: 服务端接收成功时返回 True
    """
    if not cfg.host or not cfg.api_key or not item_id:
        return False
    endpoint = "Sessions/Playing/Stopped" if state == "stopped" else "Sessions/Playing/Progress"
    play_session_id = hashlib.sha256(f"{cfg.host}:{cfg.user_id}:{item_id}".encode("utf-8")).hexdigest()[:32]
    payload: dict[str, Any] = {
        "ItemId": str(item_id),
        "PositionTicks": max(0, int(position_ms)) * _TICKS_PER_MS,
        "IsPaused": bool(is_paused or state == "paused"),
        "PlayMethod": "DirectPlay",
        "PlaySessionId": play_session_id,
    }
    if media_source_id:
        payload["MediaSourceId"] = media_source_id
    url = f"{cfg.host}{cfg.api_prefix}{endpoint}"
    try:
        res = cfg.request_factory().post_res(
            url=url,
            params=_api_params(cfg),
            json=payload,
        )
        return bool(res and res.status_code in {200, 204})
    except Exception as err:
        logger.debug(f"回传 {cfg.server_type} 播放进度失败：{err}")
        return False


def emby_search_library(
    cfg: EmbyBrowseConfig,
    keyword: str,
    types: list[str],
    limit: int,
    library_id: Optional[str] = None,
) -> list[MediaServerPlayItem]:
    """
    在 Emby 系服务器中搜索电影、电视剧与音乐单曲。

    :param cfg: Emby 系协议配置
    :param keyword: 搜索关键词
    :param types: 允许的业务类型列表（电影、电视剧、音乐）
    :param limit: 返回条数上限
    :param library_id: 可选媒体库标识限定
    :return: 统一搜索结果列表
    """
    if not cfg.host or not cfg.user_id or not keyword.strip():
        return []
    server_types: list[str] = []
    if MediaType.MOVIE.value in types:
        server_types.append("Movie")
    if MediaType.TV.value in types:
        server_types.append("Series")
    if MediaType.MUSIC.value in types:
        server_types.append("Audio")
    if not server_types:
        return []
    url = f"{cfg.host}{cfg.api_prefix}Users/{cfg.user_id}/Items"
    params = _api_params(
        cfg,
        {
            "ParentId": library_id,
            "IncludeItemTypes": ",".join(server_types),
            "Fields": "ProductionYear,Album,Artists,AlbumArtist,ImageTags",
            "Recursive": "true",
            "SearchTerm": keyword.strip(),
            "StartIndex": 0,
            "Limit": max(1, min(limit, 50)),
        },
    )
    try:
        response = cfg.request_factory().get_res(url, params)
        if not response or response.status_code != 200:
            return []
        results: list[MediaServerPlayItem] = []
        for item in response.json().get("Items") or []:
            if not isinstance(item, Mapping):
                continue
            item_id = str(item.get("Id") or "")
            title = str(item.get("Name") or "")
            raw_type = str(item.get("Type") or "")
            if not item_id or not title or raw_type not in server_types:
                continue
            image_tag = (item.get("ImageTags") or {}).get("Primary") if isinstance(item.get("ImageTags"), Mapping) else None
            results.append(
                MediaServerPlayItem(
                    id=item_id,
                    item_id=item_id,
                    title=title,
                    subtitle=_subtitle_for_browse_item(item),
                    year=item.get("ProductionYear"),
                    type=_media_type_for_item(raw_type),
                    image=_image_url(cfg, item_id, "Primary", image_tag, max_width=600),
                    server_type=cfg.server_type,
                )
            )
            if len(results) >= limit:
                break
        return results
    except Exception as err:
        logger.debug(f"搜索 {cfg.server_type} 媒体库失败：{err}")
        return []
