import asyncio
import json
import time
from typing import Any, AsyncIterator, Iterator, List, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, Body, Request
from fastapi.responses import StreamingResponse

from app import schemas
from app.chain.media import MediaChain
from app.chain.search import SearchChain
from app.core.config import settings
from app.core.event import eventmanager
from app.core.metainfo import MetaInfo
from app.core.security import verify_resource_token, verify_token
from app.helper.locale import LocaleHelper
from app.log import logger
from app.schemas import MediaRecognizeConvertEventData
from app.schemas.types import MediaType, ChainEventType
from app.utils.media import parse_media_key, resolve_media_identity
from app.utils.security import SecurityUtils

router = APIRouter()

_SSE_APPEND_FLUSH_INTERVAL = 1
_SSE_APPEND_MAX_ITEMS = 48
_SSE_HEARTBEAT_INTERVAL = 15
_SSE_REPLACE_MAX_ITEMS = 48
_SSE_RESPONSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
}


def _parse_site_list(sites: Optional[str]) -> Optional[List[int]]:
    """
    解析站点ID列表
    """
    return [int(site) for site in sites.split(",") if site] if sites else None


def _parse_media_type(mtype: Optional[str]) -> Optional[MediaType]:
    """
    解析媒体类型，兼容前端和 Agent 使用的 movie/tv 取值。
    """
    if not mtype:
        return None
    return MediaType.from_agent(mtype) or MediaType(mtype)


def _resolve_media_season(
        explicit_season: Optional[int],
        recognized_season: Optional[int],
) -> Optional[int]:
    """合并显式季号与识别结果，显式值优先且季 0 属于有效业务值。"""
    return explicit_season if explicit_season is not None else recognized_season


async def _resolve_media_search_params(
        mediaid: str,
        media_type: Optional[MediaType] = None,
        title: Optional[str] = None,
        year: Optional[str] = None,
        media_season: Optional[int] = None,
) -> tuple[Optional[dict], str]:
    """将任意来源媒体键解析为 SearchChain 可直接使用的识别参数。"""
    source, source_media_id = parse_media_key(mediaid)
    if source and source_media_id:
        if source in {"themoviedb", "bangumi", "anilist"} \
                and not source_media_id.isdigit():
            return None, "媒体ID格式错误"
        return {"source": source, "mediaid": source_media_id}, ""

    event_data = MediaRecognizeConvertEventData(
        mediaid=mediaid, convert_type=settings.RECOGNIZE_SOURCE
    )
    event = await eventmanager.async_send_event(
        ChainEventType.MediaRecognizeConvert, event_data
    )
    if event and event.event_data and event.event_data.media_dict:
        event_data = event.event_data
        search_id = event_data.media_dict.get("id")
        if search_id is not None:
            return {
                "source": event_data.convert_type,
                "mediaid": str(search_id),
            }, ""

    if not title:
        return None, "未知的媒体ID"

    meta = MetaInfo(title)
    if year:
        meta.year = year
    if media_type:
        meta.type = media_type
    if media_season is not None:
        meta.type = MediaType.TV
        meta.begin_season = media_season
    mediainfo = await MediaChain().async_recognize_by_meta(
        meta,
        obtain_images=False,
    )
    if not mediainfo:
        return None, "未识别到媒体信息"
    source, source_media_id = resolve_media_identity(media=mediainfo)
    if not source or not source_media_id:
        return None, "媒体信息缺少有效ID"
    return {"source": source, "mediaid": source_media_id}, ""


def _sse_event(data: dict, locale: Optional[str] = None) -> str:
    """
    转换为SSE事件
    """
    payload = data
    message = payload.get("message")
    text = payload.get("text")
    if isinstance(message, str) or isinstance(text, str):
        payload = data.copy()
        if isinstance(message, str):
            payload["message_i18n"] = LocaleHelper.translate_text(
                message, locale=locale
            )
        if isinstance(text, str):
            payload["text_i18n"] = LocaleHelper.translate_text(text, locale=locale)
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _serialize_signed_subtitle_result(subtitle: Any) -> dict:
    """
    序列化字幕结果并签名下载链接，签名用途绑定站点 ID。
    """
    data = subtitle.to_dict() if hasattr(subtitle, "to_dict") else dict(subtitle)
    enclosure = data.get("enclosure")
    if enclosure:
        data["enclosure"] = SecurityUtils.sign_url(
            enclosure,
            purpose=SecurityUtils.subtitle_download_purpose(data.get("site")),
        )
    return data


def _serialize_signed_subtitle_results(subtitles: List[Any]) -> List[dict]:
    """
    批量序列化字幕结果，确保返回给客户端的下载链接均已签名。
    """
    return [_serialize_signed_subtitle_result(subtitle) for subtitle in subtitles]


def _sign_subtitle_search_event(event: dict) -> dict:
    """
    签名字幕搜索流事件中的下载链接。
    """
    signed_event = dict(event)
    if "items" in signed_event:
        signed_event["items"] = _serialize_signed_subtitle_results(
            signed_event.get("items") or []
        )
    return signed_event


async def _iter_signed_subtitle_search_events(
    event_source: AsyncIterator[dict],
) -> AsyncIterator[dict]:
    """
    输出仅包含签名字幕下载链接的搜索流事件。
    """
    async for event in event_source:
        yield _sign_subtitle_search_event(event)


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


def _iter_replace_event_batches(event: dict) -> Iterator[dict]:
    """
    将超大的最终替换事件拆成有序批次，避免单个 SSE 消息承载全部完整对象。
    """
    items = event.get("items")
    if (
        event.get("type") != "replace"
        or not isinstance(items, list)
        or len(items) <= _SSE_REPLACE_MAX_ITEMS
    ):
        yield event
        return

    batch_count = (len(items) + _SSE_REPLACE_MAX_ITEMS - 1) // _SSE_REPLACE_MAX_ITEMS
    for batch_index in range(batch_count):
        start = batch_index * _SSE_REPLACE_MAX_ITEMS
        batch_event = dict(event)
        batch_event.update(
            {
                "type": "replace" if batch_index == 0 else "append",
                "items": items[start:start + _SSE_REPLACE_MAX_ITEMS],
                "replace_batch": True,
                "batch_index": batch_index,
                "batch_count": batch_count,
            }
        )
        yield batch_event


async def _iter_batched_search_events(
    event_source: AsyncIterator[dict],
) -> AsyncIterator[dict]:
    """
    对搜索流事件做轻量批处理，并在上游长时间静默时发送心跳。
    """
    iterator = event_source.__aiter__()
    pending_append_event: Optional[dict] = None
    next_event_task: Optional[asyncio.Task] = None

    try:
        while True:
            if next_event_task is None:
                next_event_task = asyncio.create_task(anext(iterator))

            timeout = (
                _SSE_APPEND_FLUSH_INTERVAL
                if pending_append_event
                else _SSE_HEARTBEAT_INTERVAL
            )
            done, _ = await asyncio.wait({next_event_task}, timeout=timeout)

            if not done:
                if pending_append_event:
                    yield pending_append_event
                    pending_append_event = None
                else:
                    yield {"type": "heartbeat"}
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

            for batched_event in _iter_replace_event_batches(event):
                yield batched_event
    finally:
        if next_event_task and not next_event_task.done():
            next_event_task.cancel()
            await asyncio.gather(next_event_task, return_exceptions=True)

    if pending_append_event:
        yield pending_append_event


async def _stream_search_events(request: Request, event_source: AsyncIterator[dict]):
    """
    输出搜索 SSE 事件，并记录连接生命周期与传输规模。
    """
    locale = LocaleHelper.get_locale_from_request(request)
    search_id = uuid4().hex[:12]
    request_path = getattr(getattr(request, "url", None), "path", "unknown")
    started_at = time.monotonic()
    event_count = 0
    transmitted_bytes = 0
    last_event_type = "none"
    last_stage = "none"
    termination_reason = "source_exhausted"
    logger.info(f"渐进式搜索流已建立，搜索ID：{search_id}，路径：{request_path}")
    try:
        has_sent_final_replace = False
        async for event in _iter_batched_search_events(event_source):
            last_event_type = event.get("type") or "unknown"
            last_stage = event.get("stage") or last_stage
            if await request.is_disconnected():
                termination_reason = "client_disconnected"
                logger.warning(
                    f"渐进式搜索客户端已断开，搜索ID：{search_id}，路径：{request_path}，"
                    f"事件：{last_event_type}，阶段：{last_stage}"
                )
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
            payload = _sse_event(event, locale=locale)
            event_count += 1
            transmitted_bytes += len(payload.encode("utf-8"))
            if event.get("type") == "done":
                termination_reason = "completed"
            yield payload
    except asyncio.CancelledError:
        termination_reason = "cancelled"
        logger.warning(
            f"渐进式搜索流已取消，搜索ID：{search_id}，路径：{request_path}，"
            f"事件：{last_event_type}，阶段：{last_stage}"
        )
        raise
    except Exception as err:
        termination_reason = "error"
        logger.error(f"渐进式搜索出错：{err}", exc_info=True)
        payload = _sse_event(
            {"type": "error", "success": False, "message": str(err)},
            locale=locale,
        )
        event_count += 1
        transmitted_bytes += len(payload.encode("utf-8"))
        yield payload
    finally:
        elapsed = time.monotonic() - started_at
        logger.info(
            f"渐进式搜索流结束，搜索ID：{search_id}，路径：{request_path}，"
            f"状态：{termination_reason}，事件数：{event_count}，"
            f"发送字节：{transmitted_bytes}，耗时：{elapsed:.2f}秒"
        )


@router.get("/last", summary="查询搜索结果", response_model=List[schemas.Context])
async def search_latest(_: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询搜索结果
    """
    torrents = await SearchChain().async_last_search_results() or []
    return [torrent.to_dict() for torrent in torrents]


@router.get("/last/context", summary="查询上次搜索上下文", response_model=schemas.Response)
async def search_latest_context(_: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询上次搜索结果及其对应的搜索参数。
    """
    search_chain = SearchChain()
    params = await search_chain.async_last_search_params() or {}
    if params.get("result_type") == "subtitle":
        results = await search_chain.async_last_subtitle_search_results() or []
    else:
        results = await search_chain.async_last_search_results() or []
    return schemas.Response(
        success=True,
        data={
            "params": params,
            "results": _serialize_signed_subtitle_results(results)
            if params.get("result_type") == "subtitle"
            else [result.to_dict() for result in results],
        },
    )


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

    media_type = _parse_media_type(mtype)
    media_season = int(season) if season else None
    site_list = _parse_site_list(sites)
    search_chain = SearchChain()

    async def event_source():
        """解析媒体身份并输出精确搜索流事件。"""
        search_params, message = await _resolve_media_search_params(
            mediaid=mediaid,
            media_type=media_type,
            title=title,
            year=year,
            media_season=media_season,
        )
        if not search_params:
            yield {"type": "error", "success": False, "message": message}
            return
        torrents = search_chain.async_search_by_id_stream(
            **search_params,
            mtype=media_type,
            area=area,
            season=media_season,
            sites=site_list,
            cache_local=True,
        )
        async for event in torrents:
            yield event

    return StreamingResponse(
        _stream_search_events(request, event_source()),
        media_type="text/event-stream",
        headers=_SSE_RESPONSE_HEADERS,
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
    根据带来源前缀的媒体 ID 精确搜索站点资源。
    """
    media_type = _parse_media_type(mtype)
    media_season = int(season) if season else None
    search_params, message = await _resolve_media_search_params(
        mediaid=mediaid,
        media_type=media_type,
        title=title,
        year=year,
        media_season=media_season,
    )
    if not search_params:
        return schemas.Response(success=False, message=message)
    torrents = await SearchChain().async_search_by_id(
        **search_params,
        mtype=media_type,
        area=area,
        season=media_season,
        sites=_parse_site_list(sites),
        cache_local=True,
    )
    if not torrents:
        return schemas.Response(success=False, message="未搜索到任何资源")
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
        _stream_search_events(request, event_source),
        media_type="text/event-stream",
        headers=_SSE_RESPONSE_HEADERS,
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


@router.get("/subtitle/title/stream", summary="渐进式模糊搜索字幕")
async def search_subtitle_by_title_stream(
    request: Request,
    keyword: Optional[str] = None,
    page: Optional[int] = 0,
    sites: Optional[str] = None,
    _: schemas.TokenPayload = Depends(verify_resource_token),
) -> Any:
    """
    根据名称渐进式模糊搜索站点字幕资源，返回格式为SSE。
    """

    event_source = SearchChain().async_search_subtitles_by_title_stream(
        title=keyword, page=page, sites=_parse_site_list(sites), cache_local=True
    )
    return StreamingResponse(
        _stream_search_events(
            request,
            _iter_signed_subtitle_search_events(event_source),
        ),
        media_type="text/event-stream",
        headers=_SSE_RESPONSE_HEADERS,
    )


@router.get("/subtitle/title", summary="模糊搜索字幕", response_model=schemas.Response)
async def search_subtitle_by_title(
    keyword: Optional[str] = None,
    page: Optional[int] = 0,
    sites: Optional[str] = None,
    _: schemas.TokenPayload = Depends(verify_token),
) -> Any:
    """
    根据名称模糊搜索站点字幕资源，支持分页。
    """
    subtitles = await SearchChain().async_search_subtitles_by_title(
        title=keyword, page=page, sites=_parse_site_list(sites), cache_local=True
    )
    if not subtitles:
        return schemas.Response(success=False, message="未搜索到任何字幕")
    return schemas.Response(
        success=True, data=_serialize_signed_subtitle_results(subtitles)
    )


async def _build_subtitle_search_source(
    mediaid: str,
    mtype: Optional[str] = None,
    title: Optional[str] = None,
    year: Optional[str] = None,
    season: Optional[str] = None,
    episode: Optional[str] = None,
    sites: Optional[str] = None,
    stream: bool = False,
) -> Any:
    """
    根据媒体ID构建字幕精确搜索调用，兼容多种媒体ID来源。
    """
    media_type = _parse_media_type(mtype)
    media_season = int(season) if season else None
    media_episode = int(episode) if episode else None
    site_list = _parse_site_list(sites)
    search_chain = SearchChain()

    def call_search(**kwargs):
        """
        根据调用模式返回普通搜索协程或流式搜索迭代器。
        """
        params = {
            **kwargs,
            "mtype": media_type,
            "season": media_season,
            "episode": media_episode,
            "sites": site_list,
            "cache_local": True,
        }
        if stream:
            return search_chain.async_search_subtitles_by_id_stream(**params)
        return search_chain.async_search_subtitles_by_id(**params)

    search_params, message = await _resolve_media_search_params(
        mediaid=mediaid,
        media_type=media_type,
        title=title,
        year=year,
        media_season=media_season,
    )
    if not search_params:
        return None, message
    return call_search(**search_params), ""


@router.get("/subtitle/media/{mediaid}/stream", summary="渐进式精确搜索字幕")
async def search_subtitle_by_id_stream(
    request: Request,
    mediaid: str,
    mtype: Optional[str] = None,
    title: Optional[str] = None,
    year: Optional[str] = None,
    season: Optional[str] = None,
    episode: Optional[str] = None,
    sites: Optional[str] = None,
    _: schemas.TokenPayload = Depends(verify_resource_token),
) -> Any:
    """
    根据带来源前缀的媒体 ID 渐进式精确搜索站点字幕资源，返回格式为SSE。
    """
    subtitles, message = await _build_subtitle_search_source(
        mediaid=mediaid,
        mtype=mtype,
        title=title,
        year=year,
        season=season,
        episode=episode,
        sites=sites,
        stream=True,
    )

    async def event_source():
        """
        输出字幕精确搜索流事件。
        """
        if not subtitles:
            yield {"type": "error", "success": False, "message": message or "未搜索到任何字幕"}
            return
        async for event in subtitles:
            yield event

    return StreamingResponse(
        _stream_search_events(
            request,
            _iter_signed_subtitle_search_events(event_source()),
        ),
        media_type="text/event-stream",
        headers=_SSE_RESPONSE_HEADERS,
    )


@router.get("/subtitle/media/{mediaid}", summary="精确搜索字幕", response_model=schemas.Response)
async def search_subtitle_by_id(
    mediaid: str,
    mtype: Optional[str] = None,
    title: Optional[str] = None,
    year: Optional[str] = None,
    season: Optional[str] = None,
    episode: Optional[str] = None,
    sites: Optional[str] = None,
    _: schemas.TokenPayload = Depends(verify_token),
) -> Any:
    """
    根据带来源前缀的媒体 ID 精确搜索站点字幕资源。
    """
    subtitles, message = await _build_subtitle_search_source(
        mediaid=mediaid,
        mtype=mtype,
        title=title,
        year=year,
        season=season,
        episode=episode,
        sites=sites,
    )
    if not subtitles:
        return schemas.Response(success=False, message=message or "未搜索到任何字幕")

    subtitles = await subtitles
    if not subtitles:
        return schemas.Response(success=False, message="未搜索到任何字幕")
    return schemas.Response(
        success=True, data=_serialize_signed_subtitle_results(subtitles)
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
