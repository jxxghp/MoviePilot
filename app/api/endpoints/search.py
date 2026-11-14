"""
提供媒体资源搜索及搜索结果查询接口。
"""

import asyncio
import json
import time
from typing import Any, AsyncGenerator, AsyncIterator, Coroutine, Iterator, List, Optional, cast
from uuid import uuid4

from fastapi import Body, Depends, Request
from fastapi.responses import StreamingResponse

from app.adapters.web.security.access import verify_resource_token, verify_token
from app.api.response import (
    CompatibleCountParam,
    CompatiblePageParam,
    ResponseAPIRouter,
)
from app.application.search.state import SearchStateService
from app.application.security.url import SecurityUtils
from app.chain.search.facade import SearchChain
from app.domain.context import Context
from app.domain.media import normalize_music_type
from app.runtime.localization import LocaleHelper
from app.runtime.log import logger
from app.schemas.context import Context as _SchemaContext
from app.schemas.context import SubtitleInfo as _SchemaSubtitleInfo
from app.schemas.media import resolve_media_identity
from app.schemas.response import Response as _SchemaResponse
from app.schemas.search import SearchLastContextData as _SchemaSearchLastContextData
from app.schemas.search import SearchRecommendStatusData as _SchemaSearchRecommendStatusData
from app.schemas.token import TokenPayload as _SchemaTokenPayload
from app.schemas.types import MediaSource, MediaType

router = ResponseAPIRouter()

_SSE_APPEND_FLUSH_INTERVAL = 1
_SSE_APPEND_MAX_ITEMS = 48
_SSE_HEARTBEAT_INTERVAL = 15
_SSE_REPLACE_MAX_ITEMS = 48
_SSE_RESPONSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
}


def _search_user_id(payload: object) -> Optional[int]:
    """
    仅从已验证的令牌载荷读取身份，缺失身份时不保存或回退全局记录。
    """
    return payload.sub if isinstance(payload, _SchemaTokenPayload) else None


async def _save_user_search_context(
    user_id: Optional[int], *, params: dict[str, Any], results: list[dict[str, Any]],
) -> None:
    """
    普通搜索也发布无诊断的用户快照，防止继续显示旧的页搜索诊断。

    :param user_id: 认证依赖提供的用户主键；缺失时不保存
    :param params: 本次实际搜索参数
    :param results: 本次完整结果；字幕保留未签名地址，读取时重新签名
    """
    if user_id is not None:
        await SearchChain().async_save_search_context(user_id, _SchemaSearchLastContextData.model_validate({
            "params": SearchStateService.build_params(**params) or {}, "results": results,
        }))


async def _iter_user_search_events(
    source: AsyncIterator[dict[str, Any]], user_id: Optional[int], *, params: dict[str, Any],
) -> AsyncIterator[dict[str, Any]]:
    """
    旧 SSE 仅在最终完成时发布无诊断快照，错误与取消不覆盖最近记录。

    :param source: 未经传输拆包或字幕签名的原始搜索事件
    :param user_id: 认证依赖提供的用户主键
    :param params: 本次实际搜索参数
    :yields: 保持原有顺序与字段的事件
    """
    try:
        async for event in source:
            if event.get("type") == "done" and event.get("stage") in (None, "done"):
                await _save_user_search_context(user_id, params=params, results=event.get("items") or [])
            yield event
    finally:
        close = getattr(source, "aclose", None)
        if close is not None:
            await close()


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
    """
    合并显式季号与识别结果，显式值优先且季 0 属于有效业务值。
    """
    return explicit_season if explicit_season is not None else recognized_season


async def _resolve_media_search_params(
        media_source: MediaSource,
        media_id: str,
        media_type: Optional[MediaType] = None,
        music_type: Optional[str] = None,
) -> tuple[Optional[dict[str, Any]], str]:
    """
    校验统一媒体身份并构造 SearchChain 精确搜索参数。
    """
    normalized_source, normalized_media_id = resolve_media_identity(
        media_source=media_source,
        media_id=media_id,
    )
    if not normalized_source or not normalized_media_id:
        return None, "媒体ID格式无效"
    normalized_music_type = None
    if music_type:
        normalized_music_type = normalize_music_type(music_type, allow_artist=False)
        if not normalized_music_type:
            return None, "音乐实体类型无效，仅支持 recording 或 album"
        if media_type != MediaType.MUSIC:
            return None, "music_type 仅能用于音乐资源搜索"

    params = {
        "media_source": normalized_source,
        "media_id": normalized_media_id,
    }
    if normalized_music_type:
        params["music_type"] = normalized_music_type
    return params, ""


def _sse_event(data: dict[str, Any], locale: Optional[str] = None) -> str:
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


def _serialize_signed_subtitle_result(subtitle: Any) -> dict[str, Any]:
    """
    序列化字幕结果并签名下载链接，签名用途绑定站点 ID。
    """
    data = subtitle.to_dict() if hasattr(subtitle, "to_dict") else dict(subtitle)
    enclosure = data.get("enclosure")
    site_id = data.get("site")
    if enclosure:
        if not isinstance(site_id, int):
            raise ValueError("字幕结果缺少有效站点 ID，无法签名下载链接")
        data["enclosure"] = SecurityUtils.sign_url(
            enclosure,
            purpose=SecurityUtils.subtitle_download_purpose(site_id),
        )
    return data


def _serialize_signed_subtitle_results(subtitles: List[Any]) -> List[dict[str, Any]]:
    """
    批量序列化字幕结果，确保返回给客户端的下载链接均已签名。
    """
    return [_serialize_signed_subtitle_result(subtitle) for subtitle in subtitles]


def _serialize_context_results(contexts: list[Context]) -> list[dict[str, Any]]:
    """
    序列化 canonical 搜索上下文，集中保持传输层投影合同。
    """
    return [context.to_dict() for context in contexts]


def _sign_subtitle_search_event(event: dict[str, Any]) -> dict[str, Any]:
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
    event_source: AsyncIterator[dict[str, Any]],
) -> AsyncIterator[dict[str, Any]]:
    """
    输出仅包含签名字幕下载链接的搜索流事件。
    """
    async for event in event_source:
        yield _sign_subtitle_search_event(event)


def _merge_append_event(pending_event: Optional[dict[str, Any]], event: dict[str, Any]) -> dict[str, Any]:
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


def _iter_replace_event_batches(event: dict[str, Any]) -> Iterator[dict[str, Any]]:
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
    event_source: AsyncIterator[dict[str, Any]],
) -> AsyncGenerator[dict[str, Any], None]:
    """
    对搜索流事件做轻量批处理，并在上游长时间静默时发送心跳。
    """
    iterator = event_source.__aiter__()
    pending_append_event: Optional[dict[str, Any]] = None
    next_event_task: Optional[asyncio.Future[dict[str, Any]]] = None

    try:
        while True:
            if next_event_task is None:
                next_event_task = asyncio.create_task(
                    cast(Coroutine[Any, Any, dict[str, Any]], anext(iterator))
                )

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
        close_iterator = getattr(iterator, "aclose", None)
        if close_iterator is not None:
            await close_iterator()

    if pending_append_event:
        yield pending_append_event


async def _stream_search_events(
        request: Request, event_source: AsyncIterator[dict[str, Any]],
) -> AsyncGenerator[str, None]:
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
    batched_events = _iter_batched_search_events(event_source)
    logger.info(f"渐进式搜索流已建立，搜索ID：{search_id}，路径：{request_path}")
    try:
        has_sent_final_replace = False
        async for event in batched_events:
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
            {
                "type": "error",
                "success": False,
                "message": "搜索失败，请稍后重试",
            },
            locale=locale,
        )
        event_count += 1
        transmitted_bytes += len(payload.encode("utf-8"))
        yield payload
    finally:
        await batched_events.aclose()
        elapsed = time.monotonic() - started_at
        logger.info(
            f"渐进式搜索流结束，搜索ID：{search_id}，路径：{request_path}，"
            f"状态：{termination_reason}，事件数：{event_count}，"
            f"发送字节：{transmitted_bytes}，耗时：{elapsed:.2f}秒"
        )


@router.get("/last", summary="查询搜索结果", response_model=List[_SchemaContext])
async def search_latest(_: _SchemaTokenPayload = Depends(verify_token), page: CompatiblePageParam = None, count: CompatibleCountParam = None) -> Any:
    """
    查询搜索结果
    """
    torrents = await SearchChain().async_last_search_results() or []
    return _serialize_context_results(torrents)


@router.get(
    "/last/context",
    summary="查询上次搜索上下文",
    response_model=_SchemaResponse[_SchemaSearchLastContextData],
)
async def search_latest_context(_: _SchemaTokenPayload = Depends(verify_token)) -> Any:
    """
    查询当前用户最近完成的搜索上下文，首次搜索前返回空快照。
    """
    user_id = _search_user_id(_)
    context = await SearchChain().async_last_search_context(user_id) if user_id is not None else _SchemaSearchLastContextData()
    if context.params.get("result_type") == "subtitle":
        context = _SchemaSearchLastContextData.model_validate({
            "params": context.params,
            "results": _serialize_signed_subtitle_results([item.model_dump(mode="json") for item in context.results]),
            "diagnostics": context.diagnostics,
        })
    return _SchemaResponse(success=True, data=context)


@router.get(
    "/media/{media_id}/stream",
    summary="渐进式精确搜索资源",
    response_model=None,
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "资源搜索 SSE 事件流",
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        }
    },
)
async def search_by_id_stream(
    request: Request,
    media_id: str,
    media_source: MediaSource,
    mtype: Optional[str] = None,
    area: Optional[str] = "title",
    season: Optional[str] = None,
    sites: Optional[str] = None,
    music_type: Optional[str] = None,
    include_candidates: bool = False,
    manual_paging: bool = False,
    page: int = 0,
    source: Optional[str] = None,
    _: _SchemaTokenPayload = Depends(verify_resource_token),
) -> Any:
    """
    根据媒体来源和原生 ID 渐进式搜索站点资源，返回格式为 SSE。
    """

    media_type = _parse_media_type(mtype)
    media_season = int(season) if season else None
    site_list = _parse_site_list(sites)

    async def event_source() -> AsyncGenerator[dict[str, Any], None]:
        """
        解析媒体身份并输出精确搜索流事件。
        """
        search_params, message = await _resolve_media_search_params(
            media_source=media_source,
            media_id=media_id,
            media_type=media_type,
            music_type=music_type,
        )
        if not search_params:
            yield {"type": "error", "success": False, "message": message}
            return
        if include_candidates:
            search_params["include_candidates"] = True
        if manual_paging and media_type != MediaType.MUSIC:
            async for event in SearchChain().search_page_events(params={
                "media_source": media_source, "media_id": media_id, "mtype": media_type,
                "area": area, "season": media_season, "sites": site_list,
                "page": page, "source": source,
                "user_id": _search_user_id(_),
            }):
                yield event
            return
        torrents = SearchChain().async_search_by_id_stream(
            **search_params,
            mtype=media_type,
            area=area,
            season=media_season,
            sites=site_list,
            cache_local=True,
        )
        async for event in _iter_user_search_events(torrents, _search_user_id(_), params={
            **search_params, "mtype": media_type, "area": area, "season": media_season, "sites": site_list,
        }):
            yield event

    return StreamingResponse(
        _stream_search_events(request, event_source()),
        media_type="text/event-stream",
        headers=_SSE_RESPONSE_HEADERS,
    )


@router.get(
    "/media/{media_id}",
    summary="精确搜索资源",
    response_model=List[_SchemaContext],
)
async def search_by_id(
    media_id: str,
    media_source: MediaSource,
    mtype: Optional[str] = None,
    area: Optional[str] = "title",
    season: Optional[str] = None,
    sites: Optional[str] = None,
    music_type: Optional[str] = None,
    include_candidates: bool = False,
    _: _SchemaTokenPayload = Depends(verify_token),
    page: CompatiblePageParam = None,
    count: CompatibleCountParam = None,
) -> Any:
    """
    根据媒体来源和原生 ID 精确搜索站点资源。
    """
    media_type = _parse_media_type(mtype)
    media_season = int(season) if season else None
    search_params, message = await _resolve_media_search_params(
        media_source=media_source,
        media_id=media_id,
        media_type=media_type,
        music_type=music_type,
    )
    if not search_params:
        return _SchemaResponse(success=False, message=message)
    if include_candidates:
        search_params["include_candidates"] = True
    torrents = await SearchChain().async_search_by_id(
        **search_params,
        mtype=media_type,
        area=area,
        season=media_season,
        sites=_parse_site_list(sites),
        cache_local=True,
    )
    serialized_results = _serialize_context_results(torrents or [])
    await _save_user_search_context(_search_user_id(_), params={
        **search_params, "mtype": media_type, "area": area, "season": media_season,
        "sites": _parse_site_list(sites),
    }, results=serialized_results)
    if not torrents:
        return _SchemaResponse(success=False, message="未搜索到任何资源")
    return _SchemaResponse(
        success=True, data=serialized_results
    )


@router.get(
    "/title/stream",
    summary="渐进式模糊搜索资源",
    response_model=None,
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "资源搜索 SSE 事件流",
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        }
    },
)
async def search_by_title_stream(
    request: Request,
    keyword: Optional[str] = None,
    mtype: Optional[str] = None,
    page: Optional[int] = 0,
    sites: Optional[str] = None,
    manual_paging: bool = False,
    source: Optional[str] = None,
    _: _SchemaTokenPayload = Depends(verify_resource_token),
) -> Any:
    """
    根据名称渐进式模糊搜索站点资源，返回格式为SSE
    """

    if manual_paging and keyword and _parse_media_type(mtype) != MediaType.MUSIC:
        event_source = SearchChain().search_page_events(params={
            "keyword": keyword, "mtype": _parse_media_type(mtype), "sites": _parse_site_list(sites),
            "page": page, "source": source,
            "user_id": _search_user_id(_),
        })
    else:
        event_source = _iter_user_search_events(SearchChain().async_search_by_title_stream(
            title=keyword or "",
            page=page,
            sites=_parse_site_list(sites),
            cache_local=True,
            mtype=_parse_media_type(mtype),
        ), _search_user_id(_), params={
            "keyword": keyword or "", "mtype": _parse_media_type(mtype), "sites": _parse_site_list(sites),
        })
    return StreamingResponse(
        _stream_search_events(request, event_source),
        media_type="text/event-stream",
        headers=_SSE_RESPONSE_HEADERS,
    )


@router.get(
    "/title",
    summary="模糊搜索资源",
    response_model=List[_SchemaContext],
)
async def search_by_title(
    keyword: Optional[str] = None,
    mtype: Optional[str] = None,
    page: Optional[int] = 0,
    sites: Optional[str] = None,
    _: _SchemaTokenPayload = Depends(verify_token),
) -> Any:
    """
    根据名称模糊搜索站点资源，支持分页，关键词为空是返回首页资源
    """
    torrents = await SearchChain().async_search_by_title(
        title=keyword or "",
        page=page,
        sites=_parse_site_list(sites),
        cache_local=True,
        mtype=_parse_media_type(mtype),
    )
    serialized_results = _serialize_context_results(torrents or [])
    await _save_user_search_context(_search_user_id(_), params={
        "keyword": keyword or "", "mtype": _parse_media_type(mtype), "sites": _parse_site_list(sites),
    }, results=serialized_results)
    if not torrents:
        return _SchemaResponse(success=False, message="未搜索到任何资源")
    return _SchemaResponse(
        success=True, data=serialized_results
    )


@router.get(
    "/subtitle/title/stream",
    summary="渐进式模糊搜索字幕",
    response_model=None,
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "字幕搜索 SSE 事件流",
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        }
    },
)
async def search_subtitle_by_title_stream(
    request: Request,
    keyword: Optional[str] = None,
    page: Optional[int] = 0,
    sites: Optional[str] = None,
    _: _SchemaTokenPayload = Depends(verify_resource_token),
) -> Any:
    """
    根据名称渐进式模糊搜索站点字幕资源，返回格式为SSE。
    """

    event_source = SearchChain().async_search_subtitles_by_title_stream(
        title=keyword or "",
        page=page,
        sites=_parse_site_list(sites),
        cache_local=True,
    )
    return StreamingResponse(
        _stream_search_events(
            request,
            _iter_signed_subtitle_search_events(_iter_user_search_events(event_source, _search_user_id(_), params={
                "keyword": keyword or "", "sites": _parse_site_list(sites), "result_type": "subtitle",
            })),
        ),
        media_type="text/event-stream",
        headers=_SSE_RESPONSE_HEADERS,
    )


@router.get(
    "/subtitle/title",
    summary="模糊搜索字幕",
    response_model=_SchemaResponse[list[_SchemaSubtitleInfo]],
)
async def search_subtitle_by_title(
    keyword: Optional[str] = None,
    page: Optional[int] = 0,
    sites: Optional[str] = None,
    _: _SchemaTokenPayload = Depends(verify_token),
) -> Any:
    """
    根据名称模糊搜索站点字幕资源，支持分页。
    """
    subtitles = await SearchChain().async_search_subtitles_by_title(
        title=keyword or "",
        page=page,
        sites=_parse_site_list(sites),
        cache_local=True,
    )
    await _save_user_search_context(_search_user_id(_), params={
        "keyword": keyword or "", "sites": _parse_site_list(sites), "result_type": "subtitle",
    }, results=[subtitle.to_dict() for subtitle in subtitles or []])
    if not subtitles:
        return _SchemaResponse(success=False, message="未搜索到任何字幕")
    return _SchemaResponse(
        success=True, data=_serialize_signed_subtitle_results(subtitles)
    )


async def _build_subtitle_search_source(
    media_source: MediaSource,
    media_id: str,
    mtype: Optional[str] = None,
    season: Optional[str] = None,
    episode: Optional[str] = None,
    sites: Optional[str] = None,
    stream: bool = False,
) -> Any:
    """
    根据媒体来源和原生 ID 构建字幕精确搜索调用。
    """
    media_type = _parse_media_type(mtype)
    media_season = int(season) if season else None
    media_episode = int(episode) if episode else None
    site_list = _parse_site_list(sites)

    def call_search(**kwargs: Any) -> Any:
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
        media_source=media_source,
        media_id=media_id,
        media_type=media_type,
    )
    if not search_params:
        return None, message
    search_chain = SearchChain()
    return call_search(**search_params), ""


@router.get(
    "/subtitle/media/{media_id}/stream",
    summary="渐进式精确搜索字幕",
    response_model=None,
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "字幕搜索 SSE 事件流",
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        }
    },
)
async def search_subtitle_by_id_stream(
    request: Request,
    media_id: str,
    media_source: MediaSource,
    mtype: Optional[str] = None,
    season: Optional[str] = None,
    episode: Optional[str] = None,
    sites: Optional[str] = None,
    _: _SchemaTokenPayload = Depends(verify_resource_token),
) -> Any:
    """
    根据媒体来源和原生 ID 渐进式精确搜索站点字幕资源，返回格式为 SSE。
    """
    subtitles, message = await _build_subtitle_search_source(
        media_source=media_source,
        media_id=media_id,
        mtype=mtype,
        season=season,
        episode=episode,
        sites=sites,
        stream=True,
    )

    async def event_source() -> AsyncGenerator[dict[str, Any], None]:
        """
        输出字幕精确搜索流事件。
        """
        if not subtitles:
            yield {"type": "error", "success": False, "message": message or "未搜索到任何字幕"}
            return
        async for event in _iter_user_search_events(subtitles, _search_user_id(_), params={
            "media_source": media_source, "media_id": media_id, "mtype": _parse_media_type(mtype),
            "season": int(season) if season else None, "episode": int(episode) if episode else None,
            "sites": _parse_site_list(sites), "result_type": "subtitle",
        }):
            yield event

    return StreamingResponse(
        _stream_search_events(
            request,
            _iter_signed_subtitle_search_events(event_source()),
        ),
        media_type="text/event-stream",
        headers=_SSE_RESPONSE_HEADERS,
    )


@router.get(
    "/subtitle/media/{media_id}",
    summary="精确搜索字幕",
    response_model=_SchemaResponse[list[_SchemaSubtitleInfo]],
)
async def search_subtitle_by_id(
    media_id: str,
    media_source: MediaSource,
    mtype: Optional[str] = None,
    season: Optional[str] = None,
    episode: Optional[str] = None,
    sites: Optional[str] = None,
    _: _SchemaTokenPayload = Depends(verify_token),
    page: CompatiblePageParam = None,
    count: CompatibleCountParam = None,
) -> Any:
    """
    根据媒体来源和原生 ID 精确搜索站点字幕资源。
    """
    subtitles, message = await _build_subtitle_search_source(
        media_source=media_source,
        media_id=media_id,
        mtype=mtype,
        season=season,
        episode=episode,
        sites=sites,
    )
    if not subtitles:
        return _SchemaResponse(success=False, message=message or "未搜索到任何字幕")

    subtitles = await subtitles
    await _save_user_search_context(_search_user_id(_), params={
        "media_source": media_source, "media_id": media_id, "mtype": _parse_media_type(mtype),
        "season": int(season) if season else None, "episode": int(episode) if episode else None,
        "sites": _parse_site_list(sites), "result_type": "subtitle",
    }, results=[subtitle.to_dict() for subtitle in subtitles or []])
    if not subtitles:
        return _SchemaResponse(success=False, message="未搜索到任何字幕")
    return _SchemaResponse(
        success=True, data=_serialize_signed_subtitle_results(subtitles)
    )


@router.post(
    "/recommend",
    summary="AI推荐资源",
    response_model=_SchemaResponse[_SchemaSearchRecommendStatusData],
)
async def recommend_search_results(
    filtered_indices: Optional[List[int]] = Body(
        None, embed=True, description="筛选后的索引列表"
    ),
    check_only: bool = Body(False, embed=True, description="仅检查状态，不启动新任务"),
    force: bool = Body(False, embed=True, description="强制重新推荐，清除旧结果"),
    _: _SchemaTokenPayload = Depends(verify_token),
) -> Any:
    """
    启动 AI 推荐或只读查询当前状态，禁用和只读状态无需读取搜索结果缓存。

    :param filtered_indices: 可选资源索引，省略时推荐当前全部候选
    :param check_only: 仅读取状态，不启动或取消任务，也不反序列化搜索缓存
    :param force: 清除旧状态并启动新任务，优先于 check_only
    :param _: 已验证的访问令牌
    :return: 推荐状态与已完成结果索引；推荐失败在 message 中返回原因
    """
    recommend_chain = SearchChain()
    # 禁用与只读轮询仅访问推荐状态，不反序列化可能很大的搜索结果缓存。
    if not recommend_chain.is_ai_recommend_enabled:
        return _SchemaResponse(success=True, data={"status": "disabled"})
    if check_only and not force:
        current_status = recommend_chain.get_current_recommend_status_only()
        if current_status.get("status") == "error":
            error_msg = current_status.pop("error", "未知错误")
            return _SchemaResponse(success=False, message=error_msg, data=current_status)
        return _SchemaResponse(success=True, data=current_status)

    # 只有真正启动或检查推荐输入变化时才读取候选资源。
    results = await recommend_chain.async_last_search_results() or []
    if not results:
        return _SchemaResponse(
            success=False, message="没有可用的搜索结果", data={"status": "error"}
        )

    # 如果是强制模式，先取消并清除旧结果，然后直接启动新任务
    if force:
        logger.info("收到新推荐请求，清除旧结果并启动新任务")
        recommend_chain.cancel_ai_recommend()
        recommend_chain.start_recommend_task(filtered_indices, len(results), results)
        # 直接返回运行中状态
        return _SchemaResponse(success=True, data={"status": "running"})

    # 获取当前状态（会检测请求是否变化）
    status_data = recommend_chain.get_recommend_status(filtered_indices, len(results))

    # 如果功能未启用，直接返回禁用状态
    if status_data.get("status") == "disabled":
        return _SchemaResponse(success=True, data=status_data)

    # 如果是空闲状态，启动新任务
    if status_data["status"] == "idle":
        recommend_chain.start_recommend_task(filtered_indices, len(results), results)
        # 立即返回运行中状态
        return _SchemaResponse(success=True, data={"status": "running"})

    # 如果有错误，将错误信息放到message中
    if status_data.get("status") == "error":
        error_msg = status_data.pop("error", "未知错误")
        return _SchemaResponse(success=False, message=error_msg, data=status_data)

    # 返回当前状态
    return _SchemaResponse(success=True, data=status_data)
