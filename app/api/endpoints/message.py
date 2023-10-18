from typing import Union, Any, List

from fastapi import APIRouter, BackgroundTasks, Depends
from fastapi import Request
from starlette.responses import PlainTextResponse

from app import schemas
from app.chain.message import MessageChain
from app.core.config import settings
from app.core.security import verify_token
from app.db.systemconfig_oper import SystemConfigOper
from app.log import logger
from app.modules.wechat.WXBizMsgCrypt3 import WXBizMsgCrypt
from app.schemas import NotificationSwitch
from app.schemas.types import SystemConfigKey, NotificationType

router = APIRouter()


def start_message_chain(body: Any, form: Any, args: Any):
    """
    启动链式任务
    """
    MessageChain().process(body=body, form=form, args=args)


@router.post("/", summary="接收用户消息", response_model=schemas.Response)
async def user_message(background_tasks: BackgroundTasks, request: Request):
    """
    用户消息响应
    """
    body = await request.body()
    form = await request.form()
    args = request.query_params
    background_tasks.add_task(start_message_chain, body, form, args)
    return schemas.Response(success=True)


@router.get("/", summary="微信验证")
def wechat_verify(echostr: str, msg_signature: str,
                  timestamp: Union[str, int], nonce: str) -> Any:
    """
    用户消息响应
    """
    logger.info(f"收到微信验证请求: {echostr}")
    try:
        wxcpt = WXBizMsgCrypt(sToken=settings.WECHAT_TOKEN,
                              sEncodingAESKey=settings.WECHAT_ENCODING_AESKEY,
                              sReceiveId=settings.WECHAT_CORPID)
    except Exception as err:
        logger.error(f"微信请求验证失败: {str(err)}")
        return str(err)
    ret, sEchoStr = wxcpt.VerifyURL(sMsgSignature=msg_signature,
                                    sTimeStamp=timestamp,
                                    sNonce=nonce,
                                    sEchoStr=echostr)
    if ret != 0:
        logger.error("微信请求验证失败 VerifyURL ret: %s" % str(ret))
    # 验证URL成功，将sEchoStr返回给企业号
    return PlainTextResponse(sEchoStr)


@router.get("/switchs", summary="查询通知消息渠道开关", response_model=List[NotificationSwitch])
def read_switchs(_: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询通知消息渠道开关
    """
    return_list = []
    # 读取数据库
    switchs = SystemConfigOper().get(SystemConfigKey.NotificationChannels)
    if not switchs:
        for noti in NotificationType:
            return_list.append(NotificationSwitch(mtype=noti.value, wechat=True,
                                                  telegram=True, slack=True,
                                                  synologychat=True))
    else:
        for switch in switchs:
            return_list.append(NotificationSwitch(**switch))
    return return_list


@router.post("/switchs", summary="设置通知消息渠道开关", response_model=schemas.Response)
def set_switchs(switchs: List[NotificationSwitch],
                _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询通知消息渠道开关
    """
    switch_list = []
    for switch in switchs:
        switch_list.append(switch.dict())
    # 存入数据库
    SystemConfigOper().set(SystemConfigKey.NotificationChannels, switch_list)

    return schemas.Response(success=True)
