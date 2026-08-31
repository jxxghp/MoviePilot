"""Agent 结构化 MoviePilot API 操作与固定路由注册表。"""

from dataclasses import dataclass
from typing import Optional

from app.agent.policy.contracts import (
    ActionEffect,
    ConfirmationMode,
    PrincipalRole,
    RecoveryMode,
    ResultSensitivity,
)


@dataclass(frozen=True)
class ApiOperationSpec:
    """描述一个可由 Agent API 网关调用的稳定业务操作。"""

    operation_id: str
    effect: ActionEffect
    required_role: PrincipalRole
    confirmation: ConfirmationMode
    recovery: RecoveryMode
    result_sensitivity: ResultSensitivity


@dataclass(frozen=True)
class ApiOperationRoute:
    """描述一个固定 API 路由，禁止调用方注入任意 URL 或 HTTP 方法。"""

    method: str
    path: str


_ADMIN = PrincipalRole.SYSTEM_ADMIN
_CONFIRM = ConfirmationMode.REQUIRED
_IDEMPOTENT = RecoveryMode.IDEMPOTENT
_DELETE_RECOVERABLE = RecoveryMode.RECOVERABLE_DELETE


def _spec(
    operation_id: str,
    *,
    effect: ActionEffect = ActionEffect.SAFE_READ,
    required_role: PrincipalRole = PrincipalRole.USER,
    confirmation: ConfirmationMode = ConfirmationMode.NONE,
    recovery: RecoveryMode = RecoveryMode.NONE,
    result_sensitivity: ResultSensitivity = ResultSensitivity.NORMAL,
) -> ApiOperationSpec:
    """构造不可变 API 操作描述。"""
    return ApiOperationSpec(
        operation_id=operation_id,
        effect=effect,
        required_role=required_role,
        confirmation=confirmation,
        recovery=recovery,
        result_sensitivity=result_sensitivity,
    )


def _write(
    operation_id: str,
    *,
    effect: ActionEffect = ActionEffect.REVERSIBLE_WRITE,
    recovery: RecoveryMode = _IDEMPOTENT,
    sensitivity: ResultSensitivity = ResultSensitivity.NORMAL,
) -> ApiOperationSpec:
    """构造管理员确认写操作。"""
    return _spec(
        operation_id,
        effect=effect,
        required_role=_ADMIN,
        confirmation=_CONFIRM,
        recovery=recovery,
        result_sensitivity=sensitivity,
    )


def _admin_read(
    operation_id: str,
    *,
    sensitivity: ResultSensitivity = ResultSensitivity.NORMAL,
) -> ApiOperationSpec:
    """构造管理员读取操作。"""
    return _spec(operation_id, required_role=_ADMIN, result_sensitivity=sensitivity)


API_FIRST_BATCH_OPERATION_SPECS: tuple[ApiOperationSpec, ...] = (
    _spec("media.search"),
    _spec("media.person.search"),
    _spec("media.person.credits"),
    _spec("media.recognize"),
    _spec("media.scrape", effect=ActionEffect.EXTERNAL_SIDE_EFFECT, required_role=_ADMIN, confirmation=_CONFIRM),
    _spec("media.episode_schedule"),
    _spec("media.detail"),
    _write("subscription.add"),
    _write("subscription.update"),
    _spec("subscription.search"),
    _spec("subscription.list"),
    _spec("subscription.shares"),
    _spec("subscription.popular"),
    _spec("subscription.history"),
    _write("subscription.delete", effect=ActionEffect.DESTRUCTIVE_WRITE, recovery=_DELETE_RECOVERABLE),
    _spec("download.add", effect=ActionEffect.EXTERNAL_SIDE_EFFECT, confirmation=_CONFIRM, recovery=_IDEMPOTENT),
    _write("download.history.delete", effect=ActionEffect.DESTRUCTIVE_WRITE, recovery=_DELETE_RECOVERABLE),
    _write("transfer.history.delete", effect=ActionEffect.DESTRUCTIVE_WRITE, recovery=_DELETE_RECOVERABLE),
    _write("site.update"),
    _admin_read("site.list"),
    _admin_read("site.userdata", sensitivity=ResultSensitivity.PRIVATE),
    _spec("site.test", effect=ActionEffect.EXTERNAL_SIDE_EFFECT, required_role=_ADMIN, confirmation=_CONFIRM),
    _write("site.cookie.update", sensitivity=ResultSensitivity.PRIVATE),
    _spec("recommendation.list"),
    _admin_read("library.exists"),
    _admin_read("storage.settings"),
    _admin_read("storage.list", sensitivity=ResultSensitivity.PRIVATE),
    _admin_read("transfer.history", sensitivity=ResultSensitivity.PRIVATE),
    _spec(
        "transfer.file",
        effect=ActionEffect.EXTERNAL_SIDE_EFFECT,
        required_role=_ADMIN,
        confirmation=_CONFIRM,
        recovery=RecoveryMode.RECONCILE,
    ),
    _admin_read("scheduler.list"),
    _spec(
        "scheduler.run",
        effect=ActionEffect.EXTERNAL_SIDE_EFFECT,
        required_role=_ADMIN,
        confirmation=_CONFIRM,
        recovery=RecoveryMode.RECONCILE,
    ),
    _admin_read("workflow.list"),
    _spec(
        "workflow.run",
        effect=ActionEffect.EXTERNAL_SIDE_EFFECT,
        required_role=_ADMIN,
        confirmation=_CONFIRM,
        recovery=RecoveryMode.RECONCILE,
    ),
    _admin_read("plugin.installed"),
    _admin_read("plugin.market"),
    _admin_read("plugin.capabilities"),
    _admin_read("plugin.config.get", sensitivity=ResultSensitivity.PRIVATE),
    _write("plugin.config.update", sensitivity=ResultSensitivity.PRIVATE),
    _spec(
        "plugin.reload",
        effect=ActionEffect.EXTERNAL_SIDE_EFFECT,
        required_role=_ADMIN,
        confirmation=_CONFIRM,
        recovery=RecoveryMode.RECONCILE,
    ),
    _spec(
        "plugin.install",
        effect=ActionEffect.EXTERNAL_SIDE_EFFECT,
        required_role=_ADMIN,
        confirmation=_CONFIRM,
        recovery=RecoveryMode.RECONCILE,
    ),
    _write("plugin.uninstall", effect=ActionEffect.DESTRUCTIVE_WRITE, recovery=_DELETE_RECOVERABLE),
    _admin_read("slash.list"),
    _admin_read("config.identifiers.get"),
    _write("config.identifiers.update"),
)


API_PARITY_OPERATION_SPECS: tuple[ApiOperationSpec, ...] = (
    _spec("search.torrents"),
    _spec("search.results"),
    _admin_read("filter.builtin"),
    _admin_read("filter.custom"),
    _admin_read("filter.groups"),
    _write("filter.custom.add", recovery=RecoveryMode.TRANSACTION),
    _write("filter.custom.update", recovery=RecoveryMode.TRANSACTION),
    _write("filter.custom.delete", effect=ActionEffect.DESTRUCTIVE_WRITE, recovery=RecoveryMode.TRANSACTION),
    _write("filter.group.add", recovery=RecoveryMode.TRANSACTION),
    _write("filter.group.update", recovery=RecoveryMode.TRANSACTION),
    _write("filter.group.delete", effect=ActionEffect.DESTRUCTIVE_WRITE, recovery=RecoveryMode.TRANSACTION),
    _admin_read("plugin.data", sensitivity=ResultSensitivity.PRIVATE),
    _admin_read("config.system.get", sensitivity=ResultSensitivity.PRIVATE),
    _write("config.system.update", recovery=RecoveryMode.TRANSACTION, sensitivity=ResultSensitivity.PRIVATE),
    _spec(
        "slash.run",
        effect=ActionEffect.EXTERNAL_SIDE_EFFECT,
        required_role=_ADMIN,
        confirmation=_CONFIRM,
        recovery=RecoveryMode.RECONCILE,
    ),
)


API_OPERATION_SPECS: tuple[ApiOperationSpec, ...] = (*API_FIRST_BATCH_OPERATION_SPECS, *API_PARITY_OPERATION_SPECS)
API_OPERATION_BY_ID = {spec.operation_id: spec for spec in API_OPERATION_SPECS}


# 所有路径均由宿主注册，调用方只能传路径参数、查询参数和 JSON body。
API_OPERATION_ROUTES: dict[str, ApiOperationRoute] = {
    "media.search": ApiOperationRoute("GET", "/api/v1/media/search"),
    "media.person.search": ApiOperationRoute("GET", "/api/v1/media/search"),
    "media.person.credits": ApiOperationRoute("GET", "/api/v1/{source}/person/credits/{person_id}"),
    "media.recognize": ApiOperationRoute("GET", "/api/v1/media/recognize"),
    "media.scrape": ApiOperationRoute("POST", "/api/v1/media/scrape/{storage}"),
    "media.episode_schedule": ApiOperationRoute("GET", "/api/v1/tmdb/{tmdbid}/{season}"),
    "media.detail": ApiOperationRoute("GET", "/api/v1/media/{media_id}"),
    "subscription.add": ApiOperationRoute("POST", "/api/v1/subscribe/"),
    "subscription.update": ApiOperationRoute("PUT", "/api/v1/subscribe/"),
    "subscription.search": ApiOperationRoute("GET", "/api/v1/subscribe/search/{subscribe_id}"),
    "subscription.list": ApiOperationRoute("GET", "/api/v1/subscribe/"),
    "subscription.shares": ApiOperationRoute("GET", "/api/v1/subscribe/shares"),
    "subscription.popular": ApiOperationRoute("GET", "/api/v1/subscribe/popular"),
    "subscription.history": ApiOperationRoute("GET", "/api/v1/subscribe/history/{mtype}"),
    "subscription.delete": ApiOperationRoute("DELETE", "/api/v1/subscribe/{subscribe_id}"),
    "download.add": ApiOperationRoute("POST", "/api/v1/download/add"),
    "download.history.delete": ApiOperationRoute("DELETE", "/api/v1/history/download"),
    "transfer.history.delete": ApiOperationRoute("DELETE", "/api/v1/history/transfer"),
    "site.list": ApiOperationRoute("GET", "/api/v1/site/"),
    "site.update": ApiOperationRoute("PUT", "/api/v1/site/"),
    "site.userdata": ApiOperationRoute("GET", "/api/v1/site/userdata/{site_id}"),
    "site.test": ApiOperationRoute("GET", "/api/v1/site/test/{site_id}"),
    "site.cookie.update": ApiOperationRoute("POST", "/api/v1/site/cookie/{site_id}"),
    "recommendation.list": ApiOperationRoute("GET", "/api/v1/recommend/agent"),
    "library.exists": ApiOperationRoute("GET", "/api/v1/mediaserver/exists"),
    "storage.settings": ApiOperationRoute("GET", "/api/v1/storage/directories"),
    "storage.list": ApiOperationRoute("POST", "/api/v1/storage/list"),
    "transfer.history": ApiOperationRoute("GET", "/api/v1/history/transfer"),
    "transfer.file": ApiOperationRoute("POST", "/api/v1/transfer/manual"),
    "scheduler.list": ApiOperationRoute("GET", "/api/v1/dashboard/schedule"),
    "scheduler.run": ApiOperationRoute("GET", "/api/v1/system/runscheduler"),
    "workflow.list": ApiOperationRoute("GET", "/api/v1/workflow/"),
    "workflow.run": ApiOperationRoute("POST", "/api/v1/workflow/{workflow_id}/run"),
    "plugin.installed": ApiOperationRoute("GET", "/api/v1/plugin/installed"),
    "plugin.market": ApiOperationRoute("GET", "/api/v1/plugin/"),
    "plugin.capabilities": ApiOperationRoute("GET", "/api/v1/plugin/runtime/capabilities"),
    "plugin.config.get": ApiOperationRoute("GET", "/api/v1/plugin/{plugin_id}"),
    "plugin.config.update": ApiOperationRoute("PUT", "/api/v1/plugin/{plugin_id}"),
    "plugin.reload": ApiOperationRoute("GET", "/api/v1/plugin/reload/{plugin_id}"),
    "plugin.install": ApiOperationRoute("GET", "/api/v1/plugin/install/{plugin_id}"),
    "plugin.uninstall": ApiOperationRoute("DELETE", "/api/v1/plugin/{plugin_id}"),
    "slash.list": ApiOperationRoute("GET", "/api/v1/message/agent/commands"),
    "config.identifiers.get": ApiOperationRoute("GET", "/api/v1/system/identifiers"),
    "config.identifiers.update": ApiOperationRoute("POST", "/api/v1/system/identifiers"),
    "search.torrents": ApiOperationRoute("GET", "/api/v1/search/media/{media_id}"),
    "search.results": ApiOperationRoute("GET", "/api/v1/search/last/context"),
    "filter.builtin": ApiOperationRoute("GET", "/api/v1/rule/builtin"),
    "filter.custom": ApiOperationRoute("GET", "/api/v1/rule/custom"),
    "filter.groups": ApiOperationRoute("GET", "/api/v1/rule/groups"),
    "filter.custom.add": ApiOperationRoute("POST", "/api/v1/rule/custom"),
    "filter.custom.update": ApiOperationRoute("PUT", "/api/v1/rule/custom/{rule_id}"),
    "filter.custom.delete": ApiOperationRoute("DELETE", "/api/v1/rule/custom/{rule_id}"),
    "filter.group.add": ApiOperationRoute("POST", "/api/v1/rule/groups"),
    "filter.group.update": ApiOperationRoute("PUT", "/api/v1/rule/groups/{name}"),
    "filter.group.delete": ApiOperationRoute("DELETE", "/api/v1/rule/groups/{name}"),
    "plugin.data": ApiOperationRoute("GET", "/api/v1/plugin/runtime/{plugin_id}/data"),
    "config.system.get": ApiOperationRoute("GET", "/api/v1/system/settings"),
    "config.system.update": ApiOperationRoute("POST", "/api/v1/system/settings"),
    "slash.run": ApiOperationRoute("POST", "/api/v1/message/agent/commands/run"),
}


def resolve_api_operation(operation_id: str) -> Optional[ApiOperationSpec]:
    """按稳定 operation ID 查找 API 操作。"""
    return API_OPERATION_BY_ID.get(operation_id)


def resolve_api_route(operation_id: str) -> Optional[ApiOperationRoute]:
    """按稳定 operation ID 查找固定 API 路由。"""
    return API_OPERATION_ROUTES.get(operation_id)


def list_api_operation_ids() -> tuple[str, ...]:
    """返回按注册顺序排列的 API 操作 ID。"""
    return tuple(spec.operation_id for spec in API_OPERATION_SPECS)


__all__ = [
    "API_FIRST_BATCH_OPERATION_SPECS",
    "API_OPERATION_BY_ID",
    "API_OPERATION_ROUTES",
    "API_OPERATION_SPECS",
    "API_PARITY_OPERATION_SPECS",
    "ApiOperationRoute",
    "ApiOperationSpec",
    "list_api_operation_ids",
    "resolve_api_operation",
    "resolve_api_route",
]
