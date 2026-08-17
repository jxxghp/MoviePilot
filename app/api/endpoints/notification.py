from typing import Any, Dict

from fastapi import Depends

from app.schemas.common import ManageRequest as _SchemaManageRequest
from app.schemas.response import Response as _SchemaResponse
from app.api.response import ResponseAPIRouter
from app.chain.notification import NotificationChain
from app.db.models import User
from app.api.deps import get_current_active_superuser

router = ResponseAPIRouter()


@router.post(
    "/manage",
    summary="通知渠道统一管理",
    response_model=_SchemaResponse[Dict[str, Any]],
)
def manage_channel(
    request: _SchemaManageRequest,
    _: User = Depends(get_current_active_superuser),
):
    """
    通知渠道统一管理入口

    端点层不定义任何渠道特定的名称与参数，
    渠道标识、管理动作与表单参数由前端上送并原样透传给渠道模块
    """
    result = NotificationChain().manage_channel(
        channel=request.target,
        action=request.action,
        **request.params,
    )
    return _SchemaResponse(
        success=bool(result.get("success")),
        message=result.get("message"),
        data=result.get("data"),
    )
