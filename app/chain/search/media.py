"""精确媒体搜索与同步异步编排 owner。"""

from dataclasses import dataclass
from typing import (
    Any,
    AsyncIterator,
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
from app.chain.search.contract import _SearchOwnerBase
from app.chain.search.execution import MediaSearchPlan, SearchExecutionOwner
from app.domain.context import Context, MediaInfo, MusicInfo
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
    include_candidates: bool = False,
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
    if include_candidates:
        cache_params["include_candidates"] = True
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
                **({"include_candidates": True} if isinstance(mediainfo, MusicInfo)
                   and cache_params.get("include_candidates") else {}),
            }
        )),
    )
    if cache_local:
        yield _IdSearchSaveRequest(contexts=contexts)
    return _IdSearchResult(contexts=contexts)


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
        include_candidates: bool = False,
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
            include_candidates=include_candidates,
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
        include_candidates: bool = False,
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
            include_candidates=include_candidates,
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
        include_candidates: bool = False,
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
            include_candidates=include_candidates,
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
        candidate_params: Dict[str, Any] = {"include_candidates": True} if include_candidates else {}
        async for event in self.async_process_stream(
            mediainfo=mediainfo, sites=sites, area=area, no_exists=no_exists,
            **candidate_params,
        ):
            if event.get("type") == "done":
                contexts = event.get("contexts") or []
                event = {key: value for key, value in event.items() if key != "contexts"}
            yield event

        if cache_local:
            await self._async_save_results(contexts)

    def process(
        self, mediainfo: MediaInfo | MusicInfo, keyword: Optional[str] = None,
        no_exists: Optional[Dict[str, Dict[int, NotExistMediaInfo]]] = None,
        sites: Optional[List[int]] = None, rule_groups: Optional[List[str]] = None,
        area: Optional[str] = "title", custom_words: Optional[List[str]] = None,
        filter_params: Optional[Dict[str, str]] = None, include_candidates: bool = False,
        candidate_filter: Optional[Callable[[List[Context]], List[Context]]] = None,
    ) -> List[Context]:
        """通过所有媒体共用的状态机同步搜索，人工候选必须显式请求。"""
        return SearchExecutionOwner.run(
            self, MediaSearchPlan(
                mediainfo=mediainfo, keyword=keyword, no_exists=no_exists, sites=sites,
                rule_groups=rule_groups, area=area, custom_words=custom_words,
                filter_params=filter_params, include_candidates=include_candidates,
                candidate_filter=candidate_filter,
            ), MediaChain(),
        )

    async def async_process(
        self, mediainfo: MediaInfo | MusicInfo, keyword: Optional[str] = None,
        no_exists: Optional[Dict[str, Dict[int, NotExistMediaInfo]]] = None,
        sites: Optional[List[int]] = None, rule_groups: Optional[List[str]] = None,
        area: Optional[str] = "title", custom_words: Optional[List[str]] = None,
        filter_params: Optional[Dict[str, str]] = None, include_candidates: bool = False,
        candidate_filter: Optional[Callable[[List[Context]], List[Context]]] = None,
    ) -> List[Context]:
        """异步执行同一搜索状态机，站点查询使用非流式异步端口。"""
        plan = MediaSearchPlan(
            mediainfo=mediainfo, keyword=keyword, no_exists=no_exists, sites=sites,
            rule_groups=rule_groups, area=area, custom_words=custom_words,
            filter_params=filter_params, include_candidates=include_candidates,
            candidate_filter=candidate_filter,
        )
        async for event in SearchExecutionOwner.events(self, plan, MediaChain(), streaming=False):
            if event["type"] == "done":
                return cast(List[Context], event["contexts"])
        return []

    async def async_process_stream(
        self, mediainfo: MediaInfo | MusicInfo, keyword: Optional[str] = None,
        no_exists: Optional[Dict[str, Dict[int, NotExistMediaInfo]]] = None,
        sites: Optional[List[int]] = None, rule_groups: Optional[List[str]] = None,
        area: Optional[str] = "title", custom_words: Optional[List[str]] = None,
        filter_params: Optional[Dict[str, str]] = None, include_candidates: bool = False,
        candidate_filter: Optional[Callable[[List[Context]], List[Context]]] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """逐站点发布统一搜索进度，并以同一结果处理器完成过滤与匹配。"""
        plan = MediaSearchPlan(
            mediainfo=mediainfo, keyword=keyword, no_exists=no_exists, sites=sites,
            rule_groups=rule_groups, area=area, custom_words=custom_words,
            filter_params=filter_params, include_candidates=include_candidates,
            candidate_filter=candidate_filter,
        )
        async for event in SearchExecutionOwner.events(self, plan, MediaChain()):
            yield event
