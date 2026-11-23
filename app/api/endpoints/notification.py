"""
提供通知渠道配置查询、管理和测试接口。
"""

from collections.abc import Awaitable
from typing import Any, Dict, TypeVar

from fastapi import Body, Depends, HTTPException
from pydantic import ValidationError

from app.api.dependencies.auth import (
    get_current_active_superuser,
    get_current_active_superuser_async,
    get_current_active_user,
    get_user_service,
)
from app.api.principal import ApiPrincipal
from app.api.response import CompatibleCountParam, CompatiblePageParam, ResponseAPIRouter
from app.application.configuration import get_configured_system_config
from app.application.messaging.channel.settings import (
    ChannelSettingsConflict,
    ChannelSettingsService,
    CommittedChannelSettingsError,
)
from app.application.security.authorization import get_channel_authorization
from app.application.security.user import UserService
from app.chain.notification import NotificationChain
from app.schemas.channel import (
    ChannelCardOrder,
    ChannelConnectionCreate,
    ChannelConnectionDelete,
    ChannelConnectionEdit,
    ChannelConnectionForm,
    ChannelConnectionInfo,
    ChannelConnectionReferences,
    ChannelCredentialRotation,
    ChannelFeature,
    ChannelSettingsView,
    InteractionPolicy,
    InteractionPolicyEdit,
    NotificationRoute,
    NotificationRouteEdit,
    NotificationRuleAccount,
)
from app.schemas.common import ManageRequest as _SchemaManageRequest
from app.schemas.push import AppPushInstanceRequest, AppPushRequest
from app.schemas.response import Response as _SchemaResponse
from app.schemas.system import NotificationConf

router = ResponseAPIRouter()


_Resource = TypeVar("_Resource")


def _channel_settings() -> ChannelSettingsService:
    """
    使用已装配的系统配置事务。
    """
    return ChannelSettingsService(get_configured_system_config())


async def _save_channel_resource(operation: Awaitable[_Resource]) -> _SchemaResponse[_Resource]:
    """
    把资源缺失与版本冲突映射到明确的写入结果。
    """
    try:
        return _SchemaResponse(success=True, data=await operation)
    except CommittedChannelSettingsError as error:
        return _SchemaResponse(success=False, message=str(error), data=error.data)
    except ChannelSettingsConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except KeyError as error:
        raise HTTPException(status_code=404, detail="渠道资源不存在或当前平台不支持此功能") from error
    except ValueError as error:
        detail = "渠道配置无效，请检查名称、类型和配置"
        if isinstance(error, ValidationError):
            kinds = {item["type"] for item in error.errors(include_input=False)}
            if "channel_connection_name_conflict" in kinds:
                detail = "连接名称已存在，请使用其他名称"
        raise HTTPException(status_code=422, detail=detail) from error


@router.get("/channels", summary="读取通知与机器人交互设置", response_model=_SchemaResponse[ChannelSettingsView])
def list_channel_settings(
    _: ApiPrincipal = Depends(get_current_active_superuser),
) -> _SchemaResponse[ChannelSettingsView]:
    """
    向管理员返回连接、通知管理和交互管理的版本化配置。
    """
    try:
        view = _channel_settings().read()
    except ValueError as error:
        raise HTTPException(status_code=422, detail="现有渠道配置无法导入，请检查名称、类型和稳定 ID") from error
    return _SchemaResponse(success=True, data=view)


@router.get(
    "/channels/notification-accounts",
    summary="读取投递规则可选账号",
    response_model=_SchemaResponse[list[NotificationRuleAccount]],
)
async def list_notification_rule_accounts(
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
    service: UserService = Depends(get_user_service),
    page: CompatiblePageParam = None,
    count: CompatibleCountParam = None,
) -> _SchemaResponse[list[NotificationRuleAccount]]:
    """
    返回管理员可选择的本地账号及其不可变通知身份，由统一响应路由提供可选分页。
    """
    accounts = [
        NotificationRuleAccount(name=user.name, identity=user.notification_identity)
        for user in await service.list()
        if user.account_type == "local" and user.is_active and user.notification_identity
    ]
    return _SchemaResponse(success=True, data=accounts)


@router.post("/channels/connections", summary="创建共享渠道连接", response_model=_SchemaResponse[ChannelConnectionInfo])
async def create_channel_connection(
    request: ChannelConnectionCreate,
    _: ApiPrincipal = Depends(get_current_active_superuser),
) -> _SchemaResponse[ChannelConnectionInfo]:
    """
    以新的独立身份存储共享连接，读取时只返回摘要。
    """
    return await _save_channel_resource(_channel_settings().create_connection(request))


@router.get("/channels/{connection_id}/connection", summary="读取连接表单", response_model=_SchemaResponse[ChannelConnectionForm])
def read_channel_connection_form(
    connection_id: str,
    _: ApiPrincipal = Depends(get_current_active_superuser),
) -> _SchemaResponse[ChannelConnectionForm]:
    """
    读取已保存的非敏感配置，锁定连接的身份字段。
    """
    try:
        form = _channel_settings().read_connection_form(connection_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="渠道连接不存在") from error
    return _SchemaResponse(success=True, data=form)


@router.put("/channels/order/{feature}", summary="保存渠道卡片顺序", response_model=_SchemaResponse[tuple[str, ...]])
async def reorder_channel_cards(
    feature: ChannelFeature, request: ChannelCardOrder,
    _: ApiPrincipal = Depends(get_current_active_superuser),
) -> _SchemaResponse[tuple[str, ...]]:
    """
    独立保存当前设置页上的卡片顺序。
    """
    return await _save_channel_resource(_channel_settings().reorder(feature, request))


@router.put("/channels/{connection_id}/connection", summary="更新共享连接", response_model=_SchemaResponse[ChannelConnectionInfo])
async def edit_channel_connection(
    connection_id: str,
    request: ChannelConnectionEdit,
    _: ApiPrincipal = Depends(get_current_active_superuser),
) -> _SchemaResponse[ChannelConnectionInfo]:
    """
    更新连接名称和总开关。
    """
    return await _save_channel_resource(_channel_settings().edit_connection(connection_id, request))


@router.put("/channels/{connection_id}/credential", summary="核验并轮换连接凭据", response_model=_SchemaResponse[ChannelConnectionInfo])
async def rotate_channel_credential(
    connection_id: str,
    request: ChannelCredentialRotation,
    _: ApiPrincipal = Depends(get_current_active_superuser),
) -> _SchemaResponse[ChannelConnectionInfo]:
    """
    验证新凭据所属账号，在同一连接内保留原授权和目标。
    """
    result = await _save_channel_resource(_channel_settings().rotate_credential(connection_id, request))
    if result.success and result.data and result.data.credential_status == "verification_required":
        return _SchemaResponse(
            success=False, message="平台身份待核验，连接已暂停。请刷新设置后重新核验。", data=result.data,
        )
    return result


@router.get("/channels/{connection_id}/references", summary="读取连接关联的成员与任务数量",
            response_model=_SchemaResponse[ChannelConnectionReferences])
def read_channel_connection_references(
    connection_id: str,
    _: ApiPrincipal = Depends(get_current_active_superuser),
) -> _SchemaResponse[ChannelConnectionReferences]:
    """
    为管理员确认移除提供保留的成员及任务记录数量。
    """
    if not any(item.id == connection_id for item in _channel_settings().read().connections):
        raise HTTPException(status_code=404, detail="连接不存在或已移除")
    authorization = get_channel_authorization()
    if authorization is None:
        raise HTTPException(status_code=503, detail="暂时无法读取连接引用")
    members, tasks = authorization.connection_reference_counts(connection_id)
    return _SchemaResponse(success=True, data=ChannelConnectionReferences(member_count=members, task_count=tasks))


@router.delete("/channels/{connection_id}/connection", summary="删除共享连接", response_model=_SchemaResponse[ChannelConnectionInfo])
async def delete_channel_connection(
    connection_id: str,
    request: ChannelConnectionDelete,
    _: ApiPrincipal = Depends(get_current_active_superuser),
) -> _SchemaResponse[ChannelConnectionInfo]:
    """
    核对用户确认的资源版本，终止该连接的收发并保留历史引用。
    """
    return await _save_channel_resource(_channel_settings().delete_connection(connection_id, request))


@router.put("/channels/{connection_id}/notification", summary="更新通知投递设置", response_model=_SchemaResponse[NotificationRoute])
async def edit_notification_route(
    connection_id: str,
    request: NotificationRouteEdit,
    _: ApiPrincipal = Depends(get_current_active_superuser),
) -> _SchemaResponse[NotificationRoute]:
    """
    仅保存指定连接的事件投递设置。
    """
    return await _save_channel_resource(_channel_settings().edit_notification(connection_id, request))


@router.put("/channels/{connection_id}/interaction", summary="更新机器人交互设置", response_model=_SchemaResponse[InteractionPolicy])
async def edit_channel_interaction(
    connection_id: str,
    request: InteractionPolicyEdit,
    _: ApiPrincipal = Depends(get_current_active_superuser),
) -> _SchemaResponse[InteractionPolicy]:
    """
    保存指定连接的机器人交互配置，接入方式须由连接能力支持。
    """
    return await _save_channel_resource(_channel_settings().edit_interaction(connection_id, request))


@router.post("/app/instance", summary="轮换 App 通知实例凭据", response_model=_SchemaResponse[Dict[str, Any]])
def manage_app_instance(request: AppPushInstanceRequest,
                        current_user: ApiPrincipal = Depends(get_current_active_superuser)) -> _SchemaResponse[object]:
    """
    仅容器管理员能轮换整个实例的发送凭据，响应只包含结果，不下发密钥。
    """
    result = NotificationChain().manage_channel(channel="AppPush", action="rotate_instance", source=request.source,
                                               owner=current_user.notification_identity, administrative=True)
    return _SchemaResponse(success=bool(result.get("success")), message=result.get("message") or "", data=result.get("data"))


@router.post("/app", summary="管理当前用户的 App 通知", response_model=_SchemaResponse[Dict[str, Any]])
def manage_app_notification(
    request: AppPushRequest,
    current_user: ApiPrincipal = Depends(get_current_active_user),
) -> _SchemaResponse[object]:
    """
    复用现有用户认证并强制注入账号通知身份，客户端不能操作其他用户设备。
    """
    if not current_user.notification_identity:
        return _SchemaResponse(success=False, message="请完成数据库升级后重新登录")
    params = request.model_dump(exclude={"action"})
    result = NotificationChain().manage_channel(channel="AppPush", action=request.action,
                                               owner=current_user.notification_identity, **params)
    return _SchemaResponse(success=bool(result.get("success")), message=result.get("message") or "", data=result.get("data"))


def _normalize_configs(value: list[NotificationConf], *, validate_identities: bool = True) -> list[dict[str, Any]]:
    """
    规整通知配置身份和名称，拒绝会导致运行目录覆盖的输入。

    :param value: 待规整的通知配置集合
    :param validate_identities: 新保存值校验稳定身份；旧值只用于缓存对账，允许读取以修复历史重复
    :return: 已规整的独立配置字典列表
    :raises ValueError: 名称为空或重复，或新值稳定身份为空或重复
    """
    normalized: list[dict[str, Any]] = []
    names: set[str] = set()
    identities: set[str] = set()
    for config in value:
        identity = config.id.strip()
        if validate_identities and (not identity or identity in identities):
            raise ValueError("通知渠道稳定身份ID不能为空或重复")
        identities.add(identity)
        name = (config.name or "").strip()
        if not name:
            raise ValueError("通知渠道名称不能为空")
        key = name.casefold()
        if key in names:
            raise ValueError(f"通知渠道名称重复：{name}")
        names.add(key)
        data = config.model_dump(exclude={"notification_enabled", "interaction_enabled", "interaction_policy"})
        data["id"] = identity
        data["name"] = name
        from app.domain.authorization import LEGACY_CHANNELS

        definition = LEGACY_CHANNELS.get(data.get("type") or "")
        if definition:
            values = dict(data.get("config") or {})
            values.pop(definition[1], None)
            values.pop(definition[2], None)
            data["config"] = values
        normalized.append(data)
    return normalized


@router.post(
    "/config",
    summary="保存兼容通知渠道配置",
    response_model=_SchemaResponse[Dict[str, Any]],
)
async def save_config(
    configs: list[NotificationConf] = Body(...),
    _: object = Depends(get_current_active_superuser),
) -> _SchemaResponse[object]:
    """
    将可表达的旧设置写入当前资源，连接变化由生命周期入口处理。
    """
    try:
        incoming = _normalize_configs(configs)
        current = await _channel_settings().replace_legacy([NotificationConf.model_validate(row) for row in incoming])
        return _SchemaResponse(success=True, message="通知配置保存成功", data={"value": current})
    except CommittedChannelSettingsError as error:
        return _SchemaResponse(success=False, message=str(error), data={"value": error.data})
    except ChannelSettingsConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ValueError:
        return _SchemaResponse(success=False, message="通知配置保存失败，请核对名称、类型和稳定 ID")


@router.post(
    "/manage",
    summary="通知渠道统一管理",
    response_model=_SchemaResponse[Dict[str, Any]],
)
def manage_channel(
    request: _SchemaManageRequest,
    _: object = Depends(get_current_active_superuser),
) -> _SchemaResponse[object]:
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
        message=result.get("message") or "",
        data=result.get("data"),
    )
