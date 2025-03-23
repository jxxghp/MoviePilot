import json
from typing import Union, Any, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from pywebpush import WebPushException, webpush
from sqlalchemy.orm import Session
from starlette.responses import PlainTextResponse

from app import schemas
from app.chain.message import MessageChain
from app.core.config import settings, global_vars
from app.core.security import verify_token, verify_apitoken
from app.db import get_db
from app.db.models import User
from app.db.models.message import Message
from app.db.user_oper import get_current_active_superuser
from app.helper.service import ServiceConfigHelper
from app.log import logger
from app.modules.wechat.WXBizMsgCrypt3 import WXBizMsgCrypt
from app.schemas.types import MessageChannel

router = APIRouter()


def start_message_chain(body: Any, form: Any, args: Any):
    """
    启动链式任务
    """
    MessageChain().process(body=body, form=form, args=args)


@router.post("/", summary="接收用户消息", response_model=schemas.Response)
async def user_message(background_tasks: BackgroundTasks, request: Request,
                       _: schemas.TokenPayload = Depends(verify_apitoken)):
    """
    用户消息响应，配置请求中需要添加参数：token=API_TOKEN&source=消息配置名
    """
    body = await request.body()
    form = await request.form()
    args = request.query_params
    background_tasks.add_task(start_message_chain, body, form, args)
    return schemas.Response(success=True)


@router.post("/web", summary="接收WEB消息", response_model=schemas.Response)
def web_message(text: str, current_user: User = Depends(get_current_active_superuser)):
    """
    WEB消息响应
    """
    MessageChain().handle_message(
        channel=MessageChannel.Web,
        source=current_user.name,
        userid=current_user.name,
        username=current_user.name,
        text=text
    )
    return schemas.Response(success=True)


@router.get("/web", summary="获取WEB消息", response_model=List[dict])
def get_web_message(_: schemas.TokenPayload = Depends(verify_token),
                    db: Session = Depends(get_db),
                    page: Optional[int] =  1,
                    count: Optional[int] =  20):
    """
    获取WEB消息列表
    """
    ret_messages = []
    messages = Message.list_by_page(db, page=page, count=count)
    for message in messages:
        try:
            ret_messages.append(message.to_dict())
        except Exception as e:
            logger.error(f"获取WEB消息列表失败: {str(e)}")
            continue
    return ret_messages


def wechat_verify(echostr: str, msg_signature: str, timestamp: Union[str, int], nonce: str,
                  source: Optional[str] =  None) -> Any:
    """
    微信验证响应
    """
    # 获取服务配置
    client_configs = ServiceConfigHelper.get_notification_configs()
    if not client_configs:
        return "未找到对应的消息配置"
    client_config = next((config for config in client_configs if
                          config.type == "wechat" and config.enabled and (not source or config.name == source)), None)
    if not client_config:
        return "未找到对应的消息配置"
    try:
        wxcpt = WXBizMsgCrypt(sToken=client_config.config.get('WECHAT_TOKEN'),
                              sEncodingAESKey=client_config.config.get('WECHAT_ENCODING_AESKEY'),
                              sReceiveId=client_config.config.get('WECHAT_CORPID'))
        ret, sEchoStr = wxcpt.VerifyURL(sMsgSignature=msg_signature,
                                        sTimeStamp=timestamp,
                                        sNonce=nonce,
                                        sEchoStr=echostr)
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


@router.get("/", summary="回调请求验证")
def incoming_verify(token: Optional[str] =  None, echostr: Optional[str] =  None, msg_signature: Optional[str] =  None,
                    timestamp: Union[str, int] = None, nonce: Optional[str] =  None, source: Optional[str] =  None,
                    _: schemas.TokenPayload = Depends(verify_apitoken)) -> Any:
    """
    微信/VoceChat等验证响应
    """
    logger.info(f"收到验证请求: token={token}, echostr={echostr}, "
                f"msg_signature={msg_signature}, timestamp={timestamp}, nonce={nonce}")
    if echostr and msg_signature and timestamp and nonce:
        return wechat_verify(echostr, msg_signature, timestamp, nonce, source)
    return vocechat_verify()


@router.post("/webpush/subscribe", summary="客户端webpush通知订阅", response_model=schemas.Response)
def subscribe(subscription: schemas.Subscription, _: schemas.TokenPayload = Depends(verify_token)):
    """
    客户端webpush通知订阅
    """
    subinfo = subscription.dict()
    if subinfo not in global_vars.get_subscriptions():
        global_vars.push_subscription(subinfo)
    logger.debug(f"通知订阅成功: {subinfo}")
    return schemas.Response(success=True)


@router.post("/webpush/send", summary="发送webpush通知", response_model=schemas.Response)
def send_notification(payload: schemas.SubscriptionMessage, _: schemas.TokenPayload = Depends(verify_token)):
    """
    发送webpush通知
    """
    for sub in global_vars.get_subscriptions():
        try:
            webpush(
                subscription_info=sub,
                data=json.dumps(payload.dict()),
                vapid_private_key=settings.VAPID.get("privateKey"),
                vapid_claims={
                    "sub": settings.VAPID.get("subject")
                },
            )
        except WebPushException as err:
            logger.error(f"WebPush发送失败: {str(err)}")
            continue
    return schemas.Response(success=True)
