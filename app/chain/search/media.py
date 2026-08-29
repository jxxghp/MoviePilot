"""精确媒体搜索与同步异步编排 owner。"""

import asyncio
import random
import time
from dataclasses import dataclass
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Dict,
    Generator,
    List,
    Optional,
    Tuple,
    Union,
    cast,
)

from app.chain.media import MediaChain
from app.chain.search.contract import _SearchOwnerBase as _SearchOwnerBase
from app.domain.context import Context, MediaInfo, MusicInfo, TorrentInfo
from app.domain.metainfo import MetaInfo
from app.runtime.execution import run_in_threadpool
from app.runtime.log import logger
from app.schemas.media import build_media_key, resolve_media_identity
from app.schemas.mediaserver import NotExistMediaInfo
from app.schemas.types import (
    MediaSource,
    MediaType,
)


def _build_id_search_params(
    media_source: MediaSource,
    media_id: str,
    mtype: Optional[MediaType],
    area: Optional[str],
    season: Optional[int],
    sites: Optional[List[int]],
    music_type: Optional[str],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """构造同步、异步和流式 ID 搜索共享的识别与缓存参数。"""
    recognition_params = {
        "media_source": media_source,
        "media_id": media_id,
        "mtype": mtype,
        "music_type": music_type,
    }
    cache_params = {
        **recognition_params,
        "area": area,
        "season": season,
        "sites": sites,
    }
    return recognition_params, cache_params


def _build_missing_media_map(
    mediainfo: MediaInfo,
    season: Optional[int],
) -> Optional[Dict[str, Dict[int, NotExistMediaInfo]]]:
    """将指定季转换为统一的缺集搜索映射。"""
    if season is None:
        return None
    resolved_source, resolved_id = resolve_media_identity(media=mediainfo)
    return {
        build_media_key(resolved_source, resolved_id): {
            season: NotExistMediaInfo(episodes=[])
        }
    }


@dataclass(frozen=True, slots=True)
class _IdSearchCacheRequest:
    """请求保存 ID 搜索参数并清理旧的 AI 推荐状态。"""

    params: Dict[str, Any]


@dataclass(frozen=True, slots=True)
class _IdSearchRecognizeRequest:
    """请求通过媒体链识别一个来源原生 ID。"""

    params: Dict[str, Any]


@dataclass(frozen=True, slots=True)
class _IdSearchProcessRequest:
    """请求执行已识别媒体的精确资源搜索。"""

    params: Dict[str, Any]


@dataclass(frozen=True, slots=True)
class _IdSearchSaveRequest:
    """请求持久化成功的 ID 搜索结果。"""

    contexts: List[Context]


@dataclass(frozen=True, slots=True)
class _IdSearchResult:
    """冻结 ID 搜索状态机结果与识别失败提示。"""

    contexts: List[Context]
    warning: Optional[str] = None


_IdSearchRequest = Union[
    _IdSearchCacheRequest,
    _IdSearchRecognizeRequest,
    _IdSearchProcessRequest,
    _IdSearchSaveRequest,
]
_IdSearchResolution = Generator[_IdSearchRequest, object, _IdSearchResult]


def _id_search_resolution(
    recognition_params: Dict[str, Any],
    cache_params: Dict[str, Any],
    season: Optional[int],
    sites: Optional[List[int]],
    area: Optional[str],
    cache_local: bool,
    failure_keyword: str,
) -> _IdSearchResolution:
    """统一 ID 搜索的缓存、识别、处理、失败短路与结果保存顺序。"""
    if cache_local:
        yield _IdSearchCacheRequest(params=cache_params)
    mediainfo = cast(
        Optional[MediaInfo],
        (yield _IdSearchRecognizeRequest(params=recognition_params)),
    )
    if not mediainfo:
        return _IdSearchResult(
            contexts=[],
            warning=f"{failure_keyword} 媒体信息识别失败！",
        )
    contexts = cast(
        List[Context],
        (yield _IdSearchProcessRequest(
            params={
                "mediainfo": mediainfo,
                "sites": sites,
                "area": area,
                "no_exists": _build_missing_media_map(mediainfo, season),
            }
        )),
    )
    if cache_local:
        yield _IdSearchSaveRequest(contexts=contexts)
    return _IdSearchResult(contexts=contexts)


def _normalize_media_search_input(mediainfo: MediaInfo) -> MediaInfo:
    """归一化非 TMDB 输入标题，并保留调用方对象由外层复制的所有权约束。"""
    if not mediainfo.tmdb_id:
        meta = MetaInfo(title=mediainfo.title)
        mediainfo.title = meta.name
        mediainfo.season = cast(int, meta.begin_season)
    return mediainfo


def _should_stop_keyword_search(
    search_multiple_name: bool,
    torrents: List[TorrentInfo],
) -> bool:
    """统一判断首个有效关键字结果是否终止后续搜索。"""
    return not search_multiple_name and bool(torrents)


@dataclass(frozen=True, slots=True)
class _KeywordSearchRequest:
    """描述共享关键字状态机交给真实 I/O 边界的一次请求。"""

    keyword: str
    search_count: int


@dataclass(frozen=True, slots=True)
class _KeywordSearchResult:
    """冻结关键字状态机聚合结果及其提前停止决策。"""

    torrents: List[TorrentInfo]
    stopped_early: bool


_KeywordSearchResolution = Generator[
    _KeywordSearchRequest, List[TorrentInfo], _KeywordSearchResult
]


def _keyword_search_resolution(
    keywords: List[str],
    search_multiple_name: bool,
) -> _KeywordSearchResolution:
    """统一推进关键字顺序、结果聚合和首个有效结果短路。"""
    torrents: List[TorrentInfo] = []
    for search_count, keyword in enumerate(keywords):
        torrents.extend(
            (yield _KeywordSearchRequest(
                keyword=keyword,
                search_count=search_count,
            ))
        )
        if _should_stop_keyword_search(search_multiple_name, torrents):
            return _KeywordSearchResult(torrents=torrents, stopped_early=True)
    return _KeywordSearchResult(torrents=torrents, stopped_early=False)


def _run_keyword_search_sync(
    resolution: _KeywordSearchResolution,
    execute: Callable[[_KeywordSearchRequest], List[TorrentInfo]],
) -> _KeywordSearchResult:
    """通过同步 provider 驱动共享关键字状态机。"""
    try:
        request = next(resolution)
    except StopIteration as outcome:
        return cast(_KeywordSearchResult, outcome.value)
    while True:
        try:
            request = resolution.send(execute(request))
        except StopIteration as outcome:
            return cast(_KeywordSearchResult, outcome.value)


async def _run_keyword_search_async(
    resolution: _KeywordSearchResolution,
    execute: Callable[[_KeywordSearchRequest], Awaitable[List[TorrentInfo]]],
) -> _KeywordSearchResult:
    """通过异步 provider 驱动共享关键字状态机。"""
    try:
        request = next(resolution)
    except StopIteration as outcome:
        return cast(_KeywordSearchResult, outcome.value)
    while True:
        try:
            request = resolution.send(await execute(request))
        except StopIteration as outcome:
            return cast(_KeywordSearchResult, outcome.value)


@dataclass(frozen=True, slots=True)
class _MediaProcessPlan:
    """冻结媒体资源处理入口的业务输入。"""

    mediainfo: MediaInfo
    keyword: Optional[str]
    no_exists: Optional[Dict[str, Dict[int, NotExistMediaInfo]]]
    sites: Optional[List[int]]
    rule_groups: Optional[List[str]]
    area: Optional[str]
    custom_words: Optional[List[str]]
    filter_params: Optional[Dict[str, str]]


@dataclass(frozen=True, slots=True)
class _MediaMusicProcessRequest:
    """请求执行音乐资源搜索外壳。"""

    params: Dict[str, Any]


@dataclass(frozen=True, slots=True)
class _MediaRecognizeRequest:
    """请求补齐缺失名称的媒体信息。"""

    params: Dict[str, Any]


@dataclass(frozen=True, slots=True)
class _MediaSupplementRequest:
    """请求聚合已启用媒体来源的附加信息。"""

    mediainfo: MediaInfo


@dataclass(frozen=True, slots=True)
class _MediaKeywordProcessRequest:
    """请求按共享关键字计划执行 provider I/O。"""

    mediainfo: MediaInfo
    keywords: List[str]
    sites: Optional[List[int]]
    area: Optional[str]
    search_multiple_name: bool


@dataclass(frozen=True, slots=True)
class _MediaParseRequest:
    """请求在同步或线程池 CPU 外壳中解析搜索结果。"""

    params: Dict[str, Any]


@dataclass(frozen=True, slots=True)
class _MediaLogRequest:
    """请求记录共享状态机决定的运行日志。"""

    message: str


@dataclass(frozen=True, slots=True)
class _MediaProcessResult:
    """冻结媒体处理结果与识别失败状态。"""

    contexts: List[Context]
    recognition_failed: bool = False


_MediaProcessRequest = Union[
    _MediaMusicProcessRequest,
    _MediaRecognizeRequest,
    _MediaSupplementRequest,
    _MediaKeywordProcessRequest,
    _MediaParseRequest,
    _MediaLogRequest,
]
_MediaProcessResolution = Generator[
    _MediaProcessRequest, object, _MediaProcessResult
]


def _media_process_resolution(
    plan: _MediaProcessPlan,
    search_multiple_name: Callable[[], bool],
    copy_media: Callable[[MediaInfo], MediaInfo],
    recognize_kwargs: Callable[[MediaInfo], Dict[str, Any]],
    prepare_params: Callable[..., Tuple[Optional[Dict[int, List[int]]], List[str]]],
) -> _MediaProcessResolution:
    """统一媒体处理的类型路由、识别、补充、搜索和解析状态。"""
    if plan.mediainfo.type == MediaType.MUSIC:
        contexts = cast(
            List[Context],
            (yield _MediaMusicProcessRequest(
                params={
                    "mediainfo": cast(MusicInfo, plan.mediainfo),
                    "keyword": plan.keyword,
                    "sites": plan.sites,
                    "rule_groups": plan.rule_groups,
                    "filter_params": plan.filter_params,
                }
            )),
        )
        return _MediaProcessResult(contexts=contexts)

    mediainfo = _normalize_media_search_input(copy_media(plan.mediainfo))
    yield _MediaLogRequest(
        message=f"开始搜索资源，关键词：{plan.keyword or mediainfo.title} ..."
    )
    if not mediainfo.names:
        recognized_media = cast(
            Optional[MediaInfo],
            (yield _MediaRecognizeRequest(
                params={
                    "mtype": mediainfo.type,
                    **recognize_kwargs(mediainfo),
                }
            )),
        )
        if not recognized_media:
            return _MediaProcessResult(contexts=[], recognition_failed=True)
        mediainfo = recognized_media

    mediainfo = cast(
        Optional[MediaInfo],
        (yield _MediaSupplementRequest(mediainfo=mediainfo)),
    ) or mediainfo
    season_episodes, keywords = prepare_params(
        mediainfo=mediainfo,
        keyword=plan.keyword,
        no_exists=plan.no_exists,
    )
    outcome = cast(
        _KeywordSearchResult,
        (yield _MediaKeywordProcessRequest(
            mediainfo=mediainfo,
            keywords=keywords,
            sites=plan.sites,
            area=plan.area,
            search_multiple_name=search_multiple_name(),
        )),
    )
    if outcome.stopped_early:
        yield _MediaLogRequest(
            message=f"共搜索到 {len(outcome.torrents)} 个资源，停止搜索"
        )
    contexts = cast(
        List[Context],
        (yield _MediaParseRequest(
            params=_build_result_params(
                torrents=outcome.torrents,
                mediainfo=mediainfo,
                keyword=plan.keyword,
                rule_groups=plan.rule_groups,
                season_episodes=season_episodes,
                custom_words=plan.custom_words,
                filter_params=plan.filter_params,
            )
        )),
    )
    return _MediaProcessResult(contexts=contexts)


def _build_result_params(
    torrents: List[TorrentInfo],
    mediainfo: MediaInfo,
    keyword: Optional[str],
    rule_groups: Optional[List[str]],
    season_episodes: Optional[Dict[int, List[int]]],
    custom_words: Optional[List[str]],
    filter_params: Optional[Dict[str, str]],
) -> Dict[str, Any]:
    """构造同步、异步和流式结果解析共享的参数快照。"""
    return {
        "torrents": torrents,
        "mediainfo": mediainfo,
        "keyword": keyword,
        "rule_groups": rule_groups,
        "season_episodes": season_episodes,
        "custom_words": custom_words,
        "filter_params": filter_params,
    }


def _build_candidate_contexts(
    mediainfo: MediaInfo,
    torrents: List[TorrentInfo],
) -> List[Context]:
    """将流式站点候选映射为尚未精确过滤的搜索上下文。"""
    return [
        Context(
            meta_info=MetaInfo(title=torrent.title, subtitle=torrent.description),
            media_info=mediainfo,
            torrent_info=torrent,
            resource_source="search",
            media_info_is_target=True,
        )
        for torrent in torrents
    ]


class SearchMediaOwner(_SearchOwnerBase):
    """精确媒体搜索与同步异步编排 owner。"""

    def _run_id_search_sync(
        self, resolution: _IdSearchResolution
    ) -> _IdSearchResult:
        """用同步 I/O 外壳驱动共享 ID 搜索状态机。"""
        response: object = None
        while True:
            try:
                request = resolution.send(response)
            except StopIteration as completed:
                return cast(_IdSearchResult, completed.value)
            if isinstance(request, _IdSearchCacheRequest):
                self.cancel_ai_recommend()
                self.save_last_search_params(**request.params)
                response = None
            elif isinstance(request, _IdSearchRecognizeRequest):
                response = MediaChain().recognize_media(**request.params)
            elif isinstance(request, _IdSearchProcessRequest):
                response = self.process(**request.params)
            else:
                self._save_results(request.contexts)
                response = None

    async def _run_id_search_async(
        self, resolution: _IdSearchResolution
    ) -> _IdSearchResult:
        """用异步 I/O 外壳驱动共享 ID 搜索状态机。"""
        response: object = None
        while True:
            try:
                request = resolution.send(response)
            except StopIteration as completed:
                return cast(_IdSearchResult, completed.value)
            if isinstance(request, _IdSearchCacheRequest):
                self.cancel_ai_recommend()
                await self.async_save_last_search_params(**request.params)
                response = None
            elif isinstance(request, _IdSearchRecognizeRequest):
                response = await MediaChain().async_recognize_media(**request.params)
            elif isinstance(request, _IdSearchProcessRequest):
                response = await self.async_process(**request.params)
            else:
                await self._async_save_results(request.contexts)
                response = None

    def search_by_id(
        self,
        media_source: MediaSource,
        media_id: str,
        mtype: Optional[MediaType] = None,
        area: Optional[str] = "title",
        season: Optional[int] = None,
        sites: Optional[List[int]] = None,
        cache_local: bool = False,
        music_type: Optional[str] = None,
    ) -> List[Context]:
        """
        根据数据源媒体 ID 搜索资源，精确匹配，不过滤本地存在的资源
        :param media_source: 媒体数据源
        :param media_id: 数据源原生 ID
        :param music_type: 音乐实体类型
        :param mtype: 媒体，电影 or 电视剧
        :param area: 搜索范围，title or imdbid
        :param season: 季数
        :param sites: 站点ID列表
        :param cache_local: 是否缓存到本地
        """
        recognition_params, cache_params = _build_id_search_params(
            media_source=media_source,
            media_id=media_id,
            mtype=mtype,
            area=area,
            season=season,
            sites=sites,
            music_type=music_type,
        )
        result = SearchMediaOwner._run_id_search_sync(
            self,
            _id_search_resolution(
                recognition_params=recognition_params,
                cache_params=cache_params,
                season=season,
                sites=sites,
                area=area,
                cache_local=cache_local,
                failure_keyword=self._build_search_keyword(
                    media_source, media_id
                ),
            )
        )
        if result.warning:
            logger.error(result.warning)
        return result.contexts

    async def async_search_by_id(
        self,
        media_source: MediaSource,
        media_id: str,
        mtype: Optional[MediaType] = None,
        area: Optional[str] = "title",
        season: Optional[int] = None,
        sites: Optional[List[int]] = None,
        cache_local: bool = False,
        music_type: Optional[str] = None,
    ) -> List[Context]:
        """
        根据数据源媒体 ID 异步搜索资源，精确匹配，不过滤本地存在的资源
        :param media_source: 媒体数据源
        :param media_id: 数据源原生 ID
        :param music_type: 音乐实体类型
        :param mtype: 媒体，电影 or 电视剧
        :param area: 搜索范围，title or imdbid
        :param season: 季数
        :param sites: 站点ID列表
        :param cache_local: 是否缓存到本地
        """
        recognition_params, cache_params = _build_id_search_params(
            media_source=media_source,
            media_id=media_id,
            mtype=mtype,
            area=area,
            season=season,
            sites=sites,
            music_type=music_type,
        )
        result = await SearchMediaOwner._run_id_search_async(
            self,
            _id_search_resolution(
                recognition_params=recognition_params,
                cache_params=cache_params,
                season=season,
                sites=sites,
                area=area,
                cache_local=cache_local,
                failure_keyword=self._build_search_keyword(
                    media_source, media_id
                ),
            )
        )
        if result.warning:
            logger.error(result.warning)
        return result.contexts

    async def async_search_by_id_stream(
        self,
        media_source: MediaSource,
        media_id: str,
        mtype: Optional[MediaType] = None,
        area: Optional[str] = "title",
        season: Optional[int] = None,
        sites: Optional[List[int]] = None,
        cache_local: bool = False,
        music_type: Optional[str] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """
        根据数据源媒体 ID 渐进式搜索资源，先返回站点原始候选，再返回过滤匹配后的最终结果
        """
        recognition_params, cache_params = _build_id_search_params(
            media_source=media_source,
            media_id=media_id,
            mtype=mtype,
            area=area,
            season=season,
            sites=sites,
            music_type=music_type,
        )
        if cache_local:
            self.cancel_ai_recommend()
            await self.async_save_last_search_params(**cache_params)
        # 音乐统一在 MediaChain.async_recognize_media 内按固定来源路由
        mediainfo = await MediaChain().async_recognize_media(**recognition_params)
        if not mediainfo:
            logger.error(f"{self._build_search_keyword(media_source, media_id)} 媒体信息识别失败！")
            yield {"type": "error", "success": False, "message": "媒体信息识别失败"}
            return

        no_exists = _build_missing_media_map(mediainfo, season)

        contexts: List[Context] = []
        async for event in self.async_process_stream(mediainfo=mediainfo, sites=sites, area=area, no_exists=no_exists):
            if event.get("type") == "done":
                contexts = event.get("contexts") or []
                event = {key: value for key, value in event.items() if key != "contexts"}
            yield event

        if cache_local:
            await self._async_save_results(contexts)

    def _run_media_process_sync(
        self, resolution: _MediaProcessResolution
    ) -> _MediaProcessResult:
        """用同步 provider 与 CPU 外壳驱动共享媒体处理状态机。"""
        response: object = None
        while True:
            try:
                request = resolution.send(response)
            except StopIteration as completed:
                return cast(_MediaProcessResult, completed.value)
            if isinstance(request, _MediaMusicProcessRequest):
                response = self._process_music(**request.params)
            elif isinstance(request, _MediaRecognizeRequest):
                response = MediaChain().recognize_media(**request.params)
            elif isinstance(request, _MediaSupplementRequest):
                response = MediaChain().supplement_media_info(request.mediainfo)
            elif isinstance(request, _MediaKeywordProcessRequest):

                def execute_search(
                    keyword_request: _KeywordSearchRequest,
                ) -> List[TorrentInfo]:
                    """执行共享关键字请求的同步站点 I/O。"""
                    if keyword_request.search_count > 0:
                        logger.info(
                            f"已搜索 {keyword_request.search_count} 次，"
                            "强制休眠 1-10 秒 ..."
                        )
                        time.sleep(random.randint(1, 10))
                    return (
                        self._SearchChain__search_all_sites(
                            mediainfo=request.mediainfo,
                            keyword=keyword_request.keyword,
                            sites=request.sites,
                            area=request.area,
                        )
                        or []
                    )

                response = _run_keyword_search_sync(
                    _keyword_search_resolution(
                        request.keywords, request.search_multiple_name
                    ),
                    execute_search,
                )
            elif isinstance(request, _MediaParseRequest):
                response = self._parse_result(**request.params)
            else:
                logger.info(request.message)
                response = None

    async def _run_media_process_async(
        self, resolution: _MediaProcessResolution
    ) -> _MediaProcessResult:
        """用异步 provider 与线程池 CPU 外壳驱动共享媒体处理状态机。"""
        response: object = None
        while True:
            try:
                request = resolution.send(response)
            except StopIteration as completed:
                return cast(_MediaProcessResult, completed.value)
            if isinstance(request, _MediaMusicProcessRequest):
                response = await self._async_process_music(**request.params)
            elif isinstance(request, _MediaRecognizeRequest):
                response = await MediaChain().async_recognize_media(
                    **request.params
                )
            elif isinstance(request, _MediaSupplementRequest):
                response = await MediaChain().async_supplement_media_info(
                    request.mediainfo
                )
            elif isinstance(request, _MediaKeywordProcessRequest):

                async def execute_search(
                    keyword_request: _KeywordSearchRequest,
                ) -> List[TorrentInfo]:
                    """执行共享关键字请求的异步站点 I/O。"""
                    if keyword_request.search_count > 0:
                        logger.info(
                            f"已搜索 {keyword_request.search_count} 次，"
                            "强制休眠 1-10 秒 ..."
                        )
                        await asyncio.sleep(random.randint(1, 10))
                    return (
                        await self._SearchChain__async_search_all_sites(
                            mediainfo=request.mediainfo,
                            keyword=keyword_request.keyword,
                            sites=request.sites,
                            area=request.area,
                        )
                        or []
                    )

                response = await _run_keyword_search_async(
                    _keyword_search_resolution(
                        request.keywords, request.search_multiple_name
                    ),
                    execute_search,
                )
            elif isinstance(request, _MediaParseRequest):
                response = await run_in_threadpool(
                    self._parse_result, **request.params
                )
            else:
                logger.info(request.message)
                response = None

    def process(
        self,
        mediainfo: MediaInfo,
        keyword: Optional[str] = None,
        no_exists: Optional[Dict[str, Dict[int, NotExistMediaInfo]]] = None,
        sites: Optional[List[int]] = None,
        rule_groups: Optional[List[str]] = None,
        area: Optional[str] = "title",
        custom_words: Optional[List[str]] = None,
        filter_params: Optional[Dict[str, str]] = None,
    ) -> List[Context]:
        """
        根据媒体信息搜索种子资源，精确匹配，应用过滤规则，同时根据no_exists过滤本地已存在的资源
        :param mediainfo: 媒体信息
        :param keyword: 搜索关键词
        :param no_exists: 缺失的媒体信息
        :param sites: 站点ID列表，为空时搜索所有站点
        :param rule_groups: 过滤规则组名称列表
        :param area: 搜索范围，title or imdbid
        :param custom_words: 自定义识别词列表
        :param filter_params: 过滤参数
        """
        result = SearchMediaOwner._run_media_process_sync(
            self,
            _media_process_resolution(
                plan=_MediaProcessPlan(
                    mediainfo=mediainfo,
                    keyword=keyword,
                    no_exists=no_exists,
                    sites=sites,
                    rule_groups=rule_groups,
                    area=area,
                    custom_words=custom_words,
                    filter_params=filter_params,
                ),
                search_multiple_name=(
                    lambda: self.runtime_config.search_multiple_name
                ),
                copy_media=self._copy_media_input,
                recognize_kwargs=self._media_recognize_kwargs,
                prepare_params=self._prepare_params,
            )
        )
        if result.recognition_failed:
            logger.error("媒体信息识别失败！")
        return result.contexts

    async def async_process(
        self,
        mediainfo: MediaInfo,
        keyword: Optional[str] = None,
        no_exists: Optional[Dict[str, Dict[int, NotExistMediaInfo]]] = None,
        sites: Optional[List[int]] = None,
        rule_groups: Optional[List[str]] = None,
        area: Optional[str] = "title",
        custom_words: Optional[List[str]] = None,
        filter_params: Optional[Dict[str, str]] = None,
    ) -> List[Context]:
        """
        根据媒体信息异步搜索种子资源，精确匹配，应用过滤规则，同时根据no_exists过滤本地已存在的资源
        :param mediainfo: 媒体信息
        :param keyword: 搜索关键词
        :param no_exists: 缺失的媒体信息
        :param sites: 站点ID列表，为空时搜索所有站点
        :param rule_groups: 过滤规则组名称列表
        :param area: 搜索范围，title or imdbid
        :param custom_words: 自定义识别词列表
        :param filter_params: 过滤参数
        """
        result = await SearchMediaOwner._run_media_process_async(
            self,
            _media_process_resolution(
                plan=_MediaProcessPlan(
                    mediainfo=mediainfo,
                    keyword=keyword,
                    no_exists=no_exists,
                    sites=sites,
                    rule_groups=rule_groups,
                    area=area,
                    custom_words=custom_words,
                    filter_params=filter_params,
                ),
                search_multiple_name=(
                    lambda: self.runtime_config.search_multiple_name
                ),
                copy_media=self._copy_media_input,
                recognize_kwargs=self._media_recognize_kwargs,
                prepare_params=self._prepare_params,
            )
        )
        if result.recognition_failed:
            logger.error("媒体信息识别失败！")
        return result.contexts

    async def async_process_stream(
        self,
        mediainfo: MediaInfo,
        keyword: Optional[str] = None,
        no_exists: Optional[Dict[str, Dict[int, NotExistMediaInfo]]] = None,
        sites: Optional[List[int]] = None,
        rule_groups: Optional[List[str]] = None,
        area: Optional[str] = "title",
        custom_words: Optional[List[str]] = None,
        filter_params: Optional[Dict[str, str]] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """
        根据媒体信息渐进式搜索种子资源，先返回站点候选，再返回过滤匹配后的最终结果
        """

        if mediainfo.type == MediaType.MUSIC:
            async for event in self._async_process_music_stream(
                mediainfo=cast(MusicInfo, mediainfo),
                keyword=keyword,
                sites=sites,
                rule_groups=rule_groups,
                filter_params=filter_params,
            ):
                yield event
            return

        mediainfo = _normalize_media_search_input(self._copy_media_input(mediainfo))
        logger.info(f"开始渐进式搜索资源，关键词：{keyword or mediainfo.title} ...")

        # 补充媒体信息
        if not mediainfo.names:
            recognized_media = await MediaChain().async_recognize_media(
                mtype=mediainfo.type,
                **self._media_recognize_kwargs(mediainfo),
            )
            if not recognized_media:
                logger.error("媒体信息识别失败！")
                yield {"type": "error", "success": False, "message": "媒体信息识别失败"}
                return
            mediainfo = recognized_media

        mediainfo = cast(
            MediaInfo,
            await MediaChain().async_supplement_media_info(mediainfo) or mediainfo,
        )

        # 准备搜索参数
        season_episodes, keywords = self._prepare_params(mediainfo=mediainfo, keyword=keyword, no_exists=no_exists)

        candidate_contexts: List[Context] = []
        resolution = _keyword_search_resolution(
            keywords, self.runtime_config.search_multiple_name
        )
        outcome = _KeywordSearchResult(torrents=[], stopped_early=False)
        try:
            request = next(resolution)
        except StopIteration as completed:
            outcome = cast(_KeywordSearchResult, completed.value)
        else:
            while True:
                if request.search_count > 0:
                    logger.info(
                        f"已搜索 {request.search_count} 次，强制休眠 1-10 秒 ..."
                    )
                    await asyncio.sleep(random.randint(1, 10))
                search_results: List[TorrentInfo] = []
                async for event in self._SearchChain__async_search_all_sites_stream(
                    mediainfo=mediainfo,
                    keyword=request.keyword,
                    sites=sites,
                    area=area,
                ):
                    result = event.pop("items", []) or []
                    search_results.extend(result)
                    batch_contexts = _build_candidate_contexts(mediainfo, result)
                    candidate_contexts.extend(batch_contexts)
                    yield {
                        **event,
                        "type": "append",
                        "stage": "searching",
                        "items": [
                            cast(Any, context).to_dict()
                            for context in batch_contexts
                        ],
                        "total_items": len(candidate_contexts),
                    }
                try:
                    request = resolution.send(search_results)
                except StopIteration as completed:
                    outcome = cast(_KeywordSearchResult, completed.value)
                    break
        if outcome.stopped_early:
            logger.info(f"共搜索到 {len(outcome.torrents)} 个资源，停止搜索")

        yield {
            "type": "progress",
            "stage": "filtering",
            "value": 98,
            "text": f"正在过滤匹配 {len(outcome.torrents)} 个候选资源 ...",
        }

        contexts = await run_in_threadpool(
            self._parse_result,
            **_build_result_params(
                torrents=outcome.torrents,
                mediainfo=mediainfo,
                keyword=keyword,
                rule_groups=rule_groups,
                season_episodes=season_episodes,
                custom_words=custom_words,
                filter_params=filter_params,
            ),
        )
        final_items = [context.to_dict() for context in contexts]
        yield {
            "type": "replace",
            "stage": "filtered",
            "value": 100,
            "text": f"过滤匹配完成，共 {len(contexts)} 个资源",
            "items": final_items,
            "total_items": len(contexts),
            "candidate_items": len(candidate_contexts),
        }
        yield {
            "type": "done",
            "stage": "done",
            "text": f"搜索完成，共 {len(contexts)} 个资源",
            "items": final_items,
            "total_items": len(contexts),
            "candidate_items": len(candidate_contexts),
            "contexts": contexts,
        }
