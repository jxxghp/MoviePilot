"""标题搜索入口与标题候选过滤 owner。"""

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

from app.application.configuration import (
    get_configured_system_config,
)
from app.chain.search.contract import _SearchOwnerBase as _SearchOwnerBase
from app.domain.context import Context, TorrentInfo
from app.domain.metainfo import MetaInfo
from app.runtime.execution import run_in_threadpool
from app.runtime.log import logger
from app.schemas.types import (
    MediaType,
    SystemConfigKey,
)


def _build_title_search_params(
    title: str,
    page: Optional[int],
    sites: Optional[List[int]],
    mtype: Optional[MediaType],
    include_empty_type: bool = False,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """构造标题搜索共享的 provider 与缓存参数。"""
    provider_params: Dict[str, Any] = {
        "keyword": title,
        "sites": sites,
        "page": page,
    }
    if mtype is not None or include_empty_type:
        provider_params["mtype"] = mtype
    cache_params = {
        "keyword": title,
        "mtype": mtype,
        "area": "title",
        "sites": sites,
    }
    return provider_params, cache_params


def _build_title_contexts(
    torrents: List[TorrentInfo],
    mtype: Optional[MediaType],
    build_meta: Callable[[TorrentInfo, Optional[MediaType]], Any],
) -> List[Context]:
    """将过滤后的标题候选统一投影为搜索上下文。"""
    return [
        Context(
            meta_info=build_meta(torrent, mtype),
            torrent_info=torrent,
            resource_source="search",
        )
        for torrent in torrents
    ]


@dataclass(frozen=True, slots=True)
class _TitleSearchResult:
    """封装标题搜索候选解析结果及需要记录的失败原因。"""

    contexts: List[Context]
    warning: Optional[str] = None


@dataclass(frozen=True, slots=True)
class _TitleSearchCacheRequest:
    """请求保存标题搜索参数并清理旧的 AI 推荐状态。"""

    params: Dict[str, Any]


@dataclass(frozen=True, slots=True)
class _TitleSearchProviderRequest:
    """请求执行标题搜索 provider I/O。"""

    params: Dict[str, Any]


@dataclass(frozen=True, slots=True)
class _TitleSearchResolveRequest:
    """请求过滤并投影标题搜索候选。"""

    title: str
    torrents: List[TorrentInfo]
    rule_groups: Optional[List[str]]
    mtype: Optional[MediaType]


@dataclass(frozen=True, slots=True)
class _TitleSearchSaveRequest:
    """请求持久化成功的标题搜索结果。"""

    contexts: List[Context]


_TitleSearchRequest = Union[
    _TitleSearchCacheRequest,
    _TitleSearchProviderRequest,
    _TitleSearchResolveRequest,
    _TitleSearchSaveRequest,
]
_TitleSearchResolution = Generator[
    _TitleSearchRequest, object, _TitleSearchResult
]


def _title_search_resolution(
    title: str,
    search_params: Dict[str, Any],
    cache_params: Dict[str, Any],
    cache_local: bool,
    mtype: Optional[MediaType],
    rule_groups: Optional[List[str]],
) -> _TitleSearchResolution:
    """统一标题搜索的缓存、provider、过滤、短路与结果保存顺序。"""
    if cache_local:
        yield _TitleSearchCacheRequest(params=cache_params)
    torrents = cast(
        List[TorrentInfo],
        (yield _TitleSearchProviderRequest(params=search_params)),
    )
    result = cast(
        _TitleSearchResult,
        (yield _TitleSearchResolveRequest(
            title=title,
            torrents=torrents,
            rule_groups=rule_groups,
            mtype=mtype,
        )),
    )
    if result.warning:
        return result
    if cache_local:
        yield _TitleSearchSaveRequest(contexts=result.contexts)
    return result


def _resolve_title_search_result(
    title: str,
    torrents: List[TorrentInfo],
    rule_groups: Optional[List[str]],
    mtype: Optional[MediaType],
    filter_torrents: Callable[..., List[TorrentInfo]],
    build_meta: Callable[[TorrentInfo, Optional[MediaType]], Any],
) -> _TitleSearchResult:
    """统一标题候选为空、过滤和上下文投影的结果决策。"""
    if not torrents:
        return _TitleSearchResult(
            contexts=[],
            warning=f"{title} 未搜索到资源",
        )
    filtered_torrents = filter_torrents(
        torrents=torrents,
        rule_groups=rule_groups,
    )
    if not filtered_torrents:
        return _TitleSearchResult(
            contexts=[],
            warning=f"{title} 没有符合过滤规则的资源",
        )
    return _TitleSearchResult(
        contexts=_build_title_contexts(
            filtered_torrents,
            mtype,
            build_meta,
        )
    )


class SearchTitleOwner(_SearchOwnerBase):
    """标题搜索入口与标题候选过滤 owner。"""

    def _resolve_title_request(
        self, request: _TitleSearchResolveRequest
    ) -> _TitleSearchResult:
        """通过可替换过滤与元数据投影点解析标题候选。"""
        return _resolve_title_search_result(
            title=request.title,
            torrents=request.torrents,
            rule_groups=request.rule_groups,
            mtype=request.mtype,
            filter_torrents=self._filter_title_search_torrents,
            build_meta=self._build_title_search_meta,
        )

    def _run_title_search_sync(
        self, resolution: _TitleSearchResolution
    ) -> _TitleSearchResult:
        """用同步 I/O 与过滤外壳驱动共享标题搜索状态机。"""
        response: object = None
        while True:
            try:
                request = resolution.send(response)
            except StopIteration as completed:
                return cast(_TitleSearchResult, completed.value)
            if isinstance(request, _TitleSearchCacheRequest):
                self.cancel_ai_recommend()
                self.save_last_search_params(**request.params)
                response = None
            elif isinstance(request, _TitleSearchProviderRequest):
                response = (
                    self._SearchChain__search_all_sites(**request.params) or []
                )
            elif isinstance(request, _TitleSearchResolveRequest):
                response = SearchTitleOwner._resolve_title_request(self, request)
            else:
                self._save_results(request.contexts)
                response = None

    async def _run_title_search_async(
        self, resolution: _TitleSearchResolution
    ) -> _TitleSearchResult:
        """用异步 I/O 与线程池过滤外壳驱动共享标题搜索状态机。"""
        response: object = None
        while True:
            try:
                request = resolution.send(response)
            except StopIteration as completed:
                return cast(_TitleSearchResult, completed.value)
            if isinstance(request, _TitleSearchCacheRequest):
                self.cancel_ai_recommend()
                await self.async_save_last_search_params(**request.params)
                response = None
            elif isinstance(request, _TitleSearchProviderRequest):
                response = (
                    await self._SearchChain__async_search_all_sites(
                        **request.params
                    )
                    or []
                )
            elif isinstance(request, _TitleSearchResolveRequest):
                if request.torrents:
                    response = await run_in_threadpool(
                        SearchTitleOwner._resolve_title_request, self, request
                    )
                else:
                    response = SearchTitleOwner._resolve_title_request(
                        self, request
                    )
            else:
                await self._async_save_results(request.contexts)
                response = None

    def search_by_title(
        self,
        title: str,
        page: Optional[int] = 0,
        sites: Optional[List[int]] = None,
        cache_local: Optional[bool] = False,
        mtype: Optional[MediaType] = None,
        rule_groups: Optional[List[str]] = None,
    ) -> List[Context]:
        """
        根据标题搜索资源，不识别媒体信息，按默认搜索过滤规则返回站点内容
        :param title: 标题，为空时返回所有站点首页内容
        :param page: 页码
        :param sites: 站点ID列表
        :param cache_local: 是否缓存到本地
        :param mtype: 限定站点资源分类
        :param rule_groups: 指定过滤规则组，为空时使用默认搜索过滤规则
        """
        search_params, cache_params = _build_title_search_params(
            title=title,
            page=page,
            sites=sites,
            mtype=mtype,
        )
        if title:
            logger.info(f"开始搜索资源，关键词：{title} ...")
        else:
            logger.info(f"开始浏览资源，站点：{sites} ...")
        result = SearchTitleOwner._run_title_search_sync(
            self,
            _title_search_resolution(
                title=title,
                search_params=search_params,
                cache_params=cache_params,
                cache_local=bool(cache_local),
                mtype=mtype,
                rule_groups=rule_groups,
            )
        )
        if result.warning:
            logger.warning(result.warning)
        return result.contexts

    async def async_search_by_title(
        self,
        title: str,
        page: Optional[int] = 0,
        sites: Optional[List[int]] = None,
        cache_local: Optional[bool] = False,
        mtype: Optional[MediaType] = None,
        rule_groups: Optional[List[str]] = None,
    ) -> List[Context]:
        """
        根据标题异步搜索资源，不识别媒体信息，按默认搜索过滤规则返回站点内容
        :param title: 标题，为空时返回所有站点首页内容
        :param page: 页码
        :param sites: 站点ID列表
        :param cache_local: 是否缓存到本地
        :param mtype: 限定站点资源分类
        :param rule_groups: 指定过滤规则组，为空时使用默认搜索过滤规则
        """
        search_params, cache_params = _build_title_search_params(
            title=title,
            page=page,
            sites=sites,
            mtype=mtype,
        )
        if title:
            logger.info(f"开始搜索资源，关键词：{title} ...")
        else:
            logger.info(f"开始浏览资源，站点：{sites} ...")
        result = await SearchTitleOwner._run_title_search_async(
            self,
            _title_search_resolution(
                title=title,
                search_params=search_params,
                cache_params=cache_params,
                cache_local=bool(cache_local),
                mtype=mtype,
                rule_groups=rule_groups,
            )
        )
        if result.warning:
            logger.warning(result.warning)
        return result.contexts

    async def async_search_by_title_stream(
        self,
        title: str,
        page: Optional[int] = 0,
        sites: Optional[List[int]] = None,
        cache_local: Optional[bool] = False,
        mtype: Optional[MediaType] = None,
        rule_groups: Optional[List[str]] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """
        根据标题渐进式搜索资源，不识别媒体信息，按默认搜索过滤规则返回结果
        """
        search_params, cache_params = _build_title_search_params(
            title=title,
            page=page,
            sites=sites,
            mtype=mtype,
            include_empty_type=True,
        )
        if cache_local:
            self.cancel_ai_recommend()
            await self.async_save_last_search_params(**cache_params)
        if title:
            logger.info(f"开始渐进式搜索资源，关键词：{title} ...")
        else:
            logger.info(f"开始渐进式浏览资源，站点：{sites} ...")

        contexts: List[Context] = []
        # 记录过滤前的候选资源数，供前端在全部被过滤时给出友好提示
        candidate_count = 0
        if rule_groups is None:
            rule_groups = get_configured_system_config().get(SystemConfigKey.SearchFilterRuleGroups) or []
        async for event in self._SearchChain__async_search_all_sites_stream(
            **search_params
        ):
            result = event.pop("items", []) or []
            candidate_count += len(result)
            result = await run_in_threadpool(
                self._filter_title_search_torrents,
                torrents=result,
                rule_groups=rule_groups,
            )
            batch_contexts = _build_title_contexts(
                result, mtype, self._build_title_search_meta
            )
            if batch_contexts:
                contexts.extend(batch_contexts)
            yield {
                **event,
                "type": "append",
                "items": [cast(Any, context).to_dict() for context in batch_contexts],
                "total_items": len(contexts),
            }

        if cache_local:
            await self._async_save_results(contexts)

        if not contexts:
            logger.warning(f"{title} 未搜索到资源")
        yield {
            "type": "done",
            "text": f"搜索完成，共 {len(contexts)} 个资源",
            "items": [cast(Any, context).to_dict() for context in contexts],
            "total_items": len(contexts),
            "candidate_items": candidate_count,
        }

    @staticmethod
    def _build_title_search_meta(
        torrent: TorrentInfo,
        mtype: Optional[MediaType],
    ) -> Any:
        """根据限定媒体类型构造模糊搜索结果的上下文元数据。"""
        return MetaInfo(title=torrent.title, subtitle=torrent.description, mtype=mtype)

    def _filter_title_search_torrents(
        self, torrents: List[TorrentInfo], rule_groups: Optional[List[str]] = None
    ) -> List[TorrentInfo]:
        """
        对标题搜索结果应用默认搜索过滤规则，不执行媒体识别和标题精确匹配。
        """
        if not torrents:
            return []

        if rule_groups is None:
            rule_groups = get_configured_system_config().get(SystemConfigKey.SearchFilterRuleGroups) or []
        if not rule_groups:
            return torrents

        logger.info(f"开始过滤标题搜索结果，使用规则组：{rule_groups} ...")
        filter_torrents = cast(
            Callable[..., List[TorrentInfo]],
            self.filter_torrents,
        )
        filtered_torrents = (
            filter_torrents(
                rule_groups=rule_groups,
                torrent_list=torrents,
                mediainfo=None,
            )
            or []
        )
        logger.info(f"标题搜索过滤完成，剩余 {len(filtered_torrents)} 个资源")
        return filtered_torrents
