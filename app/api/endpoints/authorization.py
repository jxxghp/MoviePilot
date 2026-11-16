"""
系统管理员管理渠道申请、成员权限和启用状态的端点。
"""

from dataclasses import asdict
from typing import Any

from fastapi import Depends, HTTPException

from app.api.dependencies.auth import get_current_active_superuser, get_current_active_user
from app.api.response import ResponseAPIRouter
from app.application.notification import get_notification_configs
from app.application.security.authorization import ChannelAuthorizationService, ChannelMember, get_channel_authorization
from app.domain.authorization import LEGACY_CHANNELS
from app.schemas.authorization import (
    AuthorizationChannel as _SchemaAuthorizationChannel,
)
from app.schemas.authorization import (
    ChannelAuthorizationData as _SchemaAuthorizationData,
)
from app.schemas.authorization import (
    ChannelBindingData as _SchemaBindingData,
)
from app.schemas.authorization import (
    ChannelMember as _SchemaChannelMember,
)
from app.schemas.authorization import (
    ChannelMemberCreate as _SchemaMemberCreate,
)
from app.schemas.authorization import (
    ChannelMemberUpdate as _SchemaMemberUpdate,
)
from app.schemas.message import Message
from app.schemas.response import Response as _SchemaResponse
from app.schemas.types import NotificationChannel

router = ResponseAPIRouter()


@router.post("/binding", summary="生成当前账号的渠道关联码", response_model=_SchemaResponse[_SchemaBindingData])
def create_binding(user: Any = Depends(get_current_active_user)) -> _SchemaResponse[_SchemaBindingData]:
    """
    当前网页账号生成五分钟一次性码，需在本人渠道私聊中提交以验证双方身份。
    """
    if getattr(user, "authorization_id", None) or getattr(user, "account_type", "local") != "local":
        raise HTTPException(status_code=403, detail="请登录 MoviePilot 网页账号生成关联码")
    code, expires_at = _service().create_binding(user.id)
    return _SchemaResponse(success=True, data=_SchemaBindingData(code=code, expires_at=expires_at))


def _service() -> ChannelAuthorizationService:
    """
    读取已装配授权服务，未就绪时按服务不可用返回。
    """
    service = get_channel_authorization()
    if service is None:
        raise HTTPException(status_code=503, detail="渠道授权服务尚未就绪")
    return service


def _administrator(user: Any = Depends(get_current_active_superuser)) -> Any:
    """
    仅接受网页登录系统管理员，渠道管理员不能修改公共授权。
    """
    if getattr(user, "authorization_id", None) or getattr(user, "account_type", "local") != "local":
        raise HTTPException(status_code=403, detail="请使用 MoviePilot 系统管理员账号管理渠道授权")
    return user


def _project(member: ChannelMember) -> _SchemaChannelMember:
    """
    只投影管理界面需要的信息，隐藏内部用户主键和不可变凭据身份。
    """
    config = next(
        (item for item in get_notification_configs(include_disabled=True) if item.id == member.notification_id), None
    )
    values = asdict(member)
    values["source"] = config.name if config else "渠道已删除"
    values["channel_enabled"] = bool(
        config and config.enabled and LEGACY_CHANNELS.get(config.type or "", (None,))[0] == member.channel
    )
    values["linked_username"] = _service().linked_username(member)
    return _SchemaChannelMember.model_validate(values)


@router.get("/", summary="查询渠道成员与待审批申请", response_model=_SchemaResponse[_SchemaAuthorizationData])
def list_authorizations(_: Any = Depends(_administrator)) -> _SchemaResponse[_SchemaAuthorizationData]:
    """
    返回所有授权记录及脱敏后的渠道选择项。
    """
    channels = [
        _SchemaAuthorizationChannel(
            id=item.id, name=item.name or "", channel=LEGACY_CHANNELS[item.type][0], enabled=bool(item.enabled)
        )
        for item in get_notification_configs(include_disabled=True)
        if item.type in LEGACY_CHANNELS
    ]
    return _SchemaResponse(
        success=True,
        data=_SchemaAuthorizationData(
            members=[_project(member) for member in _service().list_members()], channels=channels
        ),
    )


def _notify(member: ChannelMember) -> None:
    """
    在授权事务提交后向原渠道私发状态回执，不重放先前业务操作。
    """
    delivery = _service().delivery(member.id)
    if delivery is None:
        return
    labels = {
        "approved": "已授权，请重新发送要执行的操作。",
        "rejected": "授权申请未通过，请联系管理员。",
        "disabled": "渠道授权已停用，请联系管理员。",
    }
    from app.chain.message import MessageChain

    MessageChain().post_message(
        Message(
            channel=NotificationChannel(delivery[0]),
            source=delivery[1],
            userid=delivery[2],
            title=labels[member.status],
            save_history=False,
            private_delivery=True,
        )
    )


@router.put("/{member_id}", summary="审批或修改渠道授权", response_model=_SchemaResponse[_SchemaChannelMember])
def update_authorization(
    member_id: str, payload: _SchemaMemberUpdate, user: Any = Depends(_administrator)
) -> _SchemaResponse[_SchemaChannelMember]:
    """
    由系统管理员批准、拒绝、停用或恢复渠道成员，提交后向原渠道回执。
    """
    try:
        member = _service().update(
            member_id, status=payload.status, role=payload.role, scopes=tuple(payload.scopes), reviewer_id=user.id
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    _notify(member)
    return _SchemaResponse(success=True, data=_project(member))


@router.post("/", summary="直接授权指定渠道成员", response_model=_SchemaResponse[_SchemaChannelMember])
def create_authorization(
    payload: _SchemaMemberCreate, user: Any = Depends(_administrator)
) -> _SchemaResponse[_SchemaChannelMember]:
    """
    为已核对的平台用户创建记录并设置权限，不要求成员先申请。
    """
    config = next(
        (
            item
            for item in get_notification_configs()
            if item.id == payload.notification_id and item.type in LEGACY_CHANNELS
        ),
        None,
    )
    if config is None:
        raise HTTPException(status_code=400, detail="目标渠道不存在或尚未启用")
    service = _service()
    member = service.request(
        LEGACY_CHANNELS[config.type or ""][0],
        config.name or "",
        payload.sender_id.strip(),
        payload.display_name.strip(),
    )
    member = service.update(
        member.id, status=payload.status, role=payload.role, scopes=tuple(payload.scopes), reviewer_id=user.id
    )
    _notify(member)
    return _SchemaResponse(success=True, data=_project(member))
