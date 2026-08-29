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
        if cache_local:
            self.cancel_ai_recommend()
            self.save_last_search_params(**cache_params)
        # 音乐统一在 MediaChain.recognize_media 内按固定来源路由
        mediainfo = MediaChain().recognize_media(**recognition_params)
        if not mediainfo:
            logger.error(f"{self._build_search_keyword(media_source, media_id)} 媒体信息识别失败！")
            return []
        no_exists = _build_missing_media_map(mediainfo, season)
        results = self.process(mediainfo=mediainfo, sites=sites, area=area, no_exists=no_exists)
        # 保存到本地文件
        if cache_local:
            self._save_results(results)
        return results

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
        if cache_local:
            self.cancel_ai_recommend()
            await self.async_save_last_search_params(**cache_params)
        # 音乐统一在 MediaChain.async_recognize_media 内按固定来源路由
        mediainfo = await MediaChain().async_recognize_media(**recognition_params)
        if not mediainfo:
            logger.error(f"{self._build_search_keyword(media_source, media_id)} 媒体信息识别失败！")
            return []
        no_exists = _build_missing_media_map(mediainfo, season)
        results = await self.async_process(mediainfo=mediainfo, sites=sites, area=area, no_exists=no_exists)
        # 保存到本地文件
        if cache_local:
            await self._async_save_results(results)
        return results

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

        if mediainfo.type == MediaType.MUSIC:
            return cast(
                List[Context],
                self._process_music(
                    mediainfo=cast(MusicInfo, mediainfo),
                    keyword=keyword,
                    sites=sites,
                    rule_groups=rule_groups,
                    filter_params=filter_params,
                ),
            )

        mediainfo = _normalize_media_search_input(self._copy_media_input(mediainfo))
        logger.info(f"开始搜索资源，关键词：{keyword or mediainfo.title} ...")

        # 补充媒体信息
        if not mediainfo.names:
            recognized_media = MediaChain().recognize_media(
                mtype=mediainfo.type,
                **self._media_recognize_kwargs(mediainfo),
            )
            if not recognized_media:
                logger.error("媒体信息识别失败！")
                return []
            mediainfo = recognized_media

        # 搜索前按用户启用的数据源聚合别名；分类、风格与外部 ID 仅由 TMDB 补充。
        mediainfo = cast(
            MediaInfo,
            MediaChain().supplement_media_info(mediainfo) or mediainfo,
        )

        # 准备搜索参数
        season_episodes, keywords = self._prepare_params(mediainfo=mediainfo, keyword=keyword, no_exists=no_exists)

        def execute_search(request: _KeywordSearchRequest) -> List[TorrentInfo]:
            """执行共享状态机请求的同步站点搜索。"""
            if request.search_count > 0:
                logger.info(
                    f"已搜索 {request.search_count} 次，强制休眠 1-10 秒 ..."
                )
                time.sleep(random.randint(1, 10))
            return (
                self._SearchChain__search_all_sites(
                    mediainfo=mediainfo,
                    keyword=request.keyword,
                    sites=sites,
                    area=area,
                )
                or []
            )

        outcome = _run_keyword_search_sync(
            _keyword_search_resolution(
                keywords, self.runtime_config.search_multiple_name
            ),
            execute_search,
        )
        if outcome.stopped_early:
            logger.info(f"共搜索到 {len(outcome.torrents)} 个资源，停止搜索")

        # 处理结果
        return cast(
            List[Context],
            self._parse_result(
                **_build_result_params(
                    torrents=outcome.torrents,
                    mediainfo=mediainfo,
                    keyword=keyword,
                    rule_groups=rule_groups,
                    season_episodes=season_episodes,
                    custom_words=custom_words,
                    filter_params=filter_params,
                )
            ),
        )

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

        if mediainfo.type == MediaType.MUSIC:
            return cast(
                List[Context],
                await self._async_process_music(
                    mediainfo=cast(MusicInfo, mediainfo),
                    keyword=keyword,
                    sites=sites,
                    rule_groups=rule_groups,
                    filter_params=filter_params,
                ),
            )

        mediainfo = _normalize_media_search_input(self._copy_media_input(mediainfo))
        logger.info(f"开始搜索资源，关键词：{keyword or mediainfo.title} ...")

        # 补充媒体信息
        if not mediainfo.names:
            recognized_media = await MediaChain().async_recognize_media(
                mtype=mediainfo.type,
                **self._media_recognize_kwargs(mediainfo),
            )
            if not recognized_media:
                logger.error("媒体信息识别失败！")
                return []
            mediainfo = recognized_media

        # 异步搜索与同步入口共享同一份多来源附加信息语义。
        mediainfo = cast(
            MediaInfo,
            await MediaChain().async_supplement_media_info(mediainfo) or mediainfo,
        )

        # 准备搜索参数
        season_episodes, keywords = self._prepare_params(mediainfo=mediainfo, keyword=keyword, no_exists=no_exists)

        async def execute_search(
            request: _KeywordSearchRequest,
        ) -> List[TorrentInfo]:
            """执行共享状态机请求的异步站点搜索。"""
            if request.search_count > 0:
                logger.info(
                    f"已搜索 {request.search_count} 次，强制休眠 1-10 秒 ..."
                )
                await asyncio.sleep(random.randint(1, 10))
            return (
                await self._SearchChain__async_search_all_sites(
                    mediainfo=mediainfo,
                    keyword=request.keyword,
                    sites=sites,
                    area=area,
                )
                or []
            )

        outcome = await _run_keyword_search_async(
            _keyword_search_resolution(
                keywords, self.runtime_config.search_multiple_name
            ),
            execute_search,
        )
        if outcome.stopped_early:
            logger.info(f"共搜索到 {len(outcome.torrents)} 个资源，停止搜索")

        # 处理结果
        return cast(
            List[Context],
            await run_in_threadpool(
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
            ),
        )

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
