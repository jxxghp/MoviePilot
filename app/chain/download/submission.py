"""
种子获取、提交前校验与单个下载任务提交。
"""

import base64
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from copy import copy, deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Set, Tuple, Union, cast
from urllib.parse import urlparse

from app.application.configuration import (
    get_chain_runtime_config_snapshot,
    get_runtime_settings,
)
from app.application.directory import (
    specified_download_save_path,
    validate_download_save_path,
)
from app.application.download.admission import SubscriptionDownloadGovernance, subscription_download_label
from app.application.site.protocol import is_expiring_download_url, torrent_resource_identity

# 站点资源包在启动时加载，静态检查环境可能无法解析其实现。
from app.application.site.sites import SitesHelper  # pylint: disable=import-error,no-name-in-module
from app.application.site.throttle import defer_site_request, site_request_config, wait_for_site_request
from app.application.torrent.download import TorrentHelper, torrent_rate_limit_error
from app.chain.download.contract import _DownloadOwnerBase
from app.chain.download.ports import (
    DownloadResponsePort,
    _close_download_response,
    _download_ports_snapshot,
)
from app.chain.media import MediaChain
from app.domain import episode as episode_rules
from app.domain import site as site_rules
from app.domain import torrent as torrent_rules
from app.domain.context import (
    Context,
    MediaInfo,
    MusicInfo,
    TorrentInfo,
)
from app.domain.meta.metabase import MetaBase
from app.domain.metainfo import MetaInfo
from app.foundation.url import UrlUtils, url_origin_for_log
from app.runtime.cache import FileCache
from app.runtime.events import eventmanager
from app.runtime.locks import downloader_task_locks
from app.runtime.log import logger
from app.schemas.common import JsonData
from app.schemas.download import (
    DownloadTaskAction,
    ExistingTaskRejection,
    is_unconfirmed_download_message,
)
from app.schemas.event import ResourceDownloadEventData
from app.schemas.exception import RateLimitExceededException
from app.schemas.file import FileURI
from app.schemas.media import normalize_media_source, resolve_media_identity
from app.schemas.message import Message
from app.schemas.types import (
    ChainEventType,
    MediaSource,
    MediaType,
    MessageType,
    ModuleType,
    NotificationChannel,
)

# 转换后可无损回填到 TMDB 媒体对象的非主来源外链字段，保持主身份不变。
_AUXILIARY_EXTERNAL_ID_FIELDS = (
    "douban_id",
    "bangumi_id",
    "anilist_id",
    "imdb_id",
    "tvdb_id",
    "tvdb_slug",
)


def is_tmdb_recognize_source() -> bool:
    """
    判断当前系统影视识别源配置是否为 TMDB。

    :return: 全局 RECOGNIZE_SOURCE 规范化为 TMDB 时返回 True，否则返回 False
    """
    return normalize_media_source(get_runtime_settings().get("RECOGNIZE_SOURCE")) == MediaSource.TMDB


def _valid_season_number(value: object) -> Optional[int]:
    """
    提取有效的非负整数季号，排除布尔值和负数。

    :param value: 来源或投影返回的原始季号字段
    :return: 合法季号（含第 0 季特别篇）；无效时返回 None
    """
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def explicit_meta_seasons(meta: MetaBase) -> list[int]:
    """
    从元数据提取显式解析出的季号列表，不把缺失季号的电视剧默认当成第 1 季。

    ``MetaVideo`` 在标题仅含集号（如 ``E01-E05``）且未标季时会回填 ``begin_season=1``
    但保持 ``total_season=0``；此类默认补齐不算显式季号，以便豆瓣或 Bangumi 季级条目
    能正确带入真实季号。

    :param meta: 种子标题解析出的元数据
    :return: 按升序排列的显式季号列表；未解析出季号时返回空列表
    """
    begin = _valid_season_number(meta.begin_season)
    if begin is None:
        return []
    end = _valid_season_number(meta.end_season)
    if end is not None and end >= begin:
        return list(range(begin, end + 1))
    if begin == 1 and getattr(meta, "total_season", 0) == 0:
        return []
    return [begin]


def _extract_source_media_season(source_media: Optional[MediaInfo]) -> Optional[int]:
    """
    从非 TMDB 媒体对象本身提取季号，兼容豆瓣和 Bangumi 等季级条目标题。

    :param source_media: 转换前的原媒体对象
    :return: 提取到的季号；无法确定时返回 None
    """
    if source_media is None:
        return None
    direct_season = _valid_season_number(source_media.season)
    if direct_season is not None:
        return direct_season
    if not source_media.title:
        return None
    return _valid_season_number(MetaInfo(source_media.title).begin_season)


def _resolve_conversion_query_season(
    metainfo: MetaBase,
    media_source: MediaSource,
    source_media: Optional[MediaInfo],
    expected_mtype: Optional[MediaType],
) -> Optional[int]:
    """
    决定传给跨来源身份投影的查询季号，避免把电影或多季包误压成单季。

    :param metainfo: 种子标题解析元数据
    :param media_source: 待转换的非 TMDB 媒体来源
    :param source_media: 已知的原媒体对象；无媒体上下文下载时为 None
    :param expected_mtype: 已确认的影视类型；未确定时为 None
    :return: 传给 ``convert_media_identity`` 的季号，未限定时返回 None
    """
    if expected_mtype == MediaType.MOVIE:
        return None
    explicit_seasons = explicit_meta_seasons(metainfo)
    single_meta_season = explicit_seasons[0] if len(explicit_seasons) == 1 else None
    if source_media is not None:
        if single_meta_season is not None:
            return single_meta_season
        if not explicit_seasons:
            return _extract_source_media_season(source_media)
        return _valid_season_number(source_media.season)
    if media_source == MediaSource.IMDb:
        return single_meta_season if metainfo.type == MediaType.TV else None
    return single_meta_season


def _parse_tmdb_media_type(raw_type: object) -> Optional[MediaType]:
    """
    把投影返回的影视类型字段规范为电影或电视剧枚举。

    :param raw_type: 投影结果中的 ``media_type`` 原始值
    :return: 电影或电视剧枚举；无法识别或非影视类型时返回 None
    """
    parsed = (
        raw_type
        if isinstance(raw_type, MediaType)
        else MediaType.from_agent(str(raw_type or ""))
    )
    return parsed if parsed in (MediaType.MOVIE, MediaType.TV) else None


def available_tmdb_seasons(
    converted: MediaInfo,
    tmdbinfo: Optional[Mapping[str, Any]] = None,
) -> set[int]:
    """
    收集 TMDB 识别结果中已列出的合法季号集合，用于校验目标季是否存在。

    :param converted: TMDB 完整识别得到的媒体对象
    :param tmdbinfo: 投影步骤返回的原始 TMDB 字典，可为 None
    :return: 已确认存在的季号集合；结果未提供季清单时返回空集合
    """
    seasons: set[int] = {
        season for season in (converted.seasons or {})
        if _valid_season_number(season) is not None
    }
    for item in converted.season_info or ():
        if isinstance(item, Mapping):
            season_num = _valid_season_number(item.get("season_number"))
            if season_num is not None:
                seasons.add(season_num)
    if seasons or not tmdbinfo:
        return seasons
    raw_seasons = tmdbinfo.get("seasons")
    if not isinstance(raw_seasons, Sequence) or isinstance(raw_seasons, (str, bytes)):
        return set()
    for item in raw_seasons:
        if isinstance(item, Mapping):
            season_num = _valid_season_number(item.get("season_number"))
            if season_num is not None:
                seasons.add(season_num)
    return seasons


def _preserve_origin_media_fields(
    converted: MediaInfo,
    origin_source: MediaSource,
    origin_id: str,
    source_media: Optional[MediaInfo],
) -> None:
    """
    在转换后的 TMDB 媒体对象上回填缺失的原来源外链标识，保持 TMDB 主身份不变。

    :param converted: 转换并识别完成的 TMDB 媒体对象
    :param origin_source: 转换前的原媒体来源
    :param origin_id: 转换前的原来源原生标识
    :param source_media: 转换前的原媒体对象，可为空
    """
    source_field_map: dict[MediaSource, tuple[str, bool]] = {
        MediaSource.Douban: ("douban_id", False),
        MediaSource.Bangumi: ("bangumi_id", True),
        MediaSource.AniList: ("anilist_id", True),
        MediaSource.IMDb: ("imdb_id", False),
        MediaSource.TVDB: ("tvdb_id", True),
    }
    target = source_field_map.get(origin_source)
    if target is not None:
        field_name, is_numeric = target
        if not getattr(converted, field_name, None):
            if is_numeric:
                if origin_id.isdigit():
                    setattr(converted, field_name, int(origin_id))
            else:
                setattr(converted, field_name, origin_id)
    if source_media is None:
        return
    for field_name in _AUXILIARY_EXTERNAL_ID_FIELDS:
        if not getattr(converted, field_name, None):
            value = getattr(source_media, field_name, None)
            if value:
                setattr(converted, field_name, value)


def _validate_converted_tmdb_media(
    converted: object,
    *,
    expected_tmdb_id: str,
    expected_mtype: Optional[MediaType],
    episode_group: Optional[str],
    candidate_meta: MetaBase,
    tmdbinfo: Mapping[str, Any],
    origin_source: MediaSource,
    origin_id: str,
) -> Optional[MediaInfo]:
    """
    核验转换后的 TMDB 媒体对象与原目标在身份、类型、剧集组和电视剧季号上是否可靠一致。

    :param converted: TMDB 完整识别返回的对象
    :param expected_tmdb_id: 投影给出的目标 TMDB ID
    :param expected_mtype: 原媒体确定的影视类型；未限定时为 None
    :param episode_group: 请求保留的剧集组标识；未指定时为 None
    :param candidate_meta: 已补齐缺失季号的候选元数据
    :param tmdbinfo: 投影步骤返回的原始 TMDB 字典
    :param origin_source: 原媒体来源，用于告警日志定位
    :param origin_id: 原来源原生标识，用于告警日志定位
    :return: 通过可靠性校验的 TMDB 媒体对象；任一条件不满足时返回 None
    """
    if not isinstance(converted, MediaInfo):
        logger.warning(
            f"手动下载未能识别转换后的 TMDB {expected_tmdb_id}，保留原身份 {origin_source.value}:{origin_id}"
        )
        return None
    converted_source, converted_id = resolve_media_identity(media=converted)
    if converted_source != MediaSource.TMDB or converted_id != expected_tmdb_id:
        logger.warning(
            f"手动下载识别结果与目标 TMDB {expected_tmdb_id} 不一致，保留原身份 {origin_source.value}:{origin_id}"
        )
        return None
    if expected_mtype is not None and converted.type != expected_mtype:
        logger.warning(
            f"手动下载转换后的 TMDB {expected_tmdb_id} 类型（{converted.type}）与原目标（{expected_mtype}）冲突，"
            f"保留原身份 {origin_source.value}:{origin_id}"
        )
        return None
    if converted.type == MediaType.TV:
        if episode_group and converted.episode_group != episode_group:
            logger.warning(
                f"手动下载转换后的 TMDB {expected_tmdb_id} 未能应用剧集组 {episode_group}，"
                f"保留原身份 {origin_source.value}:{origin_id}"
            )
            return None
        requested_seasons = explicit_meta_seasons(candidate_meta)
        available_seasons = available_tmdb_seasons(converted, tmdbinfo)
        if available_seasons and requested_seasons:
            missing_seasons = [season for season in requested_seasons if season not in available_seasons]
            if missing_seasons:
                logger.warning(
                    f"手动下载转换后的 TMDB {expected_tmdb_id} 季列表未包含目标季 {missing_seasons}，"
                    f"保留原身份 {origin_source.value}:{origin_id}"
                )
                return None
    return converted


def _project_tmdb_payload(
    media_chain: Any,
    *,
    media_source: MediaSource,
    media_id: str,
    expected_mtype: Optional[MediaType],
    query_season: Optional[int],
) -> Optional[tuple[Mapping[str, Any], str, Optional[MediaType]]]:
    """
    调用共享跨来源投影获取 TMDB 标识与类型，失败或类型冲突时记录告警并返回 None。

    :param media_chain: 提供 ``convert_media_identity`` 的媒体链对象
    :param media_source: 已规范化的非 TMDB 媒体来源
    :param media_id: 已规范化的来源原生标识
    :param expected_mtype: 已确认的影视类型；未限定时为 None
    :param query_season: 传给投影接口的电视剧季号；电影或未指定时为 None
    :return: ``(原始投影字典, TMDB ID, 最终识别类型)``；无法可靠投影时返回 None
    """
    convert_identity = getattr(media_chain, "convert_media_identity", None)
    if not callable(convert_identity):
        return None
    convert_kwargs: dict[str, Any] = {
        "target_source": MediaSource.TMDB,
        "media_source": media_source,
        "media_id": media_id,
        "season": query_season,
    }
    if expected_mtype is not None:
        convert_kwargs["mtype"] = expected_mtype
    try:
        tmdbinfo = convert_identity(**convert_kwargs)
    except Exception as err:
        logger.warning(
            f"手动下载将 {media_source.value}:{media_id} 转换为 TMDB 主身份失败，保留原身份：{err}"
        )
        return None
    if not isinstance(tmdbinfo, dict):
        logger.warning(
            f"手动下载未能将 {media_source.value}:{media_id} 映射到 TMDB 主身份，保留原身份"
        )
        return None
    tmdb_id = tmdbinfo.get("id")
    if not tmdb_id or isinstance(tmdb_id, bool):
        logger.warning(
            f"手动下载转换 {media_source.value}:{media_id} 未返回有效 TMDB ID，保留原身份"
        )
        return None
    tmdb_mtype = _parse_tmdb_media_type(tmdbinfo.get("media_type"))
    if expected_mtype is not None and tmdb_mtype is not None and tmdb_mtype != expected_mtype:
        logger.warning(
            f"手动下载转换 {media_source.value}:{media_id} 类型冲突（原：{expected_mtype.value}，"
            f"映射：{tmdb_mtype.value}），保留原身份"
        )
        return None
    return tmdbinfo, str(tmdb_id), expected_mtype or tmdb_mtype


def convert_download_identity_to_tmdb(
    media_chain: Any,
    metainfo: MetaBase,
    media_source: MediaSource,
    media_id: str,
    *,
    source_media: Optional[MediaInfo] = None,
    mtype: Optional[MediaType] = None,
    episode_group: Optional[str] = None,
) -> Optional[tuple[MetaBase, MediaInfo, dict[str, JsonData]]]:
    """
    复用订阅同款跨来源映射将非 TMDB 影视下载目标转换为完整 TMDB 主身份。

    转换仅在命中有效 TMDB 映射、二次完整识别成功且通过类型、电视剧季号与剧集组核验时生效；
    任一步骤失败均保持传入的 ``metainfo`` 与 ``source_media`` 不受污染并返回 None。
    成功时会保留种子自身解析的季号与选集，并在返回对象与来源追溯字典中保留原来源信息。

    :param media_chain: 媒体链实例，需提供 ``convert_media_identity`` 与 ``recognize_media``
    :param metainfo: 种子标题解析出的元数据，转换失败时不会被修改
    :param media_source: 原媒体来源
    :param media_id: 原来源原生标识
    :param source_media: 已知的原媒体对象；无媒体上下文入口为 None
    :param mtype: 显式影视类型约束；省略时优先读取 ``source_media.type``
    :param episode_group: 显式剧集组标识；省略时回退到原媒体或元数据中的剧集组
    :return: ``(候选元数据, 完整 TMDB 媒体对象, 原来源追溯字典)``；不适用或不可靠时返回 None
    """
    normalized_source, normalized_id = resolve_media_identity(
        media_source=media_source,
        media_id=media_id,
    )
    if not normalized_source or not normalized_id or normalized_source == MediaSource.TMDB:
        return None
    expected_mtype = mtype or (
        source_media.type
        if source_media is not None and source_media.type in (MediaType.MOVIE, MediaType.TV)
        else None
    )
    if source_media is not None and expected_mtype not in (MediaType.MOVIE, MediaType.TV):
        return None
    effective_episode_group = (
        episode_group
        or (source_media.episode_group if source_media is not None else None)
        or getattr(metainfo, "episode_group", None)
    )
    query_season = _resolve_conversion_query_season(
        metainfo, normalized_source, source_media, expected_mtype,
    )
    projected = _project_tmdb_payload(
        media_chain,
        media_source=normalized_source,
        media_id=normalized_id,
        expected_mtype=expected_mtype,
        query_season=query_season,
    )
    if projected is None:
        return None
    tmdbinfo, tmdb_id, target_mtype = projected
    candidate_meta = deepcopy(metainfo)
    if target_mtype == MediaType.TV and not explicit_meta_seasons(candidate_meta):
        projected_season = _valid_season_number(tmdbinfo.get("season"))
        fallback_season = (
            projected_season
            if projected_season is not None
            else _extract_source_media_season(source_media)
        )
        if fallback_season is not None:
            candidate_meta.begin_season = fallback_season
            if getattr(candidate_meta, "total_season", 0) == 0:
                candidate_meta.total_season = 1
    recognize_kwargs: dict[str, Any] = {
        "meta": candidate_meta,
        "mtype": target_mtype,
        "media_source": MediaSource.TMDB,
        "media_id": tmdb_id,
    }
    if effective_episode_group:
        recognize_kwargs["episode_group"] = effective_episode_group
    try:
        raw_converted = media_chain.recognize_media(**recognize_kwargs)
    except Exception as err:
        logger.warning(
            f"手动下载识别转换后的 TMDB {tmdb_id} 失败，保留原身份 {normalized_source.value}:{normalized_id}：{err}"
        )
        return None
    converted = _validate_converted_tmdb_media(
        raw_converted,
        expected_tmdb_id=tmdb_id,
        expected_mtype=expected_mtype,
        episode_group=effective_episode_group,
        candidate_meta=candidate_meta,
        tmdbinfo=tmdbinfo,
        origin_source=normalized_source,
        origin_id=normalized_id,
    )
    if converted is None:
        return None
    _preserve_origin_media_fields(converted, normalized_source, normalized_id, source_media)
    origin_media: dict[str, JsonData] = {
        "media_source": normalized_source.value,
        "media_id": normalized_id,
    }
    logger.info(
        f"手动下载主身份已由 {normalized_source.value}:{normalized_id} 转换为 TMDB {tmdb_id}：{converted.title_year}"
    )
    return candidate_meta, converted, origin_media


@dataclass(frozen=True, slots=True)
class _PreparedDownload:
    """
    下载器调用前已经验证并规范化的本地提交事实。

    :param torrent: 已补齐来源凭据并规范化地址的种子信息
    :param media: 已识别的影视或音乐目标信息
    :param meta: 下载资源自身的标题解析元数据
    :param torrent_content: 可交给下载器的磁力链接、下载地址或种子字节内容
    :param folder_name: 种子内容对应的顶层目录名称
    :param file_list: 种子中待处理文件的相对路径列表
    :param download_dir: 最终下载保存 URI 的 Path 表示，保留既有下载器调用格式
    :param download_uri: 按目录配置解析出的最终下载保存地址
    :param download_episodes: 本次下载的集数范围文本，非剧集或无需限制时为 None
    :param site_downloader: 站点配置指定的下载器名称，未指定时为 None
    """

    torrent: TorrentInfo
    media: Union[MediaInfo, MusicInfo]
    meta: MetaBase
    torrent_content: Union[str, bytes]
    folder_name: str
    file_list: list[str]
    download_dir: Path
    download_uri: str
    download_episodes: Optional[str]
    site_downloader: Optional[str]


class _DownloadResourceOwner(_DownloadOwnerBase):
    """
    种子获取、间接地址解析与资源下载事件处理。
    """

    @staticmethod
    def _credential_origin(url: Optional[str]) -> Optional[tuple[str, str, int]]:
        """
        仅返回无内嵌账号的 HTTP 来源，避免按宽泛域名向其他地址补发凭据。
        """
        try:
            parts = urlparse(url or "")
            if (parts.scheme not in ("http", "https") or not parts.hostname
                    or parts.username is not None or parts.password is not None):
                return None
            port = parts.port if parts.port is not None else (443 if parts.scheme == "https" else 80)
            return parts.scheme, parts.hostname, port
        except ValueError:
            return None

    def _with_site_credentials(self, torrent: TorrentInfo) -> TorrentInfo:
        """
        为同来源的直接下载补齐 Cookie 和缺失 UA，不改写调用方对象或换票协议。
        """
        if torrent.site_cookie:
            return torrent
        origin = self._credential_origin(torrent.enclosure)
        if origin is None:
            return torrent
        try:
            if torrent.site is not None:
                site = self.site_repository.get(torrent.site)
            else:
                matches = [
                    item for item in self.site_repository.list()
                    if self._credential_origin(item.url) == origin
                ]
                site = matches[0] if len(matches) == 1 else None
        except Exception as err:
            # 查询异常可能包含数据库连接信息，只记录类型并保留既有下载行为。
            logger.warning(f"读取种子下载站点凭据失败：{type(err).__name__}")
            return torrent
        if not site or not site.cookie or self._credential_origin(site.url) != origin:
            return torrent
        enriched = copy(torrent)
        enriched.site_cookie = site.cookie
        if not torrent.site_ua and site.ua:
            enriched.site_ua = site.ua
        return enriched

    def _resolve_indirect_download_url(
            self,
            url: str,
            ua: Optional[str] = None,
            cookie: Optional[str] = None,
            site_id: Optional[int] = None,
    ) -> str:
        """
        解析索引器编码的换票请求，并返回可信来源的临时下载地址。

        :param url: 索引器编码的换票地址与请求参数
        :param ua: 本次换票使用的用户代理
        :param cookie: 换票协议允许时附带的站点 Cookie
        :param site_id: 原始资源的站点 ID，用于共享流控额度
        :return: 临时下载地址；失败原因通过异常交回种子准备流程
        :raises RateLimitExceededException: 站点限流，保留原始提示供通知和冷却记录使用
        :raises ValueError: 配方、换票响应或下载地址无效，错误使用“换票失败”分类
        """
        match = re.search(r"\[(.*)](.*)", url)
        if not match:
            raise ValueError("换票失败：请求编码无效")
        encoded_params, request_url = match.groups()
        if not encoded_params:
            return request_url
        try:
            request_text = base64.b64decode(encoded_params.encode("utf-8")).decode("utf-8")
            request_params = json.loads(request_text)
        except (ValueError, UnicodeError) as err:
            # 编码内容包含认证信息，不能把原始配方放进错误消息。
            raise ValueError("换票失败：无法解析请求编码") from err
        if not isinstance(request_params, dict):
            raise ValueError("换票失败：请求参数格式无效")
        original_cookie = cookie
        if not request_params.get("cookie"):
            cookie = None
        headers_value = request_params.get("header")
        headers = (
            cast(dict[str, str], headers_value)
            if isinstance(headers_value, dict)
            else None
        )
        params_value = request_params.get("params")
        params = (
            cast(dict[str, Any], params_value)
            if isinstance(params_value, dict)
            else None
        )
        http, _ = _download_ports_snapshot()
        config = get_chain_runtime_config_snapshot()
        effective_ua = site_rules.resolve_site_user_agent(
            ua,
            getattr(config, "site_user_agent", None),
            config.user_agent,
        ) or None
        proxies = config.proxy if request_params.get("proxy") else None
        site = site_request_config(request_url, site_id)
        wait_for_site_request(site)
        if request_params.get("method") == "get":
            response = http.get(
                request_url,
                ua=effective_ua,
                cookies=cookie,
                headers=headers,
                proxies=proxies,
                params=params,
            )
        else:
            post_options = {key: request_params[name] for name, key in
                            (("json", "json_body"), ("allow_redirects", "allow_redirects")) if name in request_params}
            response = http.post(
                request_url,
                ua=effective_ua,
                cookies=cookie,
                headers=headers,
                proxies=proxies,
                params=params,
                **post_options,
            )
        try:
            return self._indirect_download_response(response, request_params, site, original_cookie)
        finally:
            if response is not None:
                _close_download_response(response)

    def _indirect_download_response(self, response: Optional[DownloadResponsePort], request_params: Dict[str, Any],
                                    site: dict[str, object], cookie: Optional[str] = None) -> str:
        """
        在响应关闭前统一交给 Module 解释新旧配方；限流由共享冷却负责。

        :param response: 尚未关闭的原始换票响应，None 表示连接失败
        :param request_params: 索引器编码的换票协议及结果字段
        :param site: 原始站点的不含凭据流控配置
        :param cookie: 换票协议筛选前的 Cookie，仅用于错误脱敏和是否发送的诊断
        :return: 适配器解释并通过格式校验的下载地址
        :raises ValueError: 适配器拒绝响应、能力不可用或没有有效地址，保留脱敏后的响应摘要
        :raises RateLimitExceededException: 任一协议返回明确限流
        """
        try:
            payload = response.json() if response is not None else None
        except ValueError:
            payload = None
        try:
            url = self.run_module(
                "parse_torrent_download_response", request_params=request_params,
                status_code=response.status_code if response is not None else None,
                payload=payload, response_text=response.text if response is not None else None, raise_exception=True,
            )
            if request_params.get("protocol") and (not isinstance(url, str) or not url):
                raise ValueError("站点换票响应解析器不可用")
        except ValueError as err:
            if torrent_rules.rate_limit_cooldown(str(err)) is not None:
                defer_site_request(site, str(err))
                raise RateLimitExceededException(str(err)) from err
            raise ValueError(self._indirect_download_failure_reason(
                response, payload, request_params, cookie, str(err),
            )) from err
        if error := torrent_rate_limit_error(response):
            defer_site_request(site, error)
            raise RateLimitExceededException(error)
        normalized_url = UrlUtils.normalize_http_url(url)
        if isinstance(url, str) and url.startswith("magnet:"):
            return url
        if normalized_url is None:
            raise ValueError(self._indirect_download_failure_reason(
                response, payload, request_params, cookie, "接口未返回有效下载地址",
            ))
        return normalized_url

    @staticmethod
    def _redact_indirect_download_error(text: str, request_params: Dict[str, Any], cookie: Optional[str]) -> str:
        """
        移除换票错误中的凭据回显及完整签名地址，再限制通知和日志长度。

        :param text: 适配器原因或响应的有限错误字段，不接受整份业务载荷
        :param request_params: 当前配方，请求头中的认证值仅用于脱敏
        :param cookie: 调用方 Cookie，协议禁止发送时同样不能出现在诊断中
        :return: 不含已知凭据和 URL 路径、查询串的单行错误摘要
        """
        headers = request_params.get("header")
        secrets = list(headers.values()) if isinstance(headers, dict) else []
        if cookie:
            secrets.extend([cookie, *(part.partition("=")[2].strip() for part in cookie.split(";"))])
        for secret in secrets:
            if isinstance(secret, str) and secret:
                text = text.replace(secret, "[已隐藏]")
                text = text.replace(secret.removeprefix("Bearer "), "[已隐藏]")
        text = re.sub(r"https?://[^\s<>\"'，；（）]+", lambda match: url_origin_for_log(match.group()), text)
        text = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer [已隐藏]", text)
        text = re.sub(
            r"(?i)\b(token|passkey|api[-_]?key|cookie|password|secret|sign)\s*[:=]\s*[^\s,;]+",
            r"\1=[已隐藏]", text,
        )
        return " ".join(text.split())[:500]

    @classmethod
    def _indirect_download_failure_reason(
            cls, response: Optional[DownloadResponsePort], payload: object,
            request_params: Dict[str, Any], cookie: Optional[str], reason: str,
    ) -> str:
        """
        为旧配方的空结果及适配器拒绝补齐换票阶段和 HTTP 诊断，不解释专站业务码。

        只摘取错误字段，不回显 data、完整页面或下载票据；明确限流在调用方优先处理。

        :param response: 尚未关闭的换票响应；None 表示连接失败或超时
        :param payload: 索引模块使用的 JSON 载荷，非 JSON 时为 None
        :param request_params: 当前请求配方，用于凭据脱敏
        :param cookie: 协议筛选前的 Cookie，用于脱敏及发送状态判断
        :param reason: 适配器的拒绝原因或通用的地址缺失说明
        :return: 可传到日志、通知和瞬时冷却台账的失败说明
        """
        headers = request_params.get("header")
        header_cookie = isinstance(headers, dict) and any(str(key).lower() == "cookie" and value
                                                         for key, value in headers.items())
        cookie_sent = bool((cookie and request_params.get("cookie")) or header_cookie)
        details = ["未收到响应（连接失败或超时）"] if response is None else [f"HTTP {response.status_code}"]
        if response is not None:
            fields = [f"{key}={payload[key]}" for key in ("code", "message", "msg", "error")
                      if isinstance(payload, dict) and isinstance(payload.get(key), (str, int))]
            summary = "；".join(fields) or ("空响应" if not response.content else "响应未包含有效下载地址")
            details.append(f"响应摘要：{summary}")
        details.append(f"携带 Cookie：{'是' if cookie_sent else '否'}")
        error = cls._redact_indirect_download_error(f"{reason}（{'；'.join(details)}）", request_params, cookie)
        return f"换票失败：{error}"

    def download_torrent(self, torrent: TorrentInfo,
                         channel: Optional[NotificationChannel] = None,
                         source: Optional[str] = None,
                         userid: Union[str, int, None] = None,
                         on_failure: Optional[Callable[[str], None]] = None,
                         ) -> Tuple[Optional[Union[str, bytes]], str, list[str]]:
        """
        下载种子文件，如果是磁力链，会返回磁力链接本身
        换票种子按站点资源和原始换票凭据复用本地内容，命中后不再请求临时链接。

        :param on_failure: 将本次具体失败原因交还准备流程，保持返回元组与并发调用隔离
        :return: 种子内容，种子目录名，种子文件清单
        """
        enclosure = torrent.enclosure
        if not enclosure:
            return None, "", []
        if enclosure.startswith("magnet:"):
            return enclosure, "", []
        helper: TorrentHelper = cast(Any, TorrentHelper)()
        if helper.is_invalid(enclosure):
            if on_failure:
                on_failure("种子已加入下载失败黑名单（24小时）")
            logger.info(f"{torrent.title} 命中下载失败黑名单，跳过下载")
            return None, "", []
        torrent = self._with_site_credentials(torrent)
        result = self._get_torrent_content(torrent)
        if result is None:
            reason = "换票失败：未获取到下载地址" if enclosure.startswith("[") else "无法获取下载地址"
            result = None, "", [], reason
        content, download_folder, files, error_msg = result

        if isinstance(content, str):
            return content, "", []

        if not content:
            if on_failure:
                on_failure(error_msg or "下载种子内容为空")
            if not enclosure.startswith("[") and error_msg and "种子数据有误" in error_msg:
                helper.add_invalid(enclosure)
            logger.error(f"下载种子文件失败：{torrent.title}，{error_msg}")
            self.post_message(Message(
                channel=channel,
                source=source if channel else None,
                mtype=MessageType.Manual,
                title=f"{torrent.title} 种子下载失败！",
                text=f"错误信息：{error_msg}\n站点：{torrent.site_name}",
                userid=userid))
            return None, "", []

        return content, download_folder or "", files or []

    @staticmethod
    def _indirect_torrent_cache_key(torrent: TorrentInfo) -> Optional[str]:
        """
        以资源身份和换票凭据隔离缓存，临时链接不参与键，凭据不写入文件名。
        """
        identity = torrent_resource_identity(torrent)
        if not torrent.enclosure or not torrent.enclosure.startswith("[") or identity is None:
            return None
        scope = json.dumps((identity, torrent.enclosure, torrent.site_cookie))
        return f"resource-{hashlib.sha256(scope.encode('utf-8')).hexdigest()}.torrent"

    def _get_torrent_content(self, torrent: TorrentInfo) -> Optional[Tuple[
        Optional[Union[str, bytes]], Optional[str], Optional[list[str]], Optional[str],
    ]]:
        """
        先读取已验证的换票种子内容；缓存损坏时重新换票，失败与磁力链不缓存。

        :param torrent: 含原始资源地址和站点认证上下文的候选
        :return: 内容、目录、文件列表及失败原因；换票失败不作为资源损坏处理
        """
        if not torrent.enclosure:
            return None
        cache_key = self._indirect_torrent_cache_key(torrent)
        cache = FileCache(local_only=True) if cache_key else None
        if cache is not None and cache_key is not None:
            cached = cache.get(cache_key, region="torrents")
            if isinstance(cached, bytes) and cached:
                helper: TorrentHelper = cast(Any, TorrentHelper)()
                cached_folder, cached_files = helper.get_fileinfo_from_torrent_content(cached)
                if cached_files:
                    return cached, cached_folder, cached_files, ""
                cache.delete(cache_key, region="torrents")
        site_cookie: Optional[str] = torrent.site_cookie
        indirect_download = torrent.enclosure.startswith("[")
        if indirect_download:
            try:
                torrent_url = self._resolve_indirect_download_url(
                    url=torrent.enclosure,
                    ua=torrent.site_ua,
                    cookie=site_cookie,
                    site_id=torrent.site,
                )
            except RateLimitExceededException as err:
                return None, "", [], str(err)
            except ValueError as err:
                reason = str(err)
                return None, "", [], reason if reason.startswith("换票失败：") else f"换票失败：{reason}"
            # 涉及解析地址的不使用Cookie下载种子，否则MT会出错
            site_cookie = None
        else:
            torrent_url = torrent.enclosure
        if not torrent_url:
            reason = "换票失败：未获取到下载地址" if indirect_download else "无法获取下载地址"
            return None, "", [], reason
        _, content, download_folder, files, error_msg = self._download_torrent_content(
            torrent, torrent_url, site_cookie, indirect_download
        )
        if indirect_download and not content and torrent_rules.rate_limit_cooldown(error_msg) is None:
            # 临时票据的错误页或失效不证明原种子损坏，下一次调用仍应重新换票。
            reason = "返回内容不是有效种子，请重新换票" if "种子数据有误" in (error_msg or "") else (error_msg or "")
            error_msg = f"临时下载地址失效：{reason or '未下载到种子数据'}"

        if cache is not None and cache_key is not None and isinstance(content, bytes) and files:
            cache.set(cache_key, content, region="torrents")
        return content, download_folder, files, error_msg

    def _download_torrent_content(
            self,
            torrent: TorrentInfo,
            url: str,
            cookie: Optional[str],
            indirect_download: bool,
    ) -> Tuple[Optional[Path], Optional[Union[str, bytes]], Optional[str], Optional[list[str]], Optional[str]]:
        """
        短时签名失效时刷新同一资源并重试一次，最终失败再交由调用方冷却。
        """
        helper: TorrentHelper = cast(Any, TorrentHelper)()
        for attempt in range(2):
            expiring = is_expiring_download_url(url)
            result = helper.download_torrent(
                url=url,
                cookie=cookie,
                ua=site_rules.resolve_site_user_agent(
                    torrent.site_ua,
                    getattr(self.runtime_config, "site_user_agent", None),
                    self.runtime_config.user_agent,
                ),
                proxy=torrent.site_proxy,
                cache_invalid=not (indirect_download or expiring),
                site_id=torrent.site,
            )
            if result[1] or attempt or indirect_download or not expiring:
                return result
            if not re.fullmatch(r"下载种子出错，状态码：(401|403|404|410)", result[4] or ""):
                return result
            if not self._refresh_signed_torrent(torrent):
                return result
            if not torrent.enclosure:
                return result
            url, cookie = torrent.enclosure, torrent.site_cookie
        return result

    def _refresh_signed_torrent(self, torrent: TorrentInfo) -> bool:
        """
        复用站点索引器查找同站同 ID 的新地址，不允许用同标题的其他资源替换。
        """
        identity = torrent_resource_identity(torrent)
        if not torrent.site or not torrent.title or identity is None:
            return False
        try:
            site = next((item for item in SitesHelper().get_indexers() if item.get("id") == torrent.site), None)
            if not site:
                return False
            for candidate in self.search_site_torrents(site=site, keyword=torrent.title):
                if (torrent_resource_identity(candidate) != identity
                        or not candidate.enclosure
                        or candidate.enclosure == torrent.enclosure
                        or not candidate.enclosure.startswith(("http://", "https://"))):
                    continue
                for name in ("enclosure", "page_url", "site_cookie", "site_ua", "site_proxy"):
                    setattr(torrent, name, getattr(candidate, name))
                logger.info(f"{torrent.title} 已刷新临时下载链接，重试下载")
                return True
        except Exception as err:
            # 索引器异常可能携带签名 URL，不把异常正文写入日志或通知。
            logger.warning(f"{torrent.title} 刷新临时下载链接失败：{type(err).__name__}")
        return False

    @staticmethod
    def _apply_resource_download_event(
            context: Context,
            episodes: Optional[Set[int]],
            channel: Optional[NotificationChannel],
            source: Optional[str],
            downloader: Optional[str],
            save_path: Optional[str],
            userid: Union[str, int, None],
            username: Optional[str],
    ) -> tuple[Optional[str], Optional[str]]:
        """
        应用资源下载事件覆盖，并校验事件返回的下载目录。
        """
        # 空白保存目录表示使用默认下载目录，事件订阅方也只会看到 None。
        save_path = specified_download_save_path(save_path)
        meta = context.meta_info
        media = context.media_info
        event_data = ResourceDownloadEventData(
            context=context,
            episodes=set(episodes or (meta.episode_list if meta else [])),
            channel=channel,
            origin=source,
            downloader=downloader,
            options={
                "save_path": save_path,
                "userid": userid,
                "username": username,
                "media_category": media.category if media else None,
            },
        )
        event = eventmanager.send_event(ChainEventType.ResourceDownload, event_data)
        if event and event.event_data:
            event_data = cast(ResourceDownloadEventData, event.event_data)
            if event_data.cancel:
                logger.debug(
                    "Resource download canceled by event: %s,Reason: %s",
                    event_data.source,
                    event_data.reason,
                )
                return save_path, "下载被事件取消"
            if event_data.options and "save_path" in event_data.options:
                save_path = specified_download_save_path(
                    cast(Optional[str], event_data.options.get("save_path"))
                )
        if save_path is None:
            return None, None
        try:
            return validate_download_save_path(save_path), None
        except ValueError as err:
            logger.warn(str(err))
            return save_path, str(err)


class DownloadSubmissionOwner(_DownloadResourceOwner):
    """
    单任务下载准备、提交与结算 owner。
    """

    @staticmethod
    def _subscription_download_cancelled(
        governance: Optional[SubscriptionDownloadGovernance],
    ) -> bool:
        """
        在下载器副作用前读取订阅执行上下文的停止信号。
        """
        return bool(governance and governance.cancelled and governance.cancelled())

    def download_single(self, context: Context,
                        torrent_file: Optional[Path] = None,
                        torrent_content: Optional[Union[str, bytes]] = None,
                        episodes: Optional[Set[int]] = None,
                        channel: Optional[NotificationChannel] = None,
                        source: Optional[str] = None,
                        downloader: Optional[str] = None,
                        save_path: Optional[str] = None,
                        userid: Union[str, int, None] = None,
                        username: Optional[str] = None,
                        label: Optional[str] = None,
                        return_detail: bool = False,
                        custom_words: Optional[str] = None,
                        governance: Optional[SubscriptionDownloadGovernance] = None,
                        *,
                        user_specified: bool = False,
                        ) -> Union[Optional[str], Tuple[Optional[str], Optional[str]]]:
        """
        下载单个资源并发送结果通知。

        保持下载链、消息入口和插件使用的公开签名，实际流程委托给内部执行阶段。

        :param user_specified: 用户明确指定了这个种子（Web 界面、接口、智能助手，或消息交互中选中了具体资源）。
            同一种子已在下载器中、本次目标文件之前被取消选择时，按用户这次的明确要求重新选中；
            自动流程（订阅、工作流、插件）保持默认值 False，尊重之前的取消
        """
        return self._execute_download_single(
            context=context,
            torrent_file=torrent_file,
            torrent_content=torrent_content,
            episodes=episodes,
            channel=channel,
            source=source,
            downloader=downloader,
            save_path=save_path,
            userid=userid,
            username=username,
            label=label,
            return_detail=return_detail,
            custom_words=custom_words,
            governance=governance,
            user_specified=user_specified,
        )

    def _execute_download_single(
        self,
        context: Context,
        torrent_file: Optional[Path] = None,
        torrent_content: Optional[Union[str, bytes]] = None,
        episodes: Optional[Set[int]] = None,
        channel: Optional[NotificationChannel] = None,
        source: Optional[str] = None,
        downloader: Optional[str] = None,
        save_path: Optional[str] = None,
        userid: Union[str, int, None] = None,
        username: Optional[str] = None,
        label: Optional[str] = None,
        return_detail: bool = False,
        custom_words: Optional[str] = None,
        governance: Optional[SubscriptionDownloadGovernance] = None,
        user_specified: bool = False,
    ) -> Union[Optional[str], Tuple[Optional[str], Optional[str]]]:
        """
        准备下载事实，提交下载器并按订阅治理合同结算结果。

        :param user_specified: 用户明确指定了这个种子，见 ``download_single``
        """
        prepared, error_msg = self._prepare_download_single(
            context=context,
            torrent_file=torrent_file,
            torrent_content=torrent_content,
            episodes=episodes,
            channel=channel,
            source=source,
            downloader=downloader,
            save_path=save_path,
            userid=userid,
            username=username,
        )
        if prepared is None:
            if governance:
                governance.report(context, "rejected", "资源准备失败或未满足下载提交条件")
            return (None, error_msg) if return_detail else None
        if self._has_completed_music_download(prepared, source):
            reason = "音乐资源的全部音频已成功整理，跳过重复提交"
            logger.info(f"{prepared.torrent.title} {reason}")
            if governance:
                governance.report(context, "rejected", reason)
            return (None, reason) if return_detail else None
        download_hash, error_msg = self._submit_prepared_download(
            prepared=prepared,
            context=context,
            episodes=episodes,
            channel=channel,
            source=source,
            downloader=downloader,
            userid=userid,
            username=username,
            label=label,
            custom_words=custom_words,
            governance=governance,
            user_specified=user_specified,
        )
        if governance:
            if download_hash:
                governance.report(context, "submitted", None)
            elif is_unconfirmed_download_message(error_msg):
                governance.report(
                    context,
                    "failed",
                    "下载器已受理但暂未确认任务信息，已保留待确认状态并暂停同目标换源",
                )
            else:
                governance.report(context, "failed", "下载器未接受该资源，未提交成功")
        return (download_hash, error_msg) if return_detail else download_hash

    def _has_completed_music_download(self, prepared: _PreparedDownload, source: Optional[str]) -> bool:
        """
        按真实 hash 核验整包音频是否已成功整理交付；失败或部分整理可重试。

        交付事实来自整理历史本身：单曲被移出媒体库不再触发整包重投，仅当全部整理
        目标都已消失（专辑删除或整体迁移）时才视为交付失效。
        """
        if prepared.media.type != MediaType.MUSIC or not self._is_subscribe_source(source):
            return False
        download_hash = cast(Any, TorrentHelper)().get_torrent_hash(prepared.torrent_content)
        if not download_hash:
            return False
        history = self.download_history_repository.get_by_hash(download_hash)
        if not history or resolve_media_identity(media=history) != resolve_media_identity(media=prepared.media):
            return False
        files = self.download_history_repository.get_files_by_hash(download_hash)
        extensions = get_chain_runtime_config_snapshot().audio_extensions
        audio_files = [FileURI.from_uri(item.fullpath) for item in files
                       if item.fullpath and Path(item.fullpath).suffix.lower() in extensions]
        audio_paths = {(item.storage, item.path) for item in audio_files}
        if not audio_paths:
            return False
        delivered = self._music_delivered_audio_keys(download_hash, history.downloader)
        if not delivered:
            return False
        retained = sum(1 for alive in delivered.values() if alive)
        if not retained:
            logger.info(f"{prepared.torrent.title} 媒体库已无该批整理目标，视为交付失效并允许重新提交")
            return False
        if not audio_paths.issubset(delivered):
            return False
        if retained < len(delivered):
            # 下载器对同一 hash 只会复用任务，重投整包补不回被删单曲，只会重复记录历史。
            logger.info(
                f"{prepared.torrent.title} 媒体库缺失 {len(delivered) - retained} 首已整理曲目，沿用历史交付不重投整包"
            )
        return True

    def _music_delivered_audio_keys(
            self,
            download_hash: str,
            expected_downloader: Optional[str],
    ) -> Dict[Tuple[str, str], bool]:
        """
        按整理历史汇总该种子音频的交付事实，并标记每条目标当前是否仍在媒体库。

        :param download_hash: 种子真实 hash
        :param expected_downloader: 下载历史记录的下载器，用于排除跨下载器的历史记录
        :return: 下载源路径标识到“整理目标是否仍存在”的映射
        """
        delivered: Dict[Tuple[str, str], bool] = {}
        for item in self.transfer_history_repository.list_by_hash(download_hash):
            if not (item.status and item.dest
                    and item.type == MediaType.MUSIC.value
                    and item.downloader == expected_downloader
                    and item.dest_storage == "local"):
                continue
            # 同一曲目可能有多条整理记录，任一目标仍在媒体库即视为已交付。
            key = (item.src_storage or "local", str(item.src or ""))
            delivered[key] = delivered.get(key, False) or Path(item.dest).is_file()
        return delivered

    @staticmethod
    def _sync_context_after_download_event(
        context: Context,
        torrent: TorrentInfo,
    ) -> Optional[tuple[TorrentInfo, Union[MediaInfo, MusicInfo], MetaBase]]:
        """
        在 ``ResourceDownload`` 事件返回后重新读取上下文，保证插件替换后的媒体与元数据生效。

        :param context: 已派发过 ``ResourceDownload`` 事件的下载上下文
        :param torrent: 事件派发前的种子对象，用作 ``context.torrent_info`` 未修改时的回退
        :return: 同步后的 ``(种子, 媒体, 元数据)``；上下文缺失有效媒体或元数据时返回 None
        """
        media, meta = context.media_info, context.meta_info
        if not isinstance(media, (MediaInfo, MusicInfo)) or meta is None:
            return None
        updated_torrent = context.torrent_info if context.torrent_info is not None else torrent
        return updated_torrent, media, meta

    def _prepare_download_single(
        self,
        *,
        context: Context,
        torrent_file: Optional[Path],
        torrent_content: Optional[Union[str, bytes]],
        episodes: Optional[Set[int]],
        channel: Optional[NotificationChannel],
        source: Optional[str],
        downloader: Optional[str],
        save_path: Optional[str],
        userid: Union[str, int, None],
        username: Optional[str],
    ) -> tuple[Optional[_PreparedDownload], Optional[str]]:
        """
        补全媒体、读取种子并解析出可信下载目录。
        """
        torrent, media, meta = context.torrent_info, context.media_info, context.meta_info
        if torrent is None or media is None or meta is None:
            return None, "下载上下文缺少媒体、元数据或种子信息"
        supplemented_media = MediaChain().supplement_tmdb_info(media, meta)
        if not isinstance(supplemented_media, (MediaInfo, MusicInfo)):
            return None, "媒体信息补全失败"
        media = supplemented_media
        context.media_info = media
        save_path, event_error = self._apply_resource_download_event(
            context, episodes, channel, source, downloader, save_path, userid, username
        )
        if event_error:
            return None, event_error
        synced = self._sync_context_after_download_event(context, torrent)
        if synced is None:
            return None, "下载上下文缺少媒体或元数据信息"
        torrent, media, meta = synced
        site_downloader = torrent.site_downloader
        download_episodes = episode_rules.format_ranges(list(episodes)) if episodes else None
        if episodes is not None:
            context.selected_episodes = sorted(set(episodes))
        elif meta.episode_list:
            context.selected_episodes = sorted(set(meta.episode_list))
        else:
            context.selected_episodes = []
        download_error = "下载种子内容为空"

        def remember_failure(error_msg: str) -> None:
            """
            保留真实失败原因，使无效种子和限流分别使用正确的资源冷却。
            """
            nonlocal download_error
            download_error = error_msg

        if not torrent_file and not torrent_content:
            torrent_content, _, _ = self.download_torrent(
                torrent, channel=channel, source=source, userid=userid,
                on_failure=remember_failure,
            )
        elif torrent_file:
            torrent_content = (
                torrent_file.read_bytes()
                if torrent_file.exists()
                # 与 TorrentHelper.download_torrent 的写入路由一致，种子只缓存在本地文件系统
                else FileCache(local_only=True).get(
                    torrent_file.as_posix(), region="torrents"
                )
            )
        if not torrent_content:
            self._record_download_failure(
                context=context,
                error_msg=download_error,
                downloader=downloader or site_downloader,
                source=source,
                episodes=episodes,
            )
            return None, download_error
        folder_name, file_list = cast(Any, TorrentHelper)().get_fileinfo_from_torrent_content(
            torrent_content
        )
        self._record_music_album_track_keys(context, file_list)
        storage, download_dir, error_msg = self._resolve_media_download_dir(
            media_info=media,
            save_path=save_path,
        )
        if not download_dir or not storage:
            if error_msg == "未找到下载目录":
                self.messagehelper.put(
                    f"{(media.type.value if media.type else "")} {media.title_year} 未找到下载目录！",
                    title="下载失败",
                    role="system",
                    level="error",
                )
            return None, error_msg or "未找到下载目录"
        download_uri = FileURI(storage=storage, path=download_dir.as_posix()).uri
        return _PreparedDownload(
            torrent=torrent,
            media=media,
            meta=meta,
            torrent_content=torrent_content,
            folder_name=folder_name,
            file_list=file_list,
            download_dir=Path(download_uri),
            download_uri=download_uri,
            download_episodes=download_episodes,
            site_downloader=site_downloader,
        ), None

    def _submit_prepared_download(
        self,
        *,
        prepared: _PreparedDownload,
        context: Context,
        episodes: Optional[Set[int]],
        channel: Optional[NotificationChannel],
        source: Optional[str],
        downloader: Optional[str],
        userid: Union[str, int, None],
        username: Optional[str],
        label: Optional[str],
        custom_words: Optional[str],
        governance: Optional[SubscriptionDownloadGovernance],
        user_specified: bool = False,
    ) -> tuple[Optional[str], Optional[str]]:
        """
        在可取消边界后调用下载器，并分派成功或拒绝结算。

        取消检查做两次：入口先检查一次，尽早退出；取得任务锁后、调用下载器前再检查一次，并在那里才标记
        "开始产生下载副作用"。等锁期间（同一任务上的整理回写、rTorrent 新建任务的临时标签轮询等可能持锁
        数十秒）订阅可能已被取消，只在入口检查的话，锁一释放仍会提交下载器并结算。

        :param user_specified: 用户明确指定了这个种子，交给下载器：同一 hash 已存在时重新选中之前被取消的目标，
            任务已暂停且目标未下完时恢复任务；拒绝时每次都通知，不做通知去重
        """
        if self._subscription_download_cancelled(governance):
            return None, "订阅下载在提交前已取消"
        # 非订阅入口清除复用上下文上的旧标记；订阅来源在插件处理与选集之后冻结。
        context.subscription_download = None
        if governance and governance.prepare_submission:
            governance.prepare_submission(context)
        submission_label = subscription_download_label(context.subscription_download)
        if submission_label:
            logger.info(f"开始下载【{submission_label}】{prepared.torrent.title} ...")
        category = prepared.media.category
        # 音乐尚无目录分类时仍提供稳定的下载器分类，不回写媒体的目录或元数据分类。
        if not category and prepared.media.type == MediaType.MUSIC:
            category = MediaType.MUSIC.value
        task_hashes = torrent_rules.download_hashes(prepared.torrent_content)
        # 读取确认记录 → 下载器受理 → 写入下载历史整段持有任务锁，见 downloader_task_locks：
        # 已有任务上的追加与整理链回写、删除、暂停开始互斥，确认记录的读取、合并与写回也不会被并发提交打断。
        result: Optional[Tuple[Optional[str], Optional[str], Optional[str], str]]
        with downloader_task_locks.hold(task_hashes):
            try:
                previous_selection = self._existing_task_confirmed_selection(task_hashes)
            except Exception as err:
                # 读不到历史时不能当成"没有历史"继续：受理后写回的确认记录只会剩下本次的文件，之前被取消的
                # 文件从此不再受保护。数据库读不了时写历史同样会失败，直接按瞬时失败稍后重试，不调用下载器。
                logger.warning(f"读取下载历史中的已确认选集失败：{err}")
                previous_selection, result = None, (
                    None, None, None, f"读取下载历史失败，未提交下载器，稍后重试：{err}",
                )
            else:
                if self._subscription_download_cancelled(governance):
                    return None, "订阅下载在提交前已取消"
                if governance and governance.mark_started:
                    governance.mark_started()
                result = self.download(
                    content=prepared.torrent_content,
                    cookie=prepared.torrent.site_cookie,
                    episodes=cast(Set[int], episodes),
                    download_dir=prepared.download_dir,
                    category=category,
                    label=label,
                    downloader=downloader or prepared.site_downloader,
                    custom_words=custom_words,
                    episode_selection=torrent_rules.episode_selection_for_context(context) if episodes else None,
                    confirmed_selection=previous_selection,
                    user_specified=user_specified,
                )
            missing_downloader = not result
            actual_downloader, download_hash, layout, error_msg = (
                result
                if result
                else (
                    None,
                    None,
                    None,
                    self._missing_downloader_error(
                        downloader,
                        prepared,
                        user_specified=user_specified,
                        diagnostic=self._missing_downloader_diagnostic(
                            downloader,
                            prepared,
                        ),
                    ),
                )
            )
            if download_hash:
                self._settle_accepted_download(
                    prepared=prepared,
                    context=context,
                    episodes=episodes,
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    custom_words=custom_words,
                    actual_downloader=actual_downloader,
                    download_hash=download_hash,
                    layout=layout,
                    task_action=DownloadTaskAction.from_downloader_message(error_msg),
                    previous_selection=previous_selection,
                    accepted_files=self._accepted_task_files(download_hash, actual_downloader),
                    downloader_scope=self._download_scope(downloader, prepared.torrent),
                )
        if not download_hash:
            if is_unconfirmed_download_message(error_msg):
                self._record_unconfirmed_download(
                    prepared=prepared,
                    context=context,
                    episodes=episodes,
                    channel=channel,
                    source=source,
                    downloader=downloader,
                    userid=userid,
                    username=username,
                    custom_words=custom_words,
                    actual_downloader=actual_downloader,
                    error_msg=error_msg,
                    user_specified=user_specified,
                )
            else:
                self._record_rejected_download(
                    prepared=prepared,
                    context=context,
                    episodes=episodes,
                    channel=channel,
                    source=source,
                    downloader=downloader,
                    userid=userid,
                    actual_downloader=actual_downloader,
                    error_msg=error_msg,
                    user_specified=user_specified,
                    record_failure=not missing_downloader,
                )
        return download_hash, error_msg

    def _missing_downloader_diagnostic(
        self,
        downloader: Optional[str],
        prepared: _PreparedDownload,
    ) -> Optional[str]:
        """
        读取本地下载器模块健康快照，为实例缺失错误补充初始化或热重载失败线索。

        仅读取运行态缓存，不触发下载器网络请求或模块物化。

        :param downloader: 调用方或订阅显式传入的下载器名称
        :param prepared: 提交前已规范化的下载事实
        :return: 可附加到错误提示后的诊断说明；模块状态正常或未装配管理器时返回 None
        """
        manager = getattr(self, "modulemanager", None)
        get_issues = getattr(manager, "get_unhealthy_module_issues", None)
        if not callable(get_issues):
            return None
        issues = get_issues(ModuleType.Downloader)
        if not isinstance(issues, list) or not issues:
            return None
        target = downloader or prepared.site_downloader
        if target:
            for issue in issues:
                init_errors = issue.get("init_errors")
                if isinstance(init_errors, Mapping) and target in init_errors:
                    return f"实例初始化失败：{init_errors[target]}"
        unavailable = [
            f"{issue.get('name') or issue.get('id')}（{issue.get('error') or issue.get('lifecycle')}）"
            for issue in issues
            if issue.get("lifecycle") != "degraded"
        ]
        if unavailable:
            return f"下载器模块未就绪：{'；'.join(unavailable)}"
        if not target:
            degraded = [
                str(issue.get("error"))
                for issue in issues
                if issue.get("error")
            ]
            if degraded:
                return f"部分下载器实例初始化失败：{'；'.join(degraded)}"
        return None

    @staticmethod
    def _missing_downloader_error(
        downloader: Optional[str],
        prepared: _PreparedDownload,
        *,
        user_specified: bool = False,
        diagnostic: Optional[str] = None,
    ) -> str:
        """
        生成所有下载器模块均未匹配到可用实例时的诊断说明。

        区分调用方指定、站点绑定和默认下载器三种目标来源，避免只提示“未找到下载器”掩盖配置线索；
        不会静默回退到其他下载器，以免破坏用户的下载器隔离与路径映射。

        :param downloader: 调用方或订阅显式传入的下载器名称
        :param prepared: 提交前已规范化的下载事实，用于读取站点绑定下载器与站点名
        :param user_specified: 是否为用户手动提交；手动下载接口会把用户选择的下载器写入 ``site_downloader``
        :param diagnostic: 可选的运行态模块或实例初始化失败说明
        :return: 供日志与失败通知展示的具体错误说明
        """
        suffix = f"（{diagnostic}）" if diagnostic else ""
        if downloader or (prepared.site_downloader and user_specified):
            target = downloader or prepared.site_downloader
            return (
                f"未找到可用下载器实例：{target}，"
                f"请检查下载器是否已启用、改名或删除，以及订阅或下载配置中的下载器设置{suffix}"
            )
        if prepared.site_downloader:
            site_name = prepared.torrent.site_name or "当前站点"
            return (
                f"未找到站点 {site_name} 指定的下载器实例：{prepared.site_downloader}，"
                f"请检查下载器是否已启用、改名或删除，以及站点下载器配置{suffix}"
            )
        return f"未找到可用的默认下载器实例，请检查下载器配置中是否存在已启用的下载器{suffix}"

    def _existing_task_confirmed_selection(
        self, task_hashes: frozenset[str],
    ) -> Optional[torrent_rules.ConfirmedSelection]:
        """
        读取 MP 此前在同一种子上确认选中过的文件（按下载器分开），供下载器识别被别人取消的文件。

        下载器只在同一 hash 已存在时使用这份记录，并只取自己那台下载器的部分，新建任务时忽略。记录随每次
        受理累积写在该 hash 最新一条下载历史的 note 中（各台下载器的记录都在同一份里，见
        ``DownloadHistoryOwner._confirmed_task_selection``），这里只需读取最新一条。
        下载器返回的 hash 大小写不一（rTorrent 为大写），因此大小写两种写法都查询。

        :param task_hashes: 由种子内容计算出的 v1/v2 hash；无法解析时为空
        :return: 确认记录；MP 没有该 hash 的任何下载历史（外部添加或从未下载过）时返回 None，
            只有升级前的历史时返回空记录
        :raises Exception: 读取下载历史失败时原样抛出，调用方按失败处理，不能当成"没有历史"
        """
        candidates = sorted({variant for item in task_hashes for variant in (item.lower(), item.upper())})
        if not candidates:
            return None
        histories = self.download_history_repository.get_by_hashes(candidates)
        if not histories:
            return None
        # 大小写两种写法各返回一条时取最新的；登记时间只精确到秒，同一秒内以主键为准。
        latest = max(
            histories.values(),
            key=lambda item: (str(getattr(item, "date", "") or ""), getattr(item, "id", None) or 0),
        )
        return torrent_rules.ConfirmedSelection.from_note(latest.note)

    def _accepted_task_files(
        self, download_hash: str, downloader: Optional[str],
    ) -> Optional[list[tuple[int, str]]]:
        """
        受理后读取下载器里该任务的文件索引与相对路径，用于写入"MP 确认选中过的文件"。

        必须在下载链持有任务锁期间读取：之后的受理决策读到的是同一套下载器索引，不受种子文件顺序（BEP 47
        填充文件）或文件改名影响，磁力链接取得元数据后也能记录。读不到时返回 None，本次不记录确认文件，
        宁可之后把被取消的文件重新选中，也不误拒正常请求。

        :param download_hash: 下载器返回的任务 hash
        :param downloader: 下载器返回的实际下载器名称
        :return: 文件索引与相对路径；读取失败、没有文件或索引不是整数时返回 None
        """
        try:
            files = self.torrent_files(download_hash, downloader)
        except Exception as err:
            logger.warning(f"读取下载任务 {download_hash} 的文件列表失败，本次不记录已确认选集：{err}")
            return None
        entries: list[tuple[int, str]] = []
        for item in files or []:
            file_id = getattr(item, "id", None)
            if isinstance(file_id, bool) or not isinstance(file_id, (int, str)) or not str(file_id).isdigit():
                logger.warning(f"下载任务 {download_hash} 的文件索引无法识别，本次不记录已确认选集")
                return None
            entries.append((int(file_id), str(getattr(item, "name", "") or "")))
        if not entries:
            logger.warning(f"未读取到下载任务 {download_hash} 的文件列表，本次不记录已确认选集")
            return None
        return entries

    def _settle_accepted_download(
        self,
        *,
        prepared: _PreparedDownload,
        context: Context,
        episodes: Optional[Set[int]],
        channel: Optional[NotificationChannel],
        source: Optional[str],
        userid: Union[str, int, None],
        username: Optional[str],
        custom_words: Optional[str],
        actual_downloader: Optional[str],
        download_hash: str,
        layout: Optional[str],
        task_action: DownloadTaskAction = DownloadTaskAction.CREATED,
        previous_selection: Optional[torrent_rules.ConfirmedSelection] = None,
        accepted_files: Optional[list[tuple[int, str]]] = None,
        downloader_scope: str = "",
    ) -> None:
        """
        按普通下载合同结算下载器明确返回的成功结果。

        下载器只有在本次目标确认受理后才返回 hash（已有任务会先追加目标文件），
        因此这里沿用请求集数结算订阅进度是安全的。

        :param task_action: 下载器对任务的实际动作，决定通知、文件明细去重与重复退避
        :param previous_selection: 提交前在同一把任务锁内读到的确认记录，受理后与本次确认的文件合并写回
        :param accepted_files: 受理后在同一把任务锁内读到的下载器文件索引与路径，见 ``_accepted_task_files``
        :param downloader_scope: 请求指向的下载器范围，重复提交退避按它记录
        """
        if task_action.is_existing_task:
            logger.info(f"{prepared.torrent.title} 下载器中已存在同一任务（{task_action.value}），沿用该任务")
        # 下载器中已存在相同 Hash 时下载器会沿用原保存目录，须以实际目录记录
        download_dir = self._resolve_actual_download_dir(
            downloader=actual_downloader,
            download_hash=download_hash,
            fallback=prepared.download_dir,
        )
        self._settle_download_success(
            context=context,
            media=prepared.media,
            meta=prepared.meta,
            torrent=prepared.torrent,
            folder_name=prepared.folder_name,
            file_list=prepared.file_list,
            download_dir=download_dir,
            layout=layout,
            downloader=actual_downloader,
            download_hash=download_hash,
            download_episodes=prepared.download_episodes,
            episodes=episodes,
            channel=channel,
            source=source,
            userid=userid,
            username=username,
            torrent_content=prepared.torrent_content,
            custom_words=custom_words,
            task_action=task_action,
            previous_selection=previous_selection,
            accepted_files=accepted_files,
            downloader_scope=downloader_scope,
        )

    def _resolve_actual_download_dir(
        self,
        *,
        downloader: Optional[str],
        download_hash: str,
        fallback: Path,
    ) -> Path:
        """
        获取下载器中该任务的真实保存目录，作为下载历史与文件明细的落库路径。

        下载器中已存在相同 Hash 的任务时（例如刷流插件先下载），下载器会沿用
        其原有保存目录；此时按预期下载目录记录会得到磁盘上不存在的路径，后续
        整理与手动整理都无法关联该文件。仅本地绝对路径需要回读，远程存储的
        目录由对应存储协议自身管理，保持原值不变。
        """
        if not download_hash or not fallback.is_absolute():
            return fallback
        try:
            torrents = self.list_torrents(hashs=[download_hash], downloader=downloader)
        except Exception as err:
            logger.debug(f"查询下载任务实际保存目录失败：{str(err)}")
            return fallback
        if not torrents:
            return fallback
        torrent = next(
            (
                item for item in torrents
                if str(getattr(item, "hash", "")) == str(download_hash)
            ),
            torrents[0],
        )
        save_path = getattr(torrent, "save_path", None)
        if not save_path:
            return fallback
        actual_dir = Path(str(save_path))
        if not actual_dir.is_absolute():
            return fallback
        if actual_dir != fallback:
            logger.warn(
                f"下载器中已存在该任务，实际保存目录 {actual_dir.as_posix()} 与预期 "
                f"{fallback.as_posix()} 不一致，按实际目录记录下载历史"
            )
        return actual_dir

    def _record_rejected_download(
        self,
        *,
        prepared: _PreparedDownload,
        context: Context,
        episodes: Optional[Set[int]],
        channel: Optional[NotificationChannel],
        source: Optional[str],
        downloader: Optional[str],
        userid: Union[str, int, None],
        actual_downloader: Optional[str],
        error_msg: str,
        user_specified: bool = False,
        record_failure: bool = True,
    ) -> None:
        """
        按普通失败合同记录下载器明确拒绝并通知原调用渠道。

        已有任务的拒绝不会让订阅跳过候选，每一轮都交给下载器按实时状态重新判断（用户在下载器里恢复任务、
        重新勾选文件后下一轮就能接着用）。自动流程（订阅、工作流、插件）在同一原因的通知静默期内重复遇到时
        只记日志，不再通知、不顺延记录，见 ``_existing_task_rejection_noted``；用户手动指定种子时每次都通知。
        当失败源于下载器实例缺失而非资源本身问题时，调用方传入 ``record_failure=False``，避免把正常种子
        写入资源级冷却台账，修正配置后即可立即重试。

        :param user_specified: 用户明确指定了这个种子
        :param record_failure: 是否写入资源级失败冷却台账；下载器实例缺失等配置问题时为 False
        """
        rejection = ExistingTaskRejection.from_message(error_msg)
        downloader_scope = self._download_scope(downloader, prepared.torrent)
        if rejection is not None and not user_specified and self._existing_task_rejection_noted(
                context, rejection=rejection, downloader_scope=downloader_scope, episodes=episodes,
        ):
            logger.info(
                f"{prepared.media.title_year} 下载器中的同一任务仍不能受理（与上次原因相同，不重复通知）："
                f"{prepared.torrent.title}，{error_msg}"
            )
            return
        logger.error(
            f"{prepared.media.title_year} 添加下载任务失败："
            f"{prepared.torrent.title} - {prepared.torrent.enclosure}，{error_msg}"
        )
        if record_failure:
            self._record_download_failure(
                context=context,
                error_msg=error_msg,
                downloader=actual_downloader or downloader or prepared.site_downloader,
                source=source,
                episodes=episodes,
                downloader_scope=downloader_scope,
            )
        self.post_message(
            Message(
                channel=channel,
                source=source if channel else None,
                mtype=MessageType.Manual,
                title=f"添加下载任务失败：{prepared.media.title_year} {prepared.meta.season_episode}",
                text=(
                    f"站点：{prepared.torrent.site_name}\n"
                    f"种子名称：{prepared.meta.org_string}\n"
                    f"错误信息：{error_msg}"
                ),
                image=prepared.media.get_message_image(),
                poster=prepared.media.get_poster_image() or None,
                userid=userid,
            )
        )
