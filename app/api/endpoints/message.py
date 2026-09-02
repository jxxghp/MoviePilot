from __future__ import annotations

import json
import time
from typing import Annotated, Any, List, Optional, Protocol, Union

from fastapi import Depends, Request
from starlette.responses import PlainTextResponse

from app.adapters.external.wechat import WXBizMsgCrypt
from app.adapters.web.security.access import verify_apitoken, verify_token
from app.api.context import get_background_task_registry, resolve_background_task_registry
from app.api.dependencies.agent import get_message_query_service
from app.api.dependencies.auth import get_current_active_superuser
from app.api.principal import ApiPrincipal
from app.api.response import ResponseAPIRouter
from app.application.configuration import (
    get_api_runtime_config_snapshot,
    get_configured_system_config,
)
from app.application.messaging.message import MessageQueryService
from app.application.notification import get_notification_configs
from app.chain.message import MessageChain
from app.runtime.log import logger
from app.runtime.tasks import TaskRegistry
from app.runtime.webpush import webpush_registry
from app.schemas.message import MessageClearBefore as _SchemaMessageClearBefore
from app.schemas.message import MessageClearData as _SchemaMessageClearData
from app.schemas.message import MessageClearScope as _SchemaMessageClearScope
from app.schemas.message import MessageHistoryItem as _SchemaMessageHistoryItem
from app.schemas.message import Subscription as _SchemaSubscription
from app.schemas.message import SubscriptionMessage as _SchemaSubscriptionMessage
from app.schemas.message import WebMessageItem as _SchemaWebMessageItem
from app.schemas.response import Response as _SchemaResponse
from app.schemas.token import TokenPayload as _SchemaTokenPayload
from app.schemas.types import NotificationChannel, SystemConfigKey

router = ResponseAPIRouter()

_WNS_DEFAULT_TTL = 86400


class WebPushError(Protocol):
    """Web Push 订阅状态判断所需的最小异常协议。"""

    response: Any  # 推送服务响应，状态码字段由具体 SDK 提供


def is_webpush_subscription_gone(error: WebPushError) -> bool:
    """判断 Web Push 订阅是否已在浏览器或推送服务侧失效。"""
    response: Any = getattr(error, "response", None)
    status_code = getattr(response, "status_code", None) or getattr(
        response,
        "status",
        None,
    )
    return status_code in {404, 410}


def is_wns_endpoint(endpoint: str | None) -> bool:
    """判断是否为 Microsoft WNS 推送端点。"""
    return bool(endpoint and "notify.windows.com" in endpoint)


def webpush_options_for_endpoint(endpoint: str | None) -> dict[str, Any]:
    """返回指定推送端点需要的 pywebpush 附加参数。"""
    if not is_wns_endpoint(endpoint):
        return {}
    return {
        "ttl": _WNS_DEFAULT_TTL,
        "headers": {"X-WNS-Cache-Policy": "cache"},
    }


def _normalize_notification_clear_timestamp(value: Any) -> int:
    """
    规范化通知清理时间戳。
    """
    try:
        normalized_value = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return normalized_value if normalized_value > 0 else 0


def _get_notification_clear_before() -> _SchemaMessageClearBefore:
    """
    读取通知中心清理时间配置。
    """
    value = get_configured_system_config().get(SystemConfigKey.NotificationClearBefore)
    if isinstance(value, dict):
        return _SchemaMessageClearBefore(
            all=_normalize_notification_clear_timestamp(value.get("all")),
            system=_normalize_notification_clear_timestamp(value.get("system")),
            media=_normalize_notification_clear_timestamp(value.get("media")),
        )
    return _SchemaMessageClearBefore(
        all=_normalize_notification_clear_timestamp(value),
    )


def _format_notification_clear_time(value: int) -> Optional[str]:
    """
    将清理时间戳转换为消息表使用的时间字符串。
    """
    if not value:
        return None
    timestamp = value / 1000 if value > 10000000000 else value
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))


def start_message_chain(body: Any, form: Any, args: Any):
    """
    启动链式任务
    """
    MessageChain().process(body=body, form=form, args=args)


@router.post("/", summary="接收用户消息", response_model=_SchemaResponse[None])
async def user_message(
    task_registry: Annotated[TaskRegistry, Depends(get_background_task_registry)],
    request: Request,
    _: _SchemaTokenPayload = Depends(verify_apitoken),
):
    """
    用户消息响应；推荐通过 X-API-KEY 请求头传递 API_TOKEN，查询参数 token 仅保留兼容。
    """
    body = await request.body()
    form = await request.form()
    args = request.query_params
    source = args.get("source")
    content_type = request.headers.get("content-type", "")
    body_text = body.decode("utf-8", errors="replace")
    image_markers = [
        marker
        for marker in (
            '"photo"',
            '"document"',
            '"files"',
            '"attachments"',
            '"url_private"',
            '"image/"',
            '"image_url"',
        )
        if marker in body_text
    ]
    logger.info(
        "消息入口收到请求: source=%s, content_type=%s, body_bytes=%s, form_keys=%s, image_markers=%s",
        source,
        content_type,
        len(body),
        list(form.keys()) if form else [],
        image_markers,
    )
    resolve_background_task_registry(task_registry).create_sync(
        start_message_chain, body, form, args, owner="api.message.user"
    )
    return _SchemaResponse(success=True)


@router.post("/web", summary="接收WEB消息", response_model=_SchemaResponse[None])
async def web_message(
    request: Request,
    text: Optional[str] = None,
    current_user: ApiPrincipal = Depends(get_current_active_superuser),
):
    """
    WEB消息响应
    """
    images = None
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            payload = await request.json()
        except Exception:
            payload = None
        if isinstance(payload, dict):
            text = payload.get("text", text)
            image = payload.get("image")
            images = payload.get("images")
            if image:
                if isinstance(images, list):
                    images = [*images, image]
                else:
                    images = [image]
            elif isinstance(images, str):
                images = [images]

    MessageChain().handle_message(
        channel=NotificationChannel.Web,
        source=current_user.name,
        userid=current_user.name,
        username=current_user.name,
        text=text or "",
        images=images,
    )
    return _SchemaResponse(success=True)


@router.get("/web", summary="获取WEB消息", response_model=List[_SchemaWebMessageItem])
async def get_web_message(
    _: _SchemaTokenPayload = Depends(verify_token),
    service: MessageQueryService = Depends(get_message_query_service),
    page: Optional[int] = 1,
    count: Optional[int] = 20,
):
    """
    获取WEB消息列表
    """
    return await service.list_web(page=page, count=count)


@router.get("/notification", summary="获取通知消息", response_model=List[_SchemaMessageHistoryItem])
async def get_notification_message(
    _: _SchemaTokenPayload = Depends(verify_token),
    service: MessageQueryService = Depends(get_message_query_service),
    page: Optional[int] = 1,
    count: Optional[int] = 20,
):
    """
    获取系统发送的通知消息列表。
    """
    clear_before = _get_notification_clear_before()
    messages = await service.list_notifications(
        page=page,
        count=count,
        all_clear_before=_format_notification_clear_time(clear_before.all),
        system_clear_before=_format_notification_clear_time(clear_before.system),
        media_clear_before=_format_notification_clear_time(clear_before.media),
    )
    return [_SchemaMessageHistoryItem(**message) for message in messages]


@router.delete(
    "/notification",
    summary="清理通知消息",
    response_model=_SchemaResponse[_SchemaMessageClearData],
)
async def clear_notification_message(
    scope: _SchemaMessageClearScope = _SchemaMessageClearScope.All,
    _: _SchemaTokenPayload = Depends(verify_token),
):
    """
    记录通知中心清理时间，后续通知历史查询会在服务端过滤。
    """
    clear_before = _get_notification_clear_before()
    value = clear_before.model_dump()
    value[scope.value] = int(time.time() * 1000)
    await get_configured_system_config().async_set(SystemConfigKey.NotificationClearBefore, value)
    return _SchemaResponse(success=True, data={"clear_before": value})


def wechat_verify(
    echostr: str,
    msg_signature: str,
    timestamp: Union[str, int],
    nonce: str,
    source: Optional[str] = None,
) -> Any:
    """
    微信验证响应
    """
    # 获取服务配置
    client_configs = get_notification_configs(include_disabled=True)
    if not client_configs:
        return "未找到对应的消息配置"
    client_config = next(
        (
            config
            for config in client_configs
            if config.type == "wechat"
            and config.enabled
            and config.config.get("WECHAT_MODE", "app") != "bot"
            and (not source or config.name == source)
        ),
        None,
    )
    if not client_config:
        return "未找到对应的消息配置"
    try:
        wxcpt = WXBizMsgCrypt(
            sToken=client_config.config.get("WECHAT_TOKEN"),
            sEncodingAESKey=client_config.config.get("WECHAT_ENCODING_AESKEY"),
            sReceiveId=client_config.config.get("WECHAT_CORPID"),
        )
        ret, sEchoStr = wxcpt.VerifyURL(
            sMsgSignature=msg_signature,
            sTimeStamp=timestamp,
            sNonce=nonce,
            sEchoStr=echostr,
        )
        if ret == 0:
            # 验证URL成功，将sEchoStr返回给企业号
            return PlainTextResponse(sEchoStr)
        return "微信验证失败"
    except Exception as err:
        logger.error(f"微信请求验证失败: {str(err)}")
        return str(err)


def vocechat_verify() -> Any:
    """
    VoceChat验证响应
    """
    return {"status": "OK"}


@router.get(
    "/",
    summary="回调请求验证",
    response_model=None,
    responses={
        200: {
            "description": "消息平台原生验证响应",
            "content": {
                "text/plain": {"schema": {"type": "string"}},
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {"status": {"type": "string"}},
                    }
                },
            },
        }
    },
)
def incoming_verify(
    token: Optional[str] = None,
    echostr: Optional[str] = None,
    msg_signature: Optional[str] = None,
    timestamp: Union[str, int] = None,
    nonce: Optional[str] = None,
    source: Optional[str] = None,
    _: _SchemaTokenPayload = Depends(verify_apitoken),
) -> Any:
    """
    微信/VoceChat等验证响应
    """
    logger.info(
        f"收到验证请求: token={token}, echostr={echostr}, "
        f"msg_signature={msg_signature}, timestamp={timestamp}, nonce={nonce}"
    )
    if echostr and msg_signature and timestamp and nonce:
        return wechat_verify(echostr, msg_signature, timestamp, nonce, source)
    return vocechat_verify()


@router.post(
    "/webpush/subscribe",
    summary="客户端webpush通知订阅",
    response_model=_SchemaResponse[None],
)
async def subscribe(
    subscription: _SchemaSubscription, _: _SchemaTokenPayload = Depends(verify_token)
):
    """
    客户端webpush通知订阅
    """
    subinfo = subscription.model_dump()
    webpush_registry.upsert(subinfo)
    logger.debug(f"通知订阅成功: {subinfo}")
    return _SchemaResponse(success=True)


@router.post(
    "/webpush/send", summary="发送webpush通知", response_model=_SchemaResponse[None]
)
def send_notification(
    payload: _SchemaSubscriptionMessage,
    _: _SchemaTokenPayload = Depends(verify_token),
):
    """
    发送webpush通知
    """
    from pywebpush import WebPushException, webpush

    for sub in webpush_registry.list():
        try:
            webpush(
                subscription_info=sub,
                data=json.dumps(payload.model_dump()),
                vapid_private_key=get_api_runtime_config_snapshot().vapid_private_key,
                vapid_claims={"sub": get_api_runtime_config_snapshot().vapid_subject},
                **webpush_options_for_endpoint(sub.get("endpoint")),
            )
        except WebPushException as err:
            logger.error(f"WebPush发送失败: {str(err)}")
            if is_webpush_subscription_gone(err) and webpush_registry.remove(sub):
                logger.info(f"已移除失效WebPush订阅: {sub.get('endpoint')}")
            continue
    return _SchemaResponse(success=True)
