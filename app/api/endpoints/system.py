import json
import json
import time
from typing import Any, List, Union

from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import StreamingResponse

from app import schemas
from app.core.security import verify_token
from app.db.systemconfig_oper import SystemConfigOper
from app.helper.message import MessageHelper
from app.helper.progress import ProgressHelper

router = APIRouter()


@router.get("/progress/{process_type}", summary="实时进度")
def get_progress(process_type: str, token: str):
    """
    实时获取处理进度，返回格式为SSE
    """
    if not token or not verify_token(token):
        raise HTTPException(
            status_code=403,
            detail="认证失败！",
        )

    progress = ProgressHelper()

    def event_generator():
        while True:
            detail = progress.get(process_type)
            yield 'data: %s\n\n' % json.dumps(detail)
            time.sleep(0.2)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.get("/setting/{key}", summary="查询系统设置", response_model=schemas.Response)
def get_setting(key: str, _: schemas.TokenPayload = Depends(verify_token)):
    """
    查询系统设置
    """
    return schemas.Response(success=True, data={
        "value": SystemConfigOper().get(key)
    })


@router.post("/setting/{key}", summary="更新系统设置", response_model=schemas.Response)
def set_setting(key: str, value: Union[list, dict, str, int],
                _: schemas.TokenPayload = Depends(verify_token)):
    """
    更新系统设置
    """
    SystemConfigOper().set(key, value)
    return schemas.Response(success=True)


@router.get("/message", summary="实时消息")
def get_progress(token: str):
    """
    实时获取系统消息，返回格式为SSE
    """
    if not token or not verify_token(token):
        raise HTTPException(
            status_code=403,
            detail="认证失败！",
        )

    message = MessageHelper()

    def event_generator():
        while True:
            detail = message.get()
            yield 'data: %s\n\n' % (detail or '')
            time.sleep(3)

    return StreamingResponse(event_generator(), media_type="text/event-stream")
