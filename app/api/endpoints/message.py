"""
提供消息历史、通知回调和浏览器 Web Push 订阅管理接口。
"""

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
from app.api.dependencies.auth import get_current_active_superuser, get_current_active_user
from app.api.principal import ApiPrincipal
from app.api.response import (
    PositiveCountParam,
    PositivePageParam,
    ResponseAPIRouter,
)
from app.application.configuration import (
    get_api_runtime_config_snapshot,
    get_configured_system_config,
)
from app.application.messaging.message import MessageQueryService, MessageViewer
from app.application.notification import get_notification_configs, get_user_notification_types
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
from app.schemas.message import WebPushPublicKeyData as _SchemaWebPushPublicKeyData
from app.schemas.response import Response as _SchemaResponse
from app.schemas.token import TokenPayload as _SchemaTokenPayload
from app.schemas.types import NotificationChannel, SystemConfigKey

router = ResponseAPIRouter()

# WNS 未指定有效期时按一天发送，单位为秒。
_WNS_DEFAULT_TTL = 86400


def _message_viewer(token_data: _SchemaTokenPayload) -> MessageViewer:
    """
    把访问令牌载荷转换为消息读取主体。

    :param token_data: 已验证的访问令牌载荷
    :return: 用于确定可见范围的读取主体
    """
    super_user = bool(token_data.super_user)
    return MessageViewer(
        super_user=super_user,
        userid=str(token_data.sub or ""),
        visible_mtypes=None if super_user else get_user_notification_types(),
    )


class WebPushError(Protocol):
    """
    Web Push 订阅状态判断所需的最小异常协议。

    :ivar response: 推送服务的 HTTP 响应；失败前未收到响应时为 None
    """

    response: Any


def is_webpush_subscription_gone(error: WebPushError) -> bool:
    """
    判断 Web Push 订阅是否已在浏览器或推送服务侧失效。
    """
    response: Any = getattr(error, "response", None)
    status_code = getattr(response, "status_code", None) or getattr(
        response,
        "status",
        None,
    )
    return status_code in {404, 410}


def is_wns_endpoint(endpoint: str | None) -> bool:
    """
    判断是否为 Microsoft WNS 推送端点。
    """
    return bool(endpoint and "notify.windows.com" in endpoint)


def webpush_options_for_endpoint(endpoint: str | None) -> dict[str, Any]:
    """
    返回指定推送端点需要的 Web Push 附加参数（ttl 与请求头）。
    """
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


def start_message_chain(body: Any, form: Any, args: Any) -> None:
    """
    启动链式任务
    """
    MessageChain().process(body=body, form=form, args=args)


@router.post("/", summary="接收用户消息", response_model=_SchemaResponse[None])
async def user_message(
    task_registry: Annotated[TaskRegistry, Depends(get_background_task_registry)],
    request: Request,
    _: _SchemaTokenPayload = Depends(verify_apitoken),
) -> _SchemaResponse[None]:
    """
    用户消息响应；推荐通过 X-API-KEY 请求头传递 API_TOKEN，查询参数 token 仅保留兼容。
    """
    body = await request.body()
    form = await request.form()
    args = request.query_params
    source = args.get("source")
    content_type = request.headers.get("content-type") or ""
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
    current_user: ApiPrincipal = Depends(get_current_active_user),
) -> _SchemaResponse[None]:
    """
    接收并处理当前登录用户的 WEB 消息，按真实登录用户身份绑定权限。
    """
    images = None
    content_type = request.headers.get("content-type") or ""
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
        userid=str(current_user.id),
        username=current_user.name,
        text=text or "",
        images=images,
        is_channel_admin=bool(current_user.is_superuser),
    )
    return _SchemaResponse(success=True)


@router.get(
    "/web",
    summary="获取消息历史（兼容入口）",
    response_model=List[_SchemaMessageHistoryItem],
    deprecated=True,
)
@router.get(
    "/history",
    summary="获取消息历史",
    response_model=List[_SchemaMessageHistoryItem],
)
async def get_message_history(
    token_data: _SchemaTokenPayload = Depends(verify_token),
    service: MessageQueryService = Depends(get_message_query_service),
    page: PositivePageParam = 1,
    count: PositiveCountParam = 20,
) -> List[_SchemaMessageHistoryItem]:
    """
    分页获取消息历史记录，包含接收消息与发送通知。

    超级用户返回全部记录；其他用户只返回发送范围为全部用户的通知，以及本人产生的记录。

    旧路径 GET /api/v1/message/web 作为兼容入口保留相同行为；仅查询系统已发送通知时使用 GET /api/v1/message/notification。

    :param token_data: 已验证的访问令牌载荷，用于确定可见范围
    :param service: 消息历史查询服务
    :param page: 页码，从 1 开始，最小为 1；直接调用传入 None 时默认第 1 页
    :param count: 每页最多返回条数，最小为 1；直接调用传入 None 时默认 20 条
    :return: 按登记时间和主键倒序排列的消息历史列表
    """
    messages = await service.list_history(
        page=page if page is not None else 1,
        count=count if count is not None else 20,
        viewer=_message_viewer(token_data),
    )
    return [_SchemaMessageHistoryItem(**message) for message in messages]


@router.get("/notification", summary="获取通知消息", response_model=List[_SchemaMessageHistoryItem])
async def get_notification_message(
    token_data: _SchemaTokenPayload = Depends(verify_token),
    service: MessageQueryService = Depends(get_message_query_service),
    page: PositivePageParam = 1,
    count: PositiveCountParam = 20,
) -> List[_SchemaMessageHistoryItem]:
    """
    获取清理水位之后系统发送的通知消息列表，仅包含发送方向（action=1）记录。

    超级用户返回全部通知；其他用户只返回发送范围为全部用户的通知，以及本人产生的记录。

    :param token_data: 已验证的访问令牌载荷，用于确定可见范围
    :param service: 消息历史查询服务
    :param page: 页码，从 1 开始，最小为 1；直接调用传入 None 时默认第 1 页
    :param count: 每页最多返回条数，最小为 1；直接调用传入 None 时默认 20 条
    :return: 按登记时间和主键倒序排列的系统通知消息列表
    """
    clear_before = _get_notification_clear_before()
    messages = await service.list_notifications(
        page=page if page is not None else 1,
        count=count if count is not None else 20,
        all_clear_before=_format_notification_clear_time(clear_before.all),
        system_clear_before=_format_notification_clear_time(clear_before.system),
        media_clear_before=_format_notification_clear_time(clear_before.media),
        viewer=_message_viewer(token_data),
    )
    return [_SchemaMessageHistoryItem(**message) for message in messages]


@router.delete(
    "/notification",
    summary="清理通知消息",
    response_model=_SchemaResponse[_SchemaMessageClearData],
)
async def clear_notification_message(
    scope: _SchemaMessageClearScope = _SchemaMessageClearScope.All,
    _: Any = Depends(get_current_active_superuser),
) -> _SchemaResponse[dict[str, Any]]:
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
            and (config.config or {}).get("WECHAT_MODE", "app") != "bot"
            and (not source or config.name == source)
        ),
        None,
    )
    if not client_config:
        return "未找到对应的消息配置"
    config = client_config.config or {}
    try:
        wxcpt = WXBizMsgCrypt(
            sToken=config.get("WECHAT_TOKEN"),
            sEncodingAESKey=config.get("WECHAT_ENCODING_AESKEY"),
            sReceiveId=config.get("WECHAT_CORPID"),
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
        logger.error(f"微信请求验证失败: {str(err)}", exc_info=True)
        return "消息验证失败，请稍后重试"


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
    timestamp: Optional[Union[str, int]] = None,
    nonce: Optional[str] = None,
    source: Optional[str] = None,
    _: _SchemaTokenPayload = Depends(verify_apitoken),
) -> Any:
    """
    微信/VoceChat等验证响应
    """
    logger.info(
        f"收到验证请求: echostr={echostr}, "
        f"msg_signature={msg_signature}, timestamp={timestamp}, nonce={nonce}"
    )
    if echostr and msg_signature and timestamp and nonce:
        return wechat_verify(echostr, msg_signature, timestamp, nonce, source)
    return vocechat_verify()


@router.get(
    "/webpush/public-key",
    summary="获取客户端webpush公钥",
    response_model=_SchemaResponse[_SchemaWebPushPublicKeyData],
)
def get_webpush_public_key(
    _: _SchemaTokenPayload = Depends(verify_token),
) -> _SchemaResponse[_SchemaWebPushPublicKeyData]:
    """
    返回当前实例用于浏览器 Web Push 订阅的 VAPID 公钥
    """
    public_key = get_api_runtime_config_snapshot().vapid_public_key
    if not public_key:
        return _SchemaResponse(
            success=False,
            message="未配置有效的 WebPush VAPID 公钥",
        )
    return _SchemaResponse(
        success=True,
        data=_SchemaWebPushPublicKeyData(public_key=public_key),
    )


@router.post(
    "/webpush/subscribe",
    summary="客户端webpush通知订阅",
    response_model=_SchemaResponse[None],
)
async def subscribe(
    subscription: _SchemaSubscription, _: _SchemaTokenPayload = Depends(verify_token)
) -> _SchemaResponse[None]:
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
    _: ApiPrincipal = Depends(get_current_active_superuser),
) -> _SchemaResponse[None]:
    """
    发送webpush通知；会推送到全部已订阅浏览器，因此仅限超级管理员调用
    """
    from app.adapters.network.webpush import WebPushDeliveryError, send_webpush

    subscriptions = webpush_registry.list()
    if not subscriptions:
        return _SchemaResponse(success=True, message="没有可发送的浏览器通知")

    api_config = get_api_runtime_config_snapshot()
    success_count = 0
    failure_count = 0
    for sub in subscriptions:
        try:
            send_webpush(
                subscription=sub,
                data=json.dumps(payload.model_dump()),
                vapid_private_key=api_config.vapid_private_key,
                vapid_subject=api_config.vapid_subject,
                **webpush_options_for_endpoint(sub.get("endpoint")),
            )
            success_count += 1
        except WebPushDeliveryError as err:
            logger.error(f"WebPush发送失败: {str(err)}")
            if is_webpush_subscription_gone(err) and webpush_registry.remove(sub):
                logger.info(f"已移除失效WebPush订阅: {sub.get('endpoint')}")
            failure_count += 1
        except Exception as err:
            logger.error(f"WebPush发送失败: {str(err)}", exc_info=True)
            failure_count += 1

    if not failure_count:
        return _SchemaResponse(
            success=True,
            message=f"消息已发送到 {success_count} 个设备",
        )
    if success_count:
        return _SchemaResponse(
            success=True,
            message=(
                f"消息已发送到 {success_count} 个设备，"
                f"{failure_count} 个设备发送失败"
            ),
        )
    return _SchemaResponse(
        success=False,
        message="消息发送失败，请检查浏览器通知权限后重试",
    )
