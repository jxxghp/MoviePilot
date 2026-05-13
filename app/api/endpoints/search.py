import asyncio
import json
from typing import List, Any, Optional, AsyncIterator

from fastapi import APIRouter, Depends, Body, Request
from fastapi.responses import StreamingResponse

from app import schemas
from app.chain.media import MediaChain
from app.chain.search import SearchChain
from app.core.config import settings
from app.core.event import eventmanager
from app.core.metainfo import MetaInfo
from app.core.security import verify_resource_token, verify_token
from app.log import logger
from app.schemas import MediaRecognizeConvertEventData
from app.schemas.types import MediaType, ChainEventType

router = APIRouter()

_SSE_APPEND_FLUSH_INTERVAL = 1
_SSE_APPEND_MAX_ITEMS = 48


def _parse_site_list(sites: Optional[str]) -> Optional[List[int]]:
    """
    解析站点ID列表
    """
    return [int(site) for site in sites.split(",") if site] if sites else None


def _sse_event(data: dict) -> str:
    """
    转换为SSE事件
    """
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _merge_append_event(pending_event: Optional[dict], event: dict) -> dict:
    """
    合并短时间内连续到达的 append 事件，降低前端刷新频率。
    """
    items = list(event.get("items") or [])
    if not pending_event:
        merged_event = dict(event)
        merged_event["items"] = items
        return merged_event

    merged_event = dict(pending_event)
    merged_event.update({key: value for key, value in event.items() if key != "items"})
    merged_event["type"] = "append"
    merged_event["items"] = [*(pending_event.get("items") or []), *items]
    return merged_event


async def _iter_batched_search_events(
    event_source: AsyncIterator[dict],
) -> AsyncIterator[dict]:
    """
    对搜索流事件做轻量批处理，避免站点结果集中返回时产生过密 SSE。
    """
    iterator = event_source.__aiter__()
    pending_append_event: Optional[dict] = None
    next_event_task: Optional[asyncio.Task] = None

    try:
        while True:
            if next_event_task is None:
                next_event_task = asyncio.create_task(anext(iterator))

            timeout = _SSE_APPEND_FLUSH_INTERVAL if pending_append_event else None
            done, _ = await asyncio.wait({next_event_task}, timeout=timeout)

            if not done:
                if pending_append_event:
                    yield pending_append_event
                    pending_append_event = None
                continue

            try:
                event = next_event_task.result()
            except StopAsyncIteration:
                next_event_task = None
                break
            finally:
                if next_event_task and next_event_task.done():
                    next_event_task = None

            if event.get("type") == "append":
                pending_append_event = _merge_append_event(pending_append_event, event)
                if (
                    len(pending_append_event.get("items") or [])
                    >= _SSE_APPEND_MAX_ITEMS
                ):
                    yield pending_append_event
                    pending_append_event = None
                continue

            if pending_append_event:
                yield pending_append_event
                pending_append_event = None

            yield event
    finally:
        if next_event_task and not next_event_task.done():
            next_event_task.cancel()
            await asyncio.gather(next_event_task, return_exceptions=True)

    if pending_append_event:
        yield pending_append_event


async def _stream_search_events(request: Request, event_source: AsyncIterator[dict]):
    """
    输出搜索SSE事件
    """
    try:
        has_sent_final_replace = False
        async for event in _iter_batched_search_events(event_source):
            if await request.is_disconnected():
                break
            # 精确搜索会先发送 replace，再发送 done。done 再带整包 items 只会重复占用带宽和前端内存。
            if event.get("type") == "replace" and event.get("items"):
                has_sent_final_replace = True
            elif (
                event.get("type") == "done"
                and has_sent_final_replace
                and event.get("stage") == "done"
                and event.get("items")
            ):
                event = {key: value for key, value in event.items() if key != "items"}
            yield _sse_event(event)
    except Exception as err:
        logger.error(f"渐进式搜索出错：{err}", exc_info=True)
        yield _sse_event({"type": "error", "success": False, "message": str(err)})


@router.get("/last", summary="查询搜索结果", response_model=List[schemas.Context])
async def search_latest(_: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询搜索结果
    """
    torrents = await SearchChain().async_last_search_results() or []
    return [torrent.to_dict() for torrent in torrents]


@router.get("/media/{mediaid}/stream", summary="渐进式精确搜索资源")
async def search_by_id_stream(
    request: Request,
    mediaid: str,
    mtype: Optional[str] = None,
    area: Optional[str] = "title",
    title: Optional[str] = None,
    year: Optional[str] = None,
    season: Optional[str] = None,
    sites: Optional[str] = None,
    _: schemas.TokenPayload = Depends(verify_resource_token),
) -> Any:
    """
    根据TMDBID/豆瓣ID渐进式搜索站点资源，返回格式为SSE
    """

    media_type = MediaType(mtype) if mtype else None
    media_season = int(season) if season else None
    site_list = _parse_site_list(sites)
    media_chain = MediaChain()
    search_chain = SearchChain()

    async def event_source():
        nonlocal media_season
        torrents = None
        if mediaid.startswith("tmdb:"):
            tmdbid = int(mediaid.replace("tmdb:", ""))
            if settings.RECOGNIZE_SOURCE == "douban":
                doubaninfo = await media_chain.async_get_doubaninfo_by_tmdbid(
                    tmdbid=tmdbid, mtype=media_type
                )
                if doubaninfo:
                    torrents = search_chain.async_search_by_id_stream(
                        doubanid=doubaninfo.get("id"),
                        mtype=media_type,
                        area=area,
                        season=media_season,
                        sites=site_list,
                        cache_local=True,
                    )
                else:
                    yield {
                        "type": "error",
                        "success": False,
                        "message": "未识别到豆瓣媒体信息",
                    }
                    return
            else:
                torrents = search_chain.async_search_by_id_stream(
                    tmdbid=tmdbid,
                    mtype=media_type,
                    area=area,
                    season=media_season,
                    sites=site_list,
                    cache_local=True,
                )
        elif mediaid.startswith("douban:"):
            doubanid = mediaid.replace("douban:", "")
            if settings.RECOGNIZE_SOURCE == "themoviedb":
                tmdbinfo = await media_chain.async_get_tmdbinfo_by_doubanid(
                    doubanid=doubanid, mtype=media_type
                )
                if tmdbinfo:
                    if tmdbinfo.get("season") and not media_season:
                        media_season = tmdbinfo.get("season")
                    torrents = search_chain.async_search_by_id_stream(
                        tmdbid=tmdbinfo.get("id"),
                        mtype=media_type,
                        area=area,
                        season=media_season,
                        sites=site_list,
                        cache_local=True,
                    )
                else:
                    yield {
                        "type": "error",
                        "success": False,
                        "message": "未识别到TMDB媒体信息",
                    }
                    return
            else:
                torrents = search_chain.async_search_by_id_stream(
                    doubanid=doubanid,
                    mtype=media_type,
                    area=area,
                    season=media_season,
                    sites=site_list,
                    cache_local=True,
                )
        elif mediaid.startswith("bangumi:"):
            bangumiid = int(mediaid.replace("bangumi:", ""))
            if settings.RECOGNIZE_SOURCE == "themoviedb":
                tmdbinfo = await media_chain.async_get_tmdbinfo_by_bangumiid(
                    bangumiid=bangumiid
                )
                if tmdbinfo:
                    torrents = search_chain.async_search_by_id_stream(
                        tmdbid=tmdbinfo.get("id"),
                        mtype=media_type,
                        area=area,
                        season=media_season,
                        sites=site_list,
                        cache_local=True,
                    )
                else:
                    yield {
                        "type": "error",
                        "success": False,
                        "message": "未识别到TMDB媒体信息",
                    }
                    return
            else:
                doubaninfo = await media_chain.async_get_doubaninfo_by_bangumiid(
                    bangumiid=bangumiid
                )
                if doubaninfo:
                    torrents = search_chain.async_search_by_id_stream(
                        doubanid=doubaninfo.get("id"),
                        mtype=media_type,
                        area=area,
                        season=media_season,
                        sites=site_list,
                        cache_local=True,
                    )
                else:
                    yield {
                        "type": "error",
                        "success": False,
                        "message": "未识别到豆瓣媒体信息",
                    }
                    return
        else:
            event_data = MediaRecognizeConvertEventData(
                mediaid=mediaid, convert_type=settings.RECOGNIZE_SOURCE
            )
            event = await eventmanager.async_send_event(
                ChainEventType.MediaRecognizeConvert, event_data
            )
            if event and event.event_data:
                event_data = event.event_data
                if event_data.media_dict:
                    search_id = event_data.media_dict.get("id")
                    if event_data.convert_type == "themoviedb":
                        torrents = search_chain.async_search_by_id_stream(
                            tmdbid=search_id,
                            mtype=media_type,
                            area=area,
                            season=media_season,
                            sites=site_list,
                            cache_local=True,
                        )
                    elif event_data.convert_type == "douban":
                        torrents = search_chain.async_search_by_id_stream(
                            doubanid=search_id,
                            mtype=media_type,
                            area=area,
                            season=media_season,
                            sites=site_list,
                            cache_local=True,
                        )
            else:
                if not title:
                    yield {"type": "error", "success": False, "message": "未知的媒体ID"}
                    return
                meta = MetaInfo(title)
                if year:
                    meta.year = year
                if media_type:
                    meta.type = media_type
                if media_season:
                    meta.type = MediaType.TV
                    meta.begin_season = media_season
                mediainfo = await media_chain.async_recognize_by_meta(
                    meta,
                    obtain_images=False,
                )
                if mediainfo:
                    if settings.RECOGNIZE_SOURCE == "themoviedb":
                        torrents = search_chain.async_search_by_id_stream(
                            tmdbid=mediainfo.tmdb_id,
                            mtype=media_type,
                            area=area,
                            season=media_season,
                            sites=site_list,
                            cache_local=True,
                        )
                    else:
                        torrents = search_chain.async_search_by_id_stream(
                            doubanid=mediainfo.douban_id,
                            mtype=media_type,
                            area=area,
                            season=media_season,
                            sites=site_list,
                            cache_local=True,
                        )

        if not torrents:
            yield {"type": "error", "success": False, "message": "未搜索到任何资源"}
            return

        async for event in torrents:
            yield event

    return StreamingResponse(
        _stream_search_events(request, event_source()), media_type="text/event-stream"
    )


@router.get("/media/{mediaid}", summary="精确搜索资源", response_model=schemas.Response)
async def search_by_id(
    mediaid: str,
    mtype: Optional[str] = None,
    area: Optional[str] = "title",
    title: Optional[str] = None,
    year: Optional[str] = None,
    season: Optional[str] = None,
    sites: Optional[str] = None,
    _: schemas.TokenPayload = Depends(verify_token),
) -> Any:
    """
    根据TMDBID/豆瓣ID精确搜索站点资源 tmdb:/douban:/bangumi:
    """
    if mtype:
        media_type = MediaType(mtype)
    else:
        media_type = None
    if season:
        media_season = int(season)
    else:
        media_season = None
    if sites:
        site_list = [int(site) for site in sites.split(",") if site]
    else:
        site_list = None
    torrents = None
    media_chain = MediaChain()
    search_chain = SearchChain()
    # 根据前缀识别媒体ID
    if mediaid.startswith("tmdb:"):
        tmdbid = int(mediaid.replace("tmdb:", ""))
        if settings.RECOGNIZE_SOURCE == "douban":
            # 通过TMDBID识别豆瓣ID
            doubaninfo = await media_chain.async_get_doubaninfo_by_tmdbid(
                tmdbid=tmdbid, mtype=media_type
            )
            if doubaninfo:
                torrents = await search_chain.async_search_by_id(
                    doubanid=doubaninfo.get("id"),
                    mtype=media_type,
                    area=area,
                    season=media_season,
                    sites=site_list,
                    cache_local=True,
                )
            else:
                return schemas.Response(success=False, message="未识别到豆瓣媒体信息")
        else:
            torrents = await search_chain.async_search_by_id(
                tmdbid=tmdbid,
                mtype=media_type,
                area=area,
                season=media_season,
                sites=site_list,
                cache_local=True,
            )
    elif mediaid.startswith("douban:"):
        doubanid = mediaid.replace("douban:", "")
        if settings.RECOGNIZE_SOURCE == "themoviedb":
            # 通过豆瓣ID识别TMDBID
            tmdbinfo = await media_chain.async_get_tmdbinfo_by_doubanid(
                doubanid=doubanid, mtype=media_type
            )
            if tmdbinfo:
                if tmdbinfo.get("season") and not media_season:
                    media_season = tmdbinfo.get("season")
                torrents = await search_chain.async_search_by_id(
                    tmdbid=tmdbinfo.get("id"),
                    mtype=media_type,
                    area=area,
                    season=media_season,
                    sites=site_list,
                    cache_local=True,
                )
            else:
                return schemas.Response(success=False, message="未识别到TMDB媒体信息")
        else:
            torrents = await search_chain.async_search_by_id(
                doubanid=doubanid,
                mtype=media_type,
                area=area,
                season=media_season,
                sites=site_list,
                cache_local=True,
            )
    elif mediaid.startswith("bangumi:"):
        bangumiid = int(mediaid.replace("bangumi:", ""))
        if settings.RECOGNIZE_SOURCE == "themoviedb":
            # 通过BangumiID识别TMDBID
            tmdbinfo = await media_chain.async_get_tmdbinfo_by_bangumiid(
                bangumiid=bangumiid
            )
            if tmdbinfo:
                torrents = await search_chain.async_search_by_id(
                    tmdbid=tmdbinfo.get("id"),
                    mtype=media_type,
                    area=area,
                    season=media_season,
                    sites=site_list,
                    cache_local=True,
                )
            else:
                return schemas.Response(success=False, message="未识别到TMDB媒体信息")
        else:
            # 通过BangumiID识别豆瓣ID
            doubaninfo = await media_chain.async_get_doubaninfo_by_bangumiid(
                bangumiid=bangumiid
            )
            if doubaninfo:
                torrents = await search_chain.async_search_by_id(
                    doubanid=doubaninfo.get("id"),
                    mtype=media_type,
                    area=area,
                    season=media_season,
                    sites=site_list,
                    cache_local=True,
                )
            else:
                return schemas.Response(success=False, message="未识别到豆瓣媒体信息")
    else:
        # 未知前缀，广播事件解析媒体信息
        event_data = MediaRecognizeConvertEventData(
            mediaid=mediaid, convert_type=settings.RECOGNIZE_SOURCE
        )
        event = await eventmanager.async_send_event(
            ChainEventType.MediaRecognizeConvert, event_data
        )
        # 使用事件返回的上下文数据
        if event and event.event_data:
            event_data: MediaRecognizeConvertEventData = event.event_data
            if event_data.media_dict:
                search_id = event_data.media_dict.get("id")
                if event_data.convert_type == "themoviedb":
                    torrents = await search_chain.async_search_by_id(
                        tmdbid=search_id,
                        mtype=media_type,
                        area=area,
                        season=media_season,
                        cache_local=True,
                    )
                elif event_data.convert_type == "douban":
                    torrents = await search_chain.async_search_by_id(
                        doubanid=search_id,
                        mtype=media_type,
                        area=area,
                        season=media_season,
                        cache_local=True,
                    )
        else:
            if not title:
                return schemas.Response(success=False, message="未知的媒体ID")
            # 使用名称识别兜底
            meta = MetaInfo(title)
            if year:
                meta.year = year
            if media_type:
                meta.type = media_type
            if media_season:
                meta.type = MediaType.TV
                meta.begin_season = media_season
            mediainfo = await media_chain.async_recognize_by_meta(
                meta,
                obtain_images=False,
            )
            if mediainfo:
                if settings.RECOGNIZE_SOURCE == "themoviedb":
                    torrents = await search_chain.async_search_by_id(
                        tmdbid=mediainfo.tmdb_id,
                        mtype=media_type,
                        area=area,
                        season=media_season,
                        cache_local=True,
                    )
                else:
                    torrents = await search_chain.async_search_by_id(
                        doubanid=mediainfo.douban_id,
                        mtype=media_type,
                        area=area,
                        season=media_season,
                        cache_local=True,
                    )
    # 返回搜索结果
    if not torrents:
        return schemas.Response(success=False, message="未搜索到任何资源")
    else:
        return schemas.Response(
            success=True, data=[torrent.to_dict() for torrent in torrents]
        )


@router.get("/title/stream", summary="渐进式模糊搜索资源")
async def search_by_title_stream(
    request: Request,
    keyword: Optional[str] = None,
    page: Optional[int] = 0,
    sites: Optional[str] = None,
    _: schemas.TokenPayload = Depends(verify_resource_token),
) -> Any:
    """
    根据名称渐进式模糊搜索站点资源，返回格式为SSE
    """

    event_source = SearchChain().async_search_by_title_stream(
        title=keyword, page=page, sites=_parse_site_list(sites), cache_local=True
    )
    return StreamingResponse(
        _stream_search_events(request, event_source), media_type="text/event-stream"
    )


@router.get("/title", summary="模糊搜索资源", response_model=schemas.Response)
async def search_by_title(
    keyword: Optional[str] = None,
    page: Optional[int] = 0,
    sites: Optional[str] = None,
    _: schemas.TokenPayload = Depends(verify_token),
) -> Any:
    """
    根据名称模糊搜索站点资源，支持分页，关键词为空是返回首页资源
    """
    torrents = await SearchChain().async_search_by_title(
        title=keyword, page=page, sites=_parse_site_list(sites), cache_local=True
    )
    if not torrents:
        return schemas.Response(success=False, message="未搜索到任何资源")
    return schemas.Response(
        success=True, data=[torrent.to_dict() for torrent in torrents]
    )


@router.post("/recommend", summary="AI推荐资源", response_model=schemas.Response)
async def recommend_search_results(
    filtered_indices: Optional[List[int]] = Body(
        None, embed=True, description="筛选后的索引列表"
    ),
    check_only: bool = Body(False, embed=True, description="仅检查状态，不启动新任务"),
    force: bool = Body(False, embed=True, description="强制重新推荐，清除旧结果"),
    _: schemas.TokenPayload = Depends(verify_token),
) -> Any:
    """
    AI推荐资源 - 轮询接口
    前端轮询此接口，发送筛选后的索引（如果有筛选）
    后端根据请求变化自动取消旧任务并启动新任务

    参数：
    - filtered_indices: 筛选后的索引列表（可选，为空或不提供时使用所有结果）
    - check_only: 仅检查状态（首次打开页面时使用，避免触发不必要的重新推理）
    - force: 强制重新推荐（清除旧结果并重新启动）

    返回数据结构：
    {
        "success": bool,
        "message": string,   // 错误信息（仅在错误时存在）
        "data": {
            "status": string,    // 状态: disabled | idle | running | completed | error
            "results": array     // 推荐结果（仅status=completed时存在）
        }
    }
    """
    # 从缓存获取上次搜索结果
    results = await SearchChain().async_last_search_results() or []
    if not results:
        return schemas.Response(
            success=False, message="没有可用的搜索结果", data={"status": "error"}
        )

    recommend_chain = SearchChain()

    # 如果是强制模式，先取消并清除旧结果，然后直接启动新任务
    if force:
        # 检查功能是否启用
        if not recommend_chain.is_ai_recommend_enabled:
            return schemas.Response(success=True, data={"status": "disabled"})
        logger.info("收到新推荐请求，清除旧结果并启动新任务")
        recommend_chain.cancel_ai_recommend()
        recommend_chain.start_recommend_task(filtered_indices, len(results), results)
        # 直接返回运行中状态
        return schemas.Response(success=True, data={"status": "running"})

    # 如果是仅检查模式，不传递 filtered_indices（避免触发请求变化检测）
    if check_only:
        # 返回当前运行状态，不做任何任务启动或取消操作
        current_status = recommend_chain.get_current_recommend_status_only()
        # 如果有错误，将错误信息放到message中
        if current_status.get("status") == "error":
            error_msg = current_status.pop("error", "未知错误")
            return schemas.Response(
                success=False, message=error_msg, data=current_status
            )
        return schemas.Response(success=True, data=current_status)

    # 获取当前状态（会检测请求是否变化）
    status_data = recommend_chain.get_recommend_status(filtered_indices, len(results))

    # 如果功能未启用，直接返回禁用状态
    if status_data.get("status") == "disabled":
        return schemas.Response(success=True, data=status_data)

    # 如果是空闲状态，启动新任务
    if status_data["status"] == "idle":
        recommend_chain.start_recommend_task(filtered_indices, len(results), results)
        # 立即返回运行中状态
        return schemas.Response(success=True, data={"status": "running"})

    # 如果有错误，将错误信息放到message中
    if status_data.get("status") == "error":
        error_msg = status_data.pop("error", "未知错误")
        return schemas.Response(success=False, message=error_msg, data=status_data)

    # 返回当前状态
    return schemas.Response(success=True, data=status_data)
