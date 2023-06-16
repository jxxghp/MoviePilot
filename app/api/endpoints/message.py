from typing import Union, Any

from fastapi import APIRouter, BackgroundTasks
from fastapi import Request
from starlette.responses import PlainTextResponse

from app import schemas
from app.chain.user_message import UserMessageChain
from app.core.config import settings
from app.log import logger
from app.modules.wechat.WXBizMsgCrypt3 import WXBizMsgCrypt

router = APIRouter()


def start_message_chain(body: Any, form: Any, args: Any):
    """
    启动链式任务
    """
    UserMessageChain().process(body=body, form=form, args=args)


@router.post("/", response_model=schemas.Response)
async def user_message(background_tasks: BackgroundTasks, request: Request):
    """
    用户消息响应
    """
    body = await request.body()
    form = await request.form()
    args = request.query_params
    background_tasks.add_task(start_message_chain, body, form, args)
    return {"success": True}


@router.get("/")
async def wechat_verify(echostr: str, msg_signature: str, timestamp: Union[str, int], nonce: str):
    """
    用户消息响应
    """
    logger.info(f"收到微信验证请求: {echostr}")
    try:
        wxcpt = WXBizMsgCrypt(sToken=settings.WECHAT_TOKEN,
                              sEncodingAESKey=settings.WECHAT_ENCODING_AESKEY,
                              sReceiveId=settings.WECHAT_CORPID)
    except Exception as err:
        logger.error(f"微信请求验证失败: {err}")
        return str(err)
    ret, sEchoStr = wxcpt.VerifyURL(sMsgSignature=msg_signature,
                                    sTimeStamp=timestamp,
                                    sNonce=nonce,
                                    sEchoStr=echostr)
    if ret != 0:
        logger.error("微信请求验证失败 VerifyURL ret: %s" % str(ret))
    # 验证URL成功，将sEchoStr返回给企业号
    return PlainTextResponse(sEchoStr)
