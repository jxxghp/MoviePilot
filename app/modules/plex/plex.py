"""
Plex 客户端，负责媒体库同步、媒体身份查询、播放链接及 Webhook 转换。
"""

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Callable, Dict, Generator, List, Optional, Tuple, Union
from urllib.parse import quote_plus

from plexapi.audio import Album, Artist, Track
from plexapi.base import PlexPartialObject
from plexapi.exceptions import NotFound
from plexapi.library import Library, LibrarySection
from plexapi.myplex import MyPlexAccount
from plexapi.server import PlexServer
from plexapi.video import Episode, Movie, Season, Show
from requests import RequestException

from app.adapters.network.http import RequestUtils
from app.application.mediaserver import MediaServerIdentityHelper
from app.foundation.url import UrlUtils
from app.runtime.cache import cached
from app.runtime.log import logger
from app.schemas.dashboard import Statistic as _SchemaStatistic
from app.schemas.mediaserver import MediaServerField, MediaServerItem, MediaServerQueryOptions
from app.schemas.mediaserver import MediaServerItem as _SchemaMediaServerItem
from app.schemas.mediaserver import MediaServerItemUserState as _SchemaMediaServerItemUserState
from app.schemas.mediaserver import MediaServerLibrary as _SchemaMediaServerLibrary
from app.schemas.mediaserver import MediaServerPlayItem as _SchemaMediaServerPlayItem
from app.schemas.mediaserver import RefreshMediaItem as _SchemaRefreshMediaItem
from app.schemas.mediaserver import WebhookEventInfo as _SchemaWebhookEventInfo
from app.schemas.types import MediaSource, MediaType

# 全量同步每页读取 50 条，避免一次构造整个媒体库的 SDK 对象。
PLEX_LIBRARY_SYNC_PAGE_SIZE = 50
# 同步超时最多尝试 3 次，包含首次请求。
PLEX_SYNC_TIMEOUT_MAX_ATTEMPTS = 3
# 同步超时重试之间的等待秒数。
PLEX_SYNC_TIMEOUT_RETRY_DELAY_SECONDS = 1
# 同步无需流派和演职员等关联节点；保留媒体路径和来源 Guid 以免逐项补查详情。
PLEX_SYNC_EXCLUDE_ELEMENTS = (
    "Genre,Country,Rating,Collection,Director,Writer,Role,Producer,Similar,Style,Mood,Format"
)


class Plex:
    """
    Plex 媒体服务器客户端，负责连接管理和媒体库数据读取。
    """

    _plex = None
    _request_utils = None
    _sync_libraries: List[str] = []

    @staticmethod
    def _query_params(query: Optional[MediaServerQueryOptions]) -> dict[str, str]:
        """
        将显式内容投影转换为 Plex 的属性与子节点排除参数。

        不使用跨 API 版本语义不同的 includeFields；不支持裁剪的旧服务端可能忽略请求。
        Media 保存文件路径，Guid 保存媒体身份，二者只在调用方不需要时排除。

        :param query: 可选查询投影；默认选项不改变旧请求
        :return: 通过 SDK 的 params 或查询串发送的服务端参数
        """
        if query is None or query.fields is None:
            return {}
        attributes = {
            MediaServerField.ORIGINAL_TITLE: ("originalTitle",),
            MediaServerField.YEAR: ("year", "originallyAvailableAt"),
            MediaServerField.USER_STATE: ("viewOffset", "viewCount", "lastViewedAt"),
            MediaServerField.IMAGES: (
                "art",
                "thumb",
                "parentArt",
                "parentThumb",
                "grandparentArt",
                "grandparentThumb",
                "banner",
            ),
        }
        elements = {
            MediaServerField.IDENTITY: ("Guid",),
            MediaServerField.PATH: ("Media",),
            MediaServerField.IMAGES: ("Image", "OriginalImage"),
        }
        excluded_fields = [name for field, names in attributes.items() if not query.includes(field) for name in names]
        excluded_elements = PLEX_SYNC_EXCLUDE_ELEMENTS.split(",") + [
            name for field, names in elements.items() if not query.includes(field) for name in names
        ]
        return {"excludeFields": ",".join(excluded_fields), "excludeElements": ",".join(excluded_elements)}

    @staticmethod
    def _disable_auto_reload(item: Any, query: Optional[MediaServerQueryOptions]) -> None:
        """
        防止 SDK 为显式省略的可选字段自动补查完整详情。

        :param item: 待使用的 Plex SDK 对象
        :param query: 显式投影；默认选项保留 SDK 原行为
        """
        if query is not None and query.fields is not None and isinstance(item, PlexPartialObject):
            item._autoReload = False

    def _search_music_library(
        self,
        library: LibrarySection,
        title: str,
        query: Optional[MediaServerQueryOptions],
    ) -> list[Any]:
        """
        显式精简音乐查询通过 fetchItems.params 发送，避免被 search 误当成筛选字段。

        :param library: 待搜索的 Plex 音乐库
        :param title: 音乐搜索词
        :param query: 可选内容投影；默认选项保留原搜索方法
        :return: 服务端返回的音乐候选
        """
        if query is None or query.fields is None:
            return list(library.search(title=title))
        key = library._buildSearchKey(title=title, includeGuids=query.includes(MediaServerField.IDENTITY))
        return list(library.fetchItems(key, params=self._query_params(query.with_fields(MediaServerField.MUSIC))))

    def __init__(self, host: Optional[str] = None, token: Optional[str] = None, play_host: Optional[str] = None,
                 sync_libraries: Optional[list[str]] = None, timeout: Optional[int] = 30, **kwargs: Any) -> None:
        """
        连接 Plex 并缓存媒体库目录，建立可复用的自定义请求会话。

        缺少地址或令牌时不创建客户端；SDK 连接失败时保留未连接状态。

        :param host: Plex 服务根地址
        :param token: 服务访问令牌
        :param play_host: 外网播放地址，省略时链接使用服务地址
        :param sync_libraries: 同步媒体库标识列表；空列表或包含 all 表示不限制
        :param timeout: SDK 单次请求超时秒数；无效、非正或空值回退为 30
        :param kwargs: 通用服务配置的额外参数，当前不读取
        """
        try:
            self._timeout = int(timeout or 30)
        except (TypeError, ValueError):
            self._timeout = 30
        if self._timeout <= 0:
            self._timeout = 30
        if not host or not token:
            logger.error("Plex服务器配置不完整！")
            return
        self._host = host
        if self._host:
            self._host = UrlUtils.standardize_base_url(self._host)
        self._playhost = play_host
        if self._playhost:
            self._playhost = UrlUtils.standardize_base_url(self._playhost)
        self._token = token
        if self._host and self._token:
            try:
                self._plex = PlexServer(self._host, self._token, timeout=self._timeout)
                self._libraries = self._plex.library.sections()
            except Exception as e:
                self._plex = None
                logger.error(f"Plex服务器连接失败：{str(e)}")
            self._request_utils = RequestUtils(
                headers=self.__get_request_headers(),
                use_session=True,
            )
        self._sync_libraries = sync_libraries or []

    def is_inactive(self) -> bool:
        """
        判断已配置的服务是否需要重新连接。

        :return: 地址和令牌存在但 SDK 客户端为空时返回 True
        """
        if not self._host or not self._token:
            return False
        return True if not self._plex else False

    def reconnect(self) -> None:
        """
        重建 Plex SDK 客户端并刷新媒体库目录缓存；失败时清空 SDK 客户端。
        """
        try:
            self._plex = PlexServer(self._host, self._token, timeout=self._timeout)
            self._libraries = self._plex.library.sections()
        except Exception as e:
            self._plex = None
            logger.error(f"Plex服务器连接失败：{str(e)}")

    def authenticate(self, username: str, password: str) -> Optional[Tuple[str, str]]:
        """
        登录 Plex 账号，并验证该账号能否访问当前服务器。

        :param username: Plex 登录用户名或邮箱
        :param password: Plex 登录密码
        :return: 访问令牌和账号规范用户名；输入不完整、登录或服务器连接失败时返回 None
        """
        if not username or not password:
            return None
        try:
            account = MyPlexAccount(username=username, password=password, remember=False)
            if account:
                plex = PlexServer(self._host, account.authToken, timeout=self._timeout)
                if not plex:
                    return None
                return account.authToken, account.username
        except Exception as e:
            # 处理认证失败或网络错误等情况
            logger.error(f"Authentication failed: {e}")
        return None

    _image_cache: Callable[
        [Callable[..., Optional[List[str]]]], Callable[..., Optional[List[str]]]
    ] = cached(maxsize=32, ttl=86400)

    def __get_library_images(self, library_key: str, mtype: int) -> Optional[List[str]]:
        """
        按服务地址与令牌摘要隔离媒体库封面缓存。

        :param library_key: Plex 媒体库标识
        :param mtype: Plex 搜索类型值
        :return: 当前服务器凭据对应的媒体库封面
        """
        identity = hashlib.sha256(f"{self._host}\0{self._token}".encode()).hexdigest()
        return self.__get_cached_library_images(library_key, mtype, identity)

    @_image_cache
    def __get_cached_library_images(
        self, library_key: str, mtype: int, server_identity: str,
    ) -> Optional[List[str]]:
        """
        分页读取最近新增媒体，选取最多 4 张不同的库封面。

        分集优先使用父级封面；结果最多缓存一天，供媒体库卡片展示。

        :param library_key: Plex 媒体库标识
        :param mtype: Plex 搜索类型值：1 为电影，2 为剧集，8 为音乐
        :param server_identity: 服务地址与令牌摘要，只用于隔离缓存，不保存原始凭据
        :return: 带服务令牌的本地图片 URL 列表；SDK 未连接时返回 None
        """
        if not self._plex:
            return None
        # 返回结果
        poster_urls: dict[str, None] = {}
        # 页码计数
        container_start = 0
        # 需要的总条数/每页的条数
        total_size = 4
        # 如果总数不足,接续获取下一页
        while len(poster_urls) < total_size:
            items = self._plex.fetchItems(f"/hubs/home/recentlyAdded?type={mtype}&sectionID={library_key}",
                                          container_start=container_start,
                                          container_size=8,
                                          maxresults=8)
            for item in items:
                if item.type == "episode":
                    # 如果是剧集的单集,则去找上级的图片
                    if item.parentThumb is not None:
                        poster_urls[item.parentThumb] = None
                else:
                    # 否则就用自己的图片
                    if item.thumb is not None:
                        poster_urls[item.thumb] = None
                if len(poster_urls) == total_size:
                    break
            if len(items) < total_size:
                break
            container_start += total_size
        return [f"{self._host.rstrip('/') + url}?X-Plex-Token={self._token}" for url in
                list(poster_urls.keys())[:total_size]]

    def get_librarys(
        self, hidden: Optional[bool] = False, *, query: Optional[MediaServerQueryOptions] = None
    ) -> Optional[List[_SchemaMediaServerLibrary]]:
        """
        刷新媒体库目录，并转换支持的电影、剧集和音乐库。

        :param hidden: 是否隐藏未选入同步范围的库；默认返回全部支持的媒体库
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :return: 统一媒体库列表；SDK 未连接或读取库目录失败时返回 None
        """
        if not self._plex:
            return None
        try:
            self._libraries = self._plex.library.sections()
        except Exception as err:
            logger.error(f"获取媒体服务器所有媒体库列表出错：{str(err)}")
            return None
        options = query or MediaServerQueryOptions()
        libraries = []
        for library in self._libraries:
            if (
                hidden
                and self._sync_libraries
                and "all" not in self._sync_libraries
                and str(library.key) not in self._sync_libraries
            ):
                continue
            if library.type == "movie":
                library_type = MediaType.MOVIE.value
                image_list = (
                    self.__get_library_images(library.key, 1) if options.includes(MediaServerField.IMAGES) else None
                )
            elif library.type == "show":
                library_type = MediaType.TV.value
                image_list = (
                    self.__get_library_images(library.key, 2) if options.includes(MediaServerField.IMAGES) else None
                )
            elif library.type in ("artist", "music"):
                library_type = MediaType.MUSIC.value
                image_list = (
                    self.__get_library_images(library.key, 8) if options.includes(MediaServerField.IMAGES) else None
                )
            else:
                continue
            libraries.append(
                _SchemaMediaServerLibrary(
                    id=library.key,
                    name=library.title,
                    path=library.locations if options.includes(MediaServerField.PATH) else None,
                    type=library_type,
                    item_count=self.get_items_count(library.key) if options.includes(MediaServerField.COUNTS) else None,
                    image_list=image_list,
                    link=(
                        f"{self._playhost or self._host}web/index.html#!/media/{self._plex.machineIdentifier}"
                        f"/com.plexapp.plugins.library?source={library.key}&X-Plex-Token={self._token}"
                    )
                    if options.includes(MediaServerField.LINK)
                    else None,
                    server_type="plex",
                )
            )
        return libraries

    def get_medias_count(self) -> _SchemaStatistic:
        """
        统计同步范围内的电影、电视剧、分集与音乐条目数量。

        :return: 统一统计对象；SDK 未连接时返回空统计，SDK 查询异常向调用方传播
        """
        if not self._plex:
            return _SchemaStatistic()
        sections = self._plex.library.sections()
        movie_count = tv_count = episode_count = music_count = 0
        # 媒体库白名单
        allow_library = [str(lib.id) for lib in self.get_librarys(hidden=True) or []]
        for sec in sections:
            if str(sec.key) not in allow_library:
                continue
            if sec.type == "movie":
                movie_count += sec.totalSize
            if sec.type == "show":
                tv_count += sec.totalSize
                episode_count += sec.totalViewSize(libtype="episode")
            if sec.type in ("artist", "music"):
                music_count += sec.totalSize
        return _SchemaStatistic(
            movie_count=movie_count,
            tv_count=tv_count,
            episode_count=episode_count,
            music_count=music_count,
        )

    def get_movies(
        self,
        title: str,
        original_title: Optional[str] = None,
        year: Optional[str] = None,
        media_source: Optional[MediaSource] = None,
        media_id: Optional[str] = None,
        *,
        query: Optional[MediaServerQueryOptions] = None,
    ) -> Optional[List[_SchemaMediaServerItem]]:
        """
        按标题和可选年份搜索电影，并排除来源身份冲突。

        :param title: 搜索标题
        :param original_title: 不同于标题时补充搜索的原始标题
        :param year: 可选年份，空值表示不限年份
        :param media_source: 用于校验身份的媒体来源
        :param media_id: 对应来源内的标识
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :return: 去重后的电影条目列表；未连接时返回 None，无匹配项时返回空列表，SDK 查询异常向上传播
        """
        if not self._plex:
            return None
        options = query or MediaServerQueryOptions()
        params = self._query_params(query.with_fields(MediaServerField.IDENTITY) if query is not None else None)
        if query is not None and query.fields is not None:
            params["includeGuids"] = "1"
        ret_movies = []
        if year:
            movies = self._plex.library.search(title=title, year=year, libtype="movie", **params)
            # 根据原标题再查一遍
            if original_title and str(original_title) != str(title):
                movies.extend(self._plex.library.search(title=original_title, year=year, libtype="movie", **params))
        else:
            movies = self._plex.library.search(title=title, libtype="movie", **params)
            if original_title and str(original_title) != str(title):
                movies.extend(self._plex.library.search(title=original_title, libtype="movie", **params))
        for item in set(movies):
            self._disable_auto_reload(item, query)
            ids = self.__get_ids(item.guids)
            item_source, item_media_id = MediaServerIdentityHelper.from_provider_ids(ids)
            if not MediaServerIdentityHelper.are_compatible(item_source, item_media_id, media_source, media_id):
                continue
            path = None
            if options.includes(MediaServerField.PATH) and item.locations:
                path = item.locations[0]
            ret_movies.append(
                _SchemaMediaServerItem(
                    server="plex",
                    library=item.librarySectionID,
                    item_id=item.key,
                    item_type=item.type,
                    title=item.title,
                    original_title=item.originalTitle,
                    year=item.year,
                    media_source=item_source,
                    media_id=item_media_id,
                    path=path,
                )
            )
        return ret_movies

    def get_music(
        self,
        title: Optional[str] = None,
        artist: Optional[str] = None,
        album: Optional[str] = None,
        *,
        query: Optional[MediaServerQueryOptions] = None,
    ) -> List[_SchemaMediaServerItem]:
        """
        在音乐库中按专辑、歌曲或艺术家名称搜索候选。

        此方法不做名称精确匹配，由上层音乐匹配服务核对候选。

        :param title: 歌曲标题；未提供专辑名时作为查询词
        :param artist: 艺术家名称；没有专辑名和歌曲标题时作为查询词
        :param album: 专辑名称，优先作为查询词
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :return: 统一音乐候选列表；未连接或查询词为空时返回空列表，查询中途失败时保留已取得的候选
        """
        if not self._plex:
            return []
        search_term = album or title or artist
        if not search_term:
            return []
        results: List[_SchemaMediaServerItem] = []
        try:
            for library in self._plex.library.sections():
                if library.type not in ("artist", "music"):
                    continue
                for item in self._search_music_library(library, search_term, query):
                    self._disable_auto_reload(item, query)
                    item_type = getattr(item, "type", None)
                    if item_type == "track":
                        item_artist = getattr(item, "grandparentTitle", None)
                        item_album = getattr(item, "parentTitle", None)
                    else:
                        item_artist = getattr(item, "parentTitle", None)
                        item_album = getattr(item, "title", None) if item_type == "album" else None
                    item_id = getattr(item, "ratingKey", None) or getattr(item, "key", None)
                    results.append(
                        _SchemaMediaServerItem(
                            server="plex",
                            library=library.key,
                            item_id=str(item_id) if item_id is not None else None,
                            item_type=item_type or MediaType.MUSIC.value,
                            title=getattr(item, "title", None),
                            original_title=getattr(item, "title", None),
                            year=getattr(item, "year", None),
                            path=getattr(item, "file", None)
                            if query is None or query.includes(MediaServerField.PATH)
                            else None,
                            note={
                                "artist": item_artist,
                                "album": item_album,
                                "song_count": getattr(item, "leafCount", None),
                            },
                        )
                    )
        except Exception as e:
            logger.debug(f"查询Plex音乐出错：{e}")
        return results

    def get_tv_episodes(
        self,
        item_id: Optional[str] = None,
        title: Optional[str] = None,
        original_title: Optional[str] = None,
        year: Optional[str] = None,
        media_source: Optional[MediaSource] = None,
        media_id: Optional[str] = None,
        season: Optional[int] = None,
        *,
        query: Optional[MediaServerQueryOptions] = None,
    ) -> Tuple[Optional[str], Optional[Dict[int, List[int]]]]:
        """
        查询电视剧及其各季已有的分集编号。

        缓存条目标识不存在或请求失败时回退到标题搜索；其他 SDK 异常继续向调用方传播。

        :param item_id: Plex 条目标识，可以是数字标识或 /library/metadata/ 路径
        :param title: 缺少有效条目标识时使用的剧名
        :param original_title: 标题搜索无结果时使用的原始剧名
        :param year: 可选年份，空值表示不限年份
        :param media_source: 用于排除身份冲突的媒体来源
        :param media_id: 对应来源内的标识
        :param season: 指定季号，None 表示全部季
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :return: 电视剧条目路径及“季号到集号列表”映射；未连接、无匹配或身份冲突时返回 None 和空映射
        """
        if not self._plex:
            return None, {}
        if item_id:
            try:
                videos = self.__fetch_item(
                    item_id, **({"query": query.with_fields(MediaServerField.IDENTITY)} if query is not None else {})
                )
            except NotFound, RequestException:
                # Plex 删除并重新入库后元数据标识会变化，缓存的旧item_id失效时回退到搜索路径。
                logger.warning(f"Plex缓存的电视剧媒体ID {item_id} 已失效，尝试按标题重新搜索：{title}")
                videos = self.__search_show(
                    title=title,
                    original_title=original_title,
                    year=year,
                    **MediaServerQueryOptions.as_kwargs(query),
                )
        else:
            videos = self.__search_show(
                title=title, original_title=original_title, year=year, **MediaServerQueryOptions.as_kwargs(query)
            )

        if not videos:
            return None, {}
        if isinstance(videos, list):
            videos = videos[0]
        self._disable_auto_reload(videos, query)
        video_source, video_media_id = MediaServerIdentityHelper.from_provider_ids(self.__get_ids(videos.guids))
        if not MediaServerIdentityHelper.are_compatible(video_source, video_media_id, media_source, media_id):
            return None, {}
        episodes = videos.episodes(
            **(
                {"params": self._query_params(MediaServerQueryOptions(fields=frozenset()))}
                if query is not None and query.fields is not None
                else {}
            )
        )
        season_episodes: Dict[int, List[int]] = {}
        for episode in episodes:
            self._disable_auto_reload(episode, query)
            if season is not None and episode.seasonNumber != int(season):
                continue
            # Plex 空条目可能缺少季集号，不能传入后续季集模型校验。
            if episode.seasonNumber is None or episode.index is None:
                continue
            if episode.seasonNumber not in season_episodes:
                season_episodes[episode.seasonNumber] = []
            season_episodes[episode.seasonNumber].append(episode.index)
        return videos.key, season_episodes

    def get_season_episode_ids(
        self, item_id: str, season: int, *, query: Optional[MediaServerQueryOptions] = None
    ) -> Dict[int, str]:
        """
        查询指定季中每一集的 Plex 条目路径。

        :param item_id: Plex 条目标识，可以是数字标识或 /library/metadata/ 路径
        :param season: 季号
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :return: 集号到条目路径的映射；输入不完整、未连接、无分集或查询失败时返回空映射
        """
        if not self._plex or not item_id:
            return {}
        try:
            videos = self.__fetch_item(item_id, **MediaServerQueryOptions.as_kwargs(query))
            if not videos:
                return {}
            episode_ids: Dict[int, str] = {}
            for episode in videos.episodes(
                **(
                    {"params": self._query_params(MediaServerQueryOptions(fields=frozenset()))}
                    if query is not None and query.fields is not None
                    else {}
                )
            ):
                self._disable_auto_reload(episode, query)
                if episode.seasonNumber != int(season):
                    continue
                if episode.index is None or not episode.key:
                    continue
                episode_ids[int(episode.index)] = str(episode.key)
            return episode_ids
        except Exception as e:
            logger.error(f"获取 Plex 季集条目 ID 出错：{str(e)}")
            return {}

    def __search_show(
        self,
        title: Optional[str] = None,
        original_title: Optional[str] = None,
        year: Optional[str] = None,
        *,
        query: Optional[MediaServerQueryOptions] = None,
    ) -> Any:
        """
        按标题搜索Plex电视剧条目，供常规查询和缓存item_id失效后的回退查询复用。

        :param title: 标题
        :param original_title: 原产地标题
        :param year: 年份，可以为空，为空时不按年份过滤
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :return: Plex搜索返回的电视剧条目列表
        """
        if self._plex is None:
            raise AttributeError("'NoneType' object has no attribute 'library'")
        # 兼容年份为空的场景
        kwargs = {"year": year} if year else {}
        kwargs.update(self._query_params(query.with_fields(MediaServerField.IDENTITY) if query is not None else None))
        if query is not None and query.fields is not None:
            kwargs["includeGuids"] = "1"
        # 根据标题和年份搜索候选，后续仍需核对规范媒体来源身份。
        videos = self._plex.library.search(title=title, libtype="show", **kwargs)
        if not videos and original_title and str(original_title) != str(title):
            videos = self._plex.library.search(title=original_title, libtype="show", **kwargs)
        return videos

    def get_remote_image_by_id(
        self,
        item_id: str,
        image_type: str,
        depth: Optional[int] = 0,
        plex_url: Optional[bool] = True,
        *,
        query: Optional[MediaServerQueryOptions] = None,
    ) -> Optional[str]:
        """
        根据ItemId从Plex查询图片地址。

        :param item_id: 在Plex中的ID
        :param image_type: 图片的类型，Poster或者Backdrop等
        :param depth: 当前递归深度，默认为0
        :param plex_url: 是否返回Plex的URL，默认为True（仅在配置了外网地址和Token时有效）
        :param query: 可选内容投影；显式查询只获取图片及父级回退需要的基础信息
        :return: 图片对应在plex服务器或TMDB中的URL
        """
        if not self._plex:
            return None
        if depth is None:
            raise TypeError("'>' not supported between instances of 'NoneType' and 'int'")
        if depth > 2 or not item_id:
            return None
        if query is not None and not query.includes(MediaServerField.IMAGES):
            return None
        try:
            image_url = None
            ekey = item_id
            item = self._plex.fetchItem(
                ekey=ekey,
                **(
                    {"params": self._query_params(MediaServerQueryOptions(fields=frozenset({MediaServerField.IMAGES})))}
                    if query is not None and query.fields is not None
                    else {}
                ),
            )
            self._disable_auto_reload(item, query)
            if not item:
                return None
            # 直接使用当前条目的 art/thumb 作为 Plex 本地图片兜底。
            # 这样既能兼容 webhook 返回带 /children 的 key，也能避免图片列表为空时彻底没有封面。
            local_image_url = self.__build_local_image_url(item=item, image_type=image_type, plex_url=plex_url)
            # 如果配置了外网播放地址以及Token，则默认从Plex媒体服务器获取图片，否则返回有外网地址的图片资源
            # Plex外网播放地址这个框里目前可以填两种地址
            #   1. Plex的官方转发地址https://app.plex.tv, 2. 自己处理的端口转发地址
            #   如果使用的是1的官方转发地址,那么就不能走这个逻辑，因为官方转发地址无法获取到图片
            if self._playhost and "app.plex.tv" not in self._playhost and self._token and plex_url:
                image_url = local_image_url
            else:
                if image_type == "Poster":
                    # 这里必须通过 item.posters() 走 Plex 的规范元数据端点，
                    # 不能直接拼原始 item_id，否则 show webhook 返回 /children 时会变成 /children/posters。
                    images = item.posters() if hasattr(item, "posters") else []
                    images = images or []
                else:
                    # 默认使用art也就是Backdrop进行处理
                    # 同上，统一通过 item.arts() 取图片列表，避免 /children/arts 这类错误路径。
                    images = item.arts() if hasattr(item, "arts") else []
                    images = images or []
                    # 这里对episode进行特殊处理，实际上episode的Backdrop是Poster
                    # 部分分集只提供海报；先尝试背景图，缺少时再回退海报。
                    if not images and item.TYPE == "episode":
                        images = item.posters() if hasattr(item, "posters") else []
                        images = images or []
                for image in images:
                    image_key = getattr(image, "key", None)
                    if image_key and image_key.startswith("http"):
                        image_url = image_key
                        break
                # 某些 Plex 条目没有可直接外链的图片列表，此时退回到当前条目的实际图片地址。
                if not image_url and local_image_url:
                    image_url = local_image_url
                # 如果最后还是找不到，则递归父级进行查找
                parent_key = getattr(item, "parentKey", None)
                if not image_url and parent_key:
                    return self.get_remote_image_by_id(
                        item_id=parent_key,
                        image_type=image_type,
                        depth=depth + 1,
                        plex_url=plex_url,
                        **MediaServerQueryOptions.as_kwargs(query),
                    )
            return image_url
        except Exception as e:
            logger.error("获取封面出错：" + str(e))
        return None

    def __build_local_image_url(
            self,
            item: Any,
            image_type: str,
            plex_url: Optional[bool] = True
    ) -> Optional[str]:
        """
        构造 Plex 本地图片地址，作为图片列表查询失败时的兜底结果。
        """
        if not item or not plex_url or not self._token:
            return None

        # app.plex.tv 无法直接用于图片获取，因此回退到实际的 Plex 服务地址。
        image_host = self._playhost if self._playhost and "app.plex.tv" not in self._playhost else self._host
        if not image_host:
            return None

        query = {"X-Plex-Token": self._token}
        item_type = getattr(item, "TYPE", None)

        if image_type == "Poster":
            image_path = getattr(item, "thumb", None)
        else:
            image_path = getattr(item, "art", None)
            # episode 的背景图经常落在 thumb 上，这里沿用原有特殊处理逻辑。
            if not image_path and item_type == "episode":
                image_path = getattr(item, "thumb", None)

        if not image_path:
            return None
        return UrlUtils.combine_url(host=image_host, path=image_path, query=query)

    def refresh_root_library(self) -> Union[bool, Library]:
        """
        请求 Plex 刷新整个媒体库。

        :return: 原样返回 Plex SDK 的刷新结果，未连接时返回 False；不等待扫描完成，SDK 异常向上传播
        """
        if not self._plex:
            return False
        updated: Library = self._plex.library.update()
        return updated

    def refresh_library_by_items(self, items: List[_SchemaRefreshMediaItem]) -> Optional[bool]:
        """
        根据媒体目标路径合并刷新范围，并请求扫描对应父目录。

        任一目标路径无法归属媒体库时改为刷新根库；SDK 请求异常向调用方传播。

        :param items: 包含 target_path 的媒体刷新请求列表
        :return: 局部刷新请求完成或根库刷新未明确返回 False 时返回 True；未连接时返回 False
        """
        if not self._plex:
            return False
        result_dict = {}
        for item in items:
            file_path = item.target_path
            lib_key, path = self.__find_librarie(file_path, self._libraries)
            # 如果存在同一剧集的多集,key(path)相同会合并
            if path:
                result_dict[path.as_posix()] = lib_key
            else:
                result_dict[""] = lib_key
        if "" in result_dict:
            # 如果有匹配失败的,刷新整个库
            refreshed = self._plex.library.update()
            return refreshed is not False
        else:
            # 否则一个一个刷新
            for refresh_path, lib_key in result_dict.items():
                logger.info(f"刷新媒体库：{lib_key} - {refresh_path}")
                self._plex.query(f'/library/sections/{lib_key}/refresh?path={quote_plus(Path(refresh_path).parent.as_posix())}')
        return True

    @staticmethod
    def __find_librarie(path: Optional[Path], libraries: List[Any]) -> Tuple[str, Optional[Path]]:
        """
        按路径归属查找第一个匹配的媒体库。

        先解析路径再比较目录层级；多个库路径重叠时按传入列表顺序选择。

        :param path: 待刷新的目标路径；None 表示无法定位
        :param libraries: 包含 locations 目录配置的 Plex 媒体库对象列表
        :return: 媒体库标识与原目标路径；未匹配或处理异常时返回空标识和 None
        """

        def is_subpath(_path: Path, _parent: Path) -> bool:
            """
            判断_path是否是_parent的子目录下。
            """
            _path = _path.resolve()
            _parent = _parent.resolve()
            return _path.parts[:len(_parent.parts)] == _parent.parts

        if path is None:
            return "", None

        try:
            for lib in libraries:
                if hasattr(lib, "locations") and lib.locations:
                    for location in lib.locations:
                        if is_subpath(path, Path(location)):
                            return lib.key, path
        except Exception as err:
            logger.error(f"查找媒体库出错：{str(err)}")
        return "", None

    def get_iteminfo(
        self, itemid: str, *, query: Optional[MediaServerQueryOptions] = None
    ) -> Optional[_SchemaMediaServerItem]:
        """
        查询单个条目并转换为统一媒体模型。

        :param itemid: Plex 数字标识或条目路径
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :return: 条目详情；未连接、条目不存在或查询失败时返回 None
        """
        if not self._plex:
            return None
        try:
            item = self.__fetch_item(itemid, **MediaServerQueryOptions.as_kwargs(query))
            return self.__build_media_server_item(item, **MediaServerQueryOptions.as_kwargs(query))
        except Exception as err:
            logger.error(f"获取项目详情出错：{str(err)}")
        return None

    @staticmethod
    def __get_ids(guids: List[Any]) -> dict[str, Union[str, int, None]]:
        """
        从 Plex Guid 提取 IMDb、TMDB 和 TVDB 来源标识。

        :param guids: 包含 id 的字典或 SDK Guid 对象列表
        :return: 含 imdb_id、tmdb_id、tvdb_id 的字典；缺失项为 None，TMDB 标识转换为整数，无法转换时忽略
        """
        def parse_tmdb_id(value: str) -> tuple[bool, Optional[int]]:
            """
            尝试将TMDB ID字符串转换为整数。如果成功，返回(True, int)，失败则返回(False, None)。
            """
            try:
                int_value = int(value)
                return True, int_value
            except ValueError:
                return False, None

        guid_mapping = {
            "imdb://": "imdb_id",
            "tmdb://": "tmdb_id",
            "tvdb://": "tvdb_id"
        }
        ids: dict[str, Union[str, int, None]] = {varname: None for varname in guid_mapping.values()}
        for guid in guids:
            guid_id = guid['id'] if isinstance(guid, dict) else guid.id
            for prefix, varname in guid_mapping.items():
                if guid_id.startswith(prefix):
                    clean_id = guid_id[len(prefix):]
                    if varname == "tmdb_id":
                        # tmdb_id为int，Plex可能存在脏数据，特别处理tmdb_id
                        success, parsed_id = parse_tmdb_id(clean_id)
                        if success:
                            ids[varname] = parsed_id
                    else:
                        ids[varname] = clean_id
                    break

        return ids

    def __fetch_item(
        self, item_id: Union[int, str], *, query: Optional[MediaServerQueryOptions] = None
    ) -> Union[Movie, Show, Season, Episode, Artist, Album, Track]:
        """
        根据给定的item_id获取媒体项。

        :param item_id: 媒体项的ID，可以是整数或字符串，如果是字符串且表示为数字，将会被转换为整数

        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        """
        if isinstance(item_id, str):
            if item_id.isdigit():
                item_id = int(item_id)
            elif not item_id.startswith("/library/metadata/"):
                # 其他媒体服务器的 UUID 不能作为 Plex metadata key，直接按失效缓存回退。
                raise NotFound(f"invalid Plex metadata id: {item_id}")
        if self._plex is None:
            raise AttributeError("'NoneType' object has no attribute 'fetchItem'")
        params = self._query_params(query)
        if query is not None and query.fields is not None:
            params["includeGuids"] = str(int(query.includes(MediaServerField.IDENTITY)))
        item: Union[Movie, Show, Season, Episode, Artist, Album, Track] = self._plex.fetchItem(
            item_id, **({"params": params} if params else {})
        )
        self._disable_auto_reload(item, query)
        return item

    def __build_media_server_item(
        self,
        item: Union[Movie, Show, Season, Episode, Artist, Album, Track, None],
        *,
        query: Optional[MediaServerQueryOptions] = None,
    ) -> Optional[_SchemaMediaServerItem]:
        """
        按 Plex 条目实际具备的字段构造媒体信息，兼容艺人、专辑和曲目。

        :param item: Plex媒体项目
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :return: 标准媒体条目，包含播放状态和路径；输入为空时返回 None
        """
        if not item:
            return None
        options = query or MediaServerQueryOptions()
        self._disable_auto_reload(item, query)
        ids = self.__get_ids(item.guids) if options.includes(MediaServerField.IDENTITY) else {}
        locations = getattr(item, "locations", None) if options.includes(MediaServerField.PATH) else None
        path = locations[0] if locations else None
        user_state = self._item_user_state(item) if options.includes(MediaServerField.USER_STATE) else None

        return _SchemaMediaServerItem(
            server="plex",
            library=item.librarySectionID if options.includes(MediaServerField.LIBRARY) else None,
            item_id=item.key,
            item_type=item.type,
            title=item.title,
            original_title=getattr(item, "originalTitle", None)
            if options.includes(MediaServerField.ORIGINAL_TITLE)
            else None,
            year=getattr(item, "year", None) if options.includes(MediaServerField.YEAR) else None,
            media_source=MediaServerIdentityHelper.from_provider_ids(ids)[0],
            media_id=MediaServerIdentityHelper.from_provider_ids(ids)[1],
            path=path,
            user_state=user_state,
        )

    @staticmethod
    def _item_user_state(item: Any) -> _SchemaMediaServerItemUserState:
        """
        仅在需要播放状态时读取 SDK 的进度、次数和时间属性。

        :param item: 包含播放状态的 Plex 条目
        :return: 统一用户状态
        """
        playback_position = getattr(item, "viewOffset", None) or 0
        duration = getattr(item, "duration", None) or 0
        percentage = (playback_position / duration * 100) if duration > 0 else None
        played = getattr(item, "isPlayed", None) or False
        play_count = getattr(item, "viewCount", None) or 0
        last_played_date = getattr(item, "lastViewedAt", None)

        user_state = _SchemaMediaServerItemUserState(
            played=played,
            resume=playback_position > 0,
            last_played_date=last_played_date.isoformat()
            if last_played_date and hasattr(last_played_date, "isoformat")
            else None,
            play_count=play_count,
            percentage=percentage,
        )

        return user_state

    def get_items_count(self, parent: Union[str, int]) -> Optional[int]:
        """
        获取指定媒体库可同步的媒体条目总数。

        :param parent: 媒体库ID
        :return: 媒体条目总数，查询失败时返回None
        """
        if not parent or not self._plex:
            return None
        try:
            section = self._plex.library.sectionByID(int(parent))
            return int(section.totalSize) if section else None
        except Exception as err:
            logger.error(f"查询媒体库 {parent} 的媒体总数出错：{str(err)}")
            return None

    def get_items(
        self,
        parent: Union[str, int],
        start_index: Optional[int] = 0,
        limit: Optional[int] = -1,
        *,
        query: Optional[MediaServerQueryOptions] = None,
    ) -> Generator[MediaServerItem | None, Any, None]:
        """
        分页读取包含媒体路径与来源身份的条目，逐项转换为统一模型。

        查询媒体库、读取分页和转换条目中的超时最多尝试 3 次，重试间隔 1 秒。
        读取失败向上传播，防止同步过程将失败误判为媒体库清空。

        :param parent: 媒体库数字标识，不接受空值
        :param start_index: 起始条目索引，空值或负值按 0 处理
        :param limit: 本次最多读取数量；None 或 -1 表示每页 50 条持续读取，其他非正值不产生条目
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :yields: 统一媒体条目
        :raises ValueError: 媒体库标识为空或指定媒体库不存在
        :raises ConnectionError: SDK 客户端未连接
        """
        if not parent:
            raise ValueError("Plex 媒体库 ID 不能为空")
        if not self._plex:
            raise ConnectionError("Plex 服务器未连接")
        plex = self._plex
        try:
            section = self._retry_timeout(
                lambda: plex.library.sectionByID(int(parent)),
                f"获取媒体库 {parent}",
            )
            if not section:
                raise ValueError(f"Plex 媒体库 {parent} 不存在")

            if limit is None or limit == -1:
                fetch_all = True
                page_size = PLEX_LIBRARY_SYNC_PAGE_SIZE
            else:
                fetch_all = False
                if limit <= 0:
                    return None
                page_size = limit
            page_start = max(start_index or 0, 0)
            # section.all() 的默认列表会排除 Media；读取 locations 会让局部对象逐条请求详情。
            search_key = section._buildSearchKey(
                libtype=section.TYPE,
                **(
                    {"includeGuids": query.includes(MediaServerField.IDENTITY)}
                    if query is not None and query.fields is not None
                    else {}
                ),
            )
            while True:
                items = self._retry_timeout(
                    lambda: section.fetchItems(
                        search_key,
                        container_start=page_start,
                        container_size=page_size,
                        maxresults=page_size,
                        params={
                            "excludeElements": PLEX_SYNC_EXCLUDE_ELEMENTS,
                            "skipRefresh": 1,
                            **self._query_params(query),
                        },
                    ),
                    f"读取媒体库 {parent} 的第 {page_start} 项分页",
                )
                if not items:
                    break

                for item in items:
                    if not item:
                        continue
                    if isinstance(item, PlexPartialObject):
                        # 缺失的可选字段不得触发详情重载或 Plex 对网盘文件的重新读取。
                        item._autoReload = False
                    try:
                        media_item = self._retry_timeout(
                            lambda: self.__build_media_server_item(
                                item, **MediaServerQueryOptions.as_kwargs(query)
                            ),
                            f"处理媒体项目 {getattr(item, 'key', '')}",
                        )
                    except Exception as err:
                        logger.error(f"处理媒体项目时出错，同步中止：{str(err)}")
                        raise
                    if media_item is not None:
                        yield media_item

                if not fetch_all or len(items) < page_size:
                    break
                page_start += len(items)
        except Exception as err:
            logger.error(f"同步读取 Plex 媒体库 {parent} 出错：{str(err)}")
            raise

    @staticmethod
    def _is_timeout_exception(error: Exception) -> bool:
        """
        识别 PlexAPI 底层 HTTP 客户端抛出的超时异常。
        """
        return any("timeout" in exception_type.__name__.casefold() for exception_type in type(error).__mro__)

    @staticmethod
    def _retry_timeout(operation: Callable[[], Any], description: str) -> Any:
        """
        只对超时异常重试，避免同步时一次瞬时超时丢失整个媒体库。

        最多尝试 3 次，重试间隔 1 秒；异常类或父类名称包含 timeout 时按超时处理。

        :param operation: 每次尝试执行的无参操作
        :param description: 写入重试日志的操作说明
        :return: 操作的原始结果；非超时异常立即传播，最后一次超时也传播给调用方
        """
        for attempt in range(PLEX_SYNC_TIMEOUT_MAX_ATTEMPTS):
            try:
                return operation()
            except Exception as err:
                if not Plex._is_timeout_exception(err):
                    raise
                if attempt + 1 == PLEX_SYNC_TIMEOUT_MAX_ATTEMPTS:
                    logger.error(f"Plex {description}连续超时，已重试 {attempt} 次：{str(err)}")
                    raise
                logger.warning(
                    f"Plex {description}请求超时，准备第 {attempt + 1} 次重试：{str(err)}"
                )
                time.sleep(PLEX_SYNC_TIMEOUT_RETRY_DELAY_SECONDS)
        return None

    def get_webhook_message(self, form: Any) -> Optional[_SchemaWebhookEventInfo]:
        """
        解析 Plex 表单事件，补全媒体名称、设备、用户和背景图。

        分集使用 grandparentTitle、parentIndex 和 index 生成季集标题；
        存在媒体条目标识时会查询图片，原始负载保留到 json_object 供后续处理。

        :param form: 表单对象，payload 字段为包含 event 和 Metadata 等字段的 JSON
        :return: 统一 Webhook 事件；表单或 payload 为空、JSON 解析失败或没有事件类型时返回 None
        """
        if not form:
            return None
        payload = form.get("payload")
        if not payload:
            return None
        try:
            message = json.loads(payload)
        except Exception as e:
            logger.debug(f"解析plex webhook出错：{str(e)}")
            return None
        eventType = message.get('event')
        if not eventType:
            return None
        logger.debug(f"接收到plex webhook：{message}")
        eventItem = _SchemaWebhookEventInfo(event=eventType, channel="plex")
        if message.get('Metadata'):
            if message.get('Metadata', {}).get('type') == 'episode':
                eventItem.item_type = "TV"
                eventItem.item_name = "%s %s%s %s" % (
                    message.get('Metadata', {}).get('grandparentTitle'),
                    "S" + str(message.get('Metadata', {}).get('parentIndex')),
                    "E" + str(message.get('Metadata', {}).get('index')),
                    message.get('Metadata', {}).get('title'))
                eventItem.item_id = message.get('Metadata', {}).get('key')
                eventItem.season_id = message.get('Metadata', {}).get('parentIndex')
                eventItem.episode_id = message.get('Metadata', {}).get('index')

                if (message.get('Metadata', {}).get('summary')
                        and len(message.get('Metadata', {}).get('summary')) > 100):
                    eventItem.overview = str(message.get('Metadata', {}).get('summary'))[:100] + "..."
                else:
                    eventItem.overview = message.get('Metadata', {}).get('summary')
            else:
                eventItem.item_type = "MOV" if message.get('Metadata',
                                                           {}).get('type') == 'movie' else "SHOW"
                eventItem.item_name = "%s %s" % (
                    message.get('Metadata', {}).get('title'),
                    "(" + str(message.get('Metadata', {}).get('year')) + ")")
                eventItem.item_id = message.get('Metadata', {}).get('key')
                if (message.get('Metadata', {}).get('summary')
                        and len(message.get('Metadata', {}).get('summary')) > 100):
                    eventItem.overview = str(message.get('Metadata', {}).get('summary'))[:100] + "..."
                else:
                    eventItem.overview = message.get('Metadata', {}).get('summary')
        if message.get('Player'):
            eventItem.ip = message.get('Player').get('publicAddress')
            eventItem.client = message.get('Player').get('title')
            # 这里给个空,防止拼消息的时候出现None
            eventItem.device_name = ' '
        if message.get('Account'):
            eventItem.user_name = message.get("Account").get('title')

        # 获取消息图片
        if eventItem.item_id:
            # 根据返回的item_id去调用媒体服务器获取
            eventItem.image_url = self.get_remote_image_by_id(item_id=eventItem.item_id,
                                                              image_type="Backdrop")

        eventItem.json_object = message

        return eventItem

    def get_plex(self) -> Optional[PlexServer]:
        """
        提供当前 Plex SDK 客户端供扩展调用。

        :return: 已建立的 SDK 客户端，未连接时为 None；调用方不得据此推断本次请求必然成功
        """
        return self._plex

    def get_play_url(self, item_id: str) -> str:
        """
        构造带访问令牌的 Plex 网页播放链接。

        :param item_id: Plex 条目标识，可以是数字标识或 /library/metadata/ 路径
        :return: 以外网播放地址或服务地址为根的条目链接；调用前须已经建立 SDK 连接
        """
        if self._plex is None:
            raise AttributeError("'NoneType' object has no attribute 'machineIdentifier'")
        return f'{self._playhost or self._host}web/index.html#!/server/{self._plex.machineIdentifier}/details?key={item_id}&X-Plex-Token={self._token}'

    def get_resume(
        self, num: Optional[int] = 12, *, query: Optional[MediaServerQueryOptions] = None
    ) -> Optional[List[_SchemaMediaServerPlayItem]]:
        """
        获取同步范围内可以继续观看的媒体。

        只使用列表已有字段，缺失的年份、图片或时长不触发逐条详情请求。

        :param num: 最多返回条目数量，默认 12
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :return: 包含观看进度百分比的媒体列表；未连接或无法读取媒体库范围时返回 None，SDK 请求异常向上传播
        """
        if not self._plex:
            return None
        # 媒体库白名单
        options = query or MediaServerQueryOptions()
        libraries = self.get_librarys(
            hidden=True,
            **(
                {"query": MediaServerQueryOptions(fields=frozenset())}
                if query is not None and query.fields is not None
                else {}
            ),
        )
        if libraries is None:
            return None
        allow_library = ",".join(map(str, (lib.id for lib in libraries)))
        params = {"contentDirectoryID": allow_library, **self._query_params(query)}
        items = self._plex.fetchItems(
            "/hubs/continueWatching/items", container_start=0, container_size=num, maxresults=num, params=params
        )
        ret_resume: List[_SchemaMediaServerPlayItem] = []
        for item in items:
            if isinstance(item, PlexPartialObject):
                # SDK 会为缺失属性同步重载详情，继续观看展示不能等待额外的媒体探测。
                item._autoReload = False
            item_type = MediaType.MOVIE.value if item.TYPE == "movie" else MediaType.TV.value
            if item_type == MediaType.MOVIE.value:
                title = item.title
                subtitle = str(item.year) if item.year else None
            else:
                title = item.grandparentTitle
                subtitle = f"S{item.parentIndex}:E{item.index} - {item.title}"
            link = self.get_play_url(item.key) if options.includes(MediaServerField.LINK) else None
            image = item.artUrl if options.includes(MediaServerField.IMAGES) else None
            ret_resume.append(
                _SchemaMediaServerPlayItem(
                    id=item.key,
                    title=title,
                    subtitle=subtitle,
                    type=item_type,
                    image=image,
                    link=link,
                    percent=item.viewOffset / item.duration * 100 if item.viewOffset and item.duration else 0,
                    server_type="plex",
                )
            )
        return ret_resume[:num]

    def get_latest(
        self, num: Optional[int] = 20, *, query: Optional[MediaServerQueryOptions] = None
    ) -> Optional[List[_SchemaMediaServerPlayItem]]:
        """
        在允许同步的媒体库中汇总最近新增条目。

        通过 promoted 分组排除继续观看项，按 addedAt 降序汇总；SDK 请求异常向调用方传播。

        :param num: 最多返回条目数量，默认 20
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :return: 按获取到的推荐分组内容排序后的最新条目列表；未连接或无法读取库范围时返回 None
        """
        if not self._plex:
            return None
        # 请求参数（除黑名单）
        libraries = self.get_librarys(
            hidden=True,
            **(
                {"query": MediaServerQueryOptions(fields=frozenset())}
                if query is not None and query.fields is not None
                else {}
            ),
        )
        if libraries is None:
            return None
        allow_library = ",".join(map(str, (lib.id for lib in libraries)))
        params = {
            "contentDirectoryID": allow_library,
            "count": num,
            "excludeContinueWatching": 1,
            **self._query_params(query),
        }
        ret_resume: List[_SchemaMediaServerPlayItem] = []
        sub_result = []
        offset = 0
        if num is None:
            raise TypeError("'>=' not supported between instances of 'int' and 'NoneType'")
        while True:
            if len(ret_resume) >= num:
                break
            # 获取所有资料库
            hubs = self._plex.fetchItems(
                "/hubs/promoted", container_start=offset, container_size=num, maxresults=num, params=params
            )
            if len(hubs) == 0:
                break

            # 合并排序
            for hub in hubs:
                for item in hub.items():
                    sub_result.append(item)
            sub_result.sort(key=lambda x: x.addedAt, reverse=True)

            for item in sub_result:
                self._disable_auto_reload(item, query)
                if len(ret_resume) >= num:
                    break
                ret_resume.append(self._latest_play_item(item, query))
            offset += num
        return ret_resume[:num]

    def _latest_play_item(self, item: Any, query: Optional[MediaServerQueryOptions]) -> _SchemaMediaServerPlayItem:
        """
        将最新入库的 SDK 对象转换为展示条目，按需构造图片与跳转链接。

        :param item: Plex 电影、季、分集或剧集对象
        :param query: 可选内容投影；默认保留原展示字段
        :return: 统一最新入库条目
        """
        options = query or MediaServerQueryOptions()
        item_type, title = "", ""
        image: Optional[str] = ""
        if item.TYPE == "movie":
            item_type = MediaType.MOVIE.value
            title = item.title
            image = item.posterUrl if options.includes(MediaServerField.IMAGES) else None
        elif item.TYPE == "season":
            item_type = MediaType.TV.value
            title = "%s 第%s季" % (item.parentTitle, item.index)
            image = item.posterUrl if options.includes(MediaServerField.IMAGES) else None
        elif item.TYPE == "episode":
            item_type = MediaType.TV.value
            title = "%s 第%s季 第%s集" % (item.grandparentTitle, item.parentIndex, item.index)
            thumb = (
                (item.parentThumb or item.grandparentThumb or "").lstrip("/")
                if options.includes(MediaServerField.IMAGES)
                else ""
            )
            image = (
                (self._host + thumb + f"?X-Plex-Token={self._token}")
                if options.includes(MediaServerField.IMAGES)
                else None
            )
        elif item.TYPE == "show":
            item_type = MediaType.TV.value
            title = "%s 共%s季" % (item.title, item.seasonCount)
            image = item.posterUrl if options.includes(MediaServerField.IMAGES) else None
        link = self.get_play_url(item.key) if options.includes(MediaServerField.LINK) else None
        return _SchemaMediaServerPlayItem(
            id=item.key,
            title=title,
            subtitle=str(item.year) if item.year else None,
            type=item_type,
            image=image,
            link=link,
            server_type="plex",
        )

    def get_data(self, endpoint: str, **kwargs: object) -> Optional[Any]:
        """
        使用复用的 HTTP 会话发送自定义 GET 请求。

        :param endpoint: 相对于服务根地址的端点或请求 URL
        :param kwargs: 透传给 RequestUtils 的请求参数，可覆盖默认请求头和异常选项
        :return: 原始响应，由调用方读取和关闭；会话未创建或请求异常时返回 None
        """
        return self.__request(method="get", endpoint=endpoint, **kwargs)

    def post_data(self, endpoint: str, **kwargs: object) -> Optional[Any]:
        """
        使用复用的 HTTP 会话发送自定义 POST 请求。

        :param endpoint: 相对于服务根地址的端点或请求 URL
        :param kwargs: 透传给 RequestUtils 的请求参数，可覆盖默认请求头和异常选项
        :return: 原始响应，由调用方读取和关闭；会话未创建或请求异常时返回 None
        """
        return self.__request(method="post", endpoint=endpoint, **kwargs)

    def put_data(self, endpoint: str, **kwargs: object) -> Optional[Any]:
        """
        使用复用的 HTTP 会话发送自定义 PUT 请求。

        :param endpoint: 相对于服务根地址的端点或请求 URL
        :param kwargs: 透传给 RequestUtils 的请求参数，可覆盖默认请求头和异常选项
        :return: 原始响应，由调用方读取和关闭；会话未创建或请求异常时返回 None
        """
        return self.__request(method="put", endpoint=endpoint, **kwargs)

    def __request(self, method: str, endpoint: str, **kwargs: object) -> Optional[Any]:
        """
        为自定义 Plex 请求补充令牌请求头，并记录请求异常。

        :param method: 请求工具支持的方法名，如 get、post 或 put
        :param endpoint: 相对服务根地址的端点或请求 URL
        :param kwargs: 请求参数；未显式提供时添加认证请求头并启用异常抛出，再在此边界捕获
        :return: 原始响应；会话不可用、方法不存在或请求失败时返回 None
        """
        if not self._request_utils:
            return None
        try:
            url = UrlUtils.adapt_request_url(host=self._host, endpoint=endpoint)
            kwargs.setdefault("headers", self.__get_request_headers())
            kwargs.setdefault("raise_exception", True)
            request_method = getattr(self._request_utils, f"{method}_res", None)
            if request_method:
                return request_method(url=url, **kwargs)
            else:
                logger.error(f"方法 {method} 不存在")
                return None
        except Exception as e:
            logger.error("连接Plex出错：" + str(e))
            return None

    def __get_request_headers(self) -> dict[str, str]:
        """
        获取请求头。
        """
        return {
            "X-Plex-Token": self._token,
            "Accept": "application/json",
            "Content-Type": "application/json"
        }

    def close(self) -> None:
        """
        关闭自定义 HTTP 请求会话；不主动关闭 Plex SDK 客户端。
        """
        if self._request_utils:
            self._request_utils.close()
