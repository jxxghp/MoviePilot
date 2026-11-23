"""
系统管理员管理渠道申请、成员权限和启用状态的端点。
"""

from dataclasses import asdict
from typing import Any

from fastapi import Depends, HTTPException, Security

from app.adapters.web.security.access import oauth2_scheme_manual_error
from app.api.dependencies.auth import get_current_active_superuser, get_current_active_user
from app.api.response import ResponseAPIRouter
from app.application.messaging.channel.catalog import interaction_channel
from app.application.notification import get_notification_configs
from app.application.security.authorization import (
    ChannelAuthorizationConflict,
    ChannelAuthorizationService,
    ChannelMember,
    get_channel_authorization,
)
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
    ChannelMemberBatch as _SchemaMemberBatch,
)
from app.schemas.authorization import (
    ChannelMemberBatchResult as _SchemaBatchResult,
)
from app.schemas.authorization import (
    ChannelMemberCreate as _SchemaMemberCreate,
)
from app.schemas.authorization import (
    ChannelMemberDelete as _SchemaMemberDelete,
)
from app.schemas.authorization import (
    ChannelMemberUpdate as _SchemaMemberUpdate,
)
from app.schemas.response import Response as _SchemaResponse

router = ResponseAPIRouter()
_RECEIPT_STATUS = "授权已保存。若原渠道仍可用，后台会私发回执。"


@router.post("/binding", summary="生成当前账号的渠道关联码", response_model=_SchemaResponse[_SchemaBindingData])
def create_binding(
    user: Any = Depends(get_current_active_user),
    bearer: str | None = Security(oauth2_scheme_manual_error),
) -> _SchemaResponse[_SchemaBindingData]:
    """
    当前网页账号生成五分钟一次性码，需在本人渠道私聊中提交以验证双方身份。
    """
    if not bearer or getattr(user, "authorization_id", None) or getattr(user, "account_type", "") != "local":
        raise HTTPException(status_code=403, detail="请登录 MoviePilot 网页账号生成关联码")
    try:
        code, expires_at = _service().create_binding(user.id)
    except (PermissionError, ValueError) as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    return _SchemaResponse(success=True, data=_SchemaBindingData(code=code, expires_at=expires_at))


def _service() -> ChannelAuthorizationService:
    """
    读取已装配授权服务，未就绪时按服务不可用返回。
    """
    service = get_channel_authorization()
    if service is None:
        raise HTTPException(status_code=503, detail="渠道授权服务尚未就绪")
    return service


def _administrator(
    user: Any = Depends(get_current_active_superuser),
    bearer: str | None = Security(oauth2_scheme_manual_error),
) -> Any:
    """
    仅接受网页登录系统管理员，渠道管理员不能修改公共授权。
    """
    if not bearer or getattr(user, "authorization_id", None) or getattr(user, "account_type", "") != "local":
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
        config and config.enabled and config.interaction_enabled is not False
        and interaction_channel(config.type) == member.channel
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
            id=item.id, name=item.name or "", channel=channel,
            enabled=bool(item.enabled) and item.interaction_enabled is not False
        )
        for item in get_notification_configs(include_disabled=True)
        if (channel := interaction_channel(item.type)) is not None
    ]
    return _SchemaResponse(
        success=True,
        data=_SchemaAuthorizationData(
            members=[_project(member) for member in _service().list_members()], channels=channels
        ),
    )


@router.post("/batch", summary="批量管理明确选中的渠道授权", response_model=_SchemaResponse[_SchemaBatchResult])
def batch_authorizations(
    payload: _SchemaMemberBatch, user: Any = Depends(_administrator),
) -> _SchemaResponse[_SchemaBatchResult]:
    """
    在单一事务中批量通过、拒绝、停用或删除，任一版本冲突均不提交。
    """
    try:
        changed = _service().manage_members(
            payload.action, tuple((item.id, item.expected_updated_at) for item in payload.members),
            reviewer_id=user.id, scopes=tuple(payload.scopes), confirm_approved=payload.confirm_approved,
        )
    except ChannelAuthorizationConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    deleted = payload.action == "delete"
    return _SchemaResponse(success=True, data=_SchemaBatchResult(
        members=[] if deleted else [_project(member) for member in changed],
        deleted_ids=[member.id for member in changed] if deleted else [],
    ))


@router.delete("/{member_id}", summary="删除渠道申请或授权记录", response_model=_SchemaResponse[None])
def delete_authorization(
    member_id: str, payload: _SchemaMemberDelete, user: Any = Depends(_administrator),
) -> _SchemaResponse[None]:
    """
    删除当前版本，已授权成员须二次确认，业务历史不随授权记录删除。
    """
    try:
        _service().manage_members(
            "delete", ((member_id, payload.expected_updated_at),),
            reviewer_id=user.id, confirm_approved=payload.confirm_approved,
        )
    except ChannelAuthorizationConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return _SchemaResponse(success=True)


@router.put("/{member_id}", summary="审批或修改渠道授权", response_model=_SchemaResponse[_SchemaChannelMember])
def update_authorization(
    member_id: str, payload: _SchemaMemberUpdate, user: Any = Depends(_administrator)
) -> _SchemaResponse[_SchemaChannelMember]:
    """
    由系统管理员批准、拒绝、停用或恢复渠道成员，提交后向原渠道回执。
    """
    try:
        member = _service().update(
            member_id, status=payload.status, role=payload.role, scopes=tuple(payload.scopes),
            reviewer_id=user.id, queue_receipt=True,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return _SchemaResponse(success=True, message=_RECEIPT_STATUS, data=_project(member))


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
            if item.id == payload.notification_id and item.interaction_enabled is not False
        ),
        None,
    )
    channel = interaction_channel(config.type) if config else None
    if config is None or channel is None:
        raise HTTPException(status_code=400, detail="目标渠道不存在或尚未启用")
    service = _service()
    try:
        member = service.request(
            channel,
            config.name or "",
            payload.sender_id.strip(),
            payload.display_name.strip(),
        )
    except (PermissionError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    try:
        member = service.update(
            member.id, status=payload.status, role=payload.role, scopes=tuple(payload.scopes),
            reviewer_id=user.id, queue_receipt=True,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return _SchemaResponse(success=True, message=_RECEIPT_STATUS, data=_project(member))
