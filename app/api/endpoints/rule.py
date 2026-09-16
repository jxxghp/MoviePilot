"""过滤规则和规则组管理 API。"""

from typing import Annotated, Any, Optional

from fastapi import Depends, Query

from app.api.context import get_host_runtime
from app.api.dependencies.auth import (
    get_current_active_superuser_async,
    get_current_active_user_async,
)
from app.api.principal import ApiPrincipal
from app.api.response import ResponseAPIRouter
from app.application.filtering import FilterRuleService
from app.schemas.common import JsonObject as _SchemaJsonObject
from app.schemas.response import Response as _SchemaResponse
from app.schemas.rule import (
    CustomFilterRuleCreateRequest as _SchemaCustomFilterRuleCreateRequest,
)
from app.schemas.rule import (
    CustomFilterRuleReorderRequest as _SchemaCustomFilterRuleReorderRequest,
)
from app.schemas.rule import (
    CustomFilterRuleUpdateRequest as _SchemaCustomFilterRuleUpdateRequest,
)
from app.schemas.rule import (
    FilterRuleGroupCreateRequest as _SchemaFilterRuleGroupCreateRequest,
)
from app.schemas.rule import (
    FilterRuleGroupReorderRequest as _SchemaFilterRuleGroupReorderRequest,
)
from app.schemas.rule import (
    FilterRuleGroupUpdateRequest as _SchemaFilterRuleGroupUpdateRequest,
)
from app.startup.composition.context import HostRuntime

router = ResponseAPIRouter()


def _service(runtime: HostRuntime) -> FilterRuleService:
    """从宿主组合根收窄出规则 API 所需端口。"""
    return FilterRuleService(
        runtime.agent.subscriptions,
        runtime.agent.async_rule_group_mutation_scope,
        runtime.system.publish_config_changed,
    )


@router.get(  # type: ignore[misc]
    "/builtin",
    summary="查询内置过滤规则",
    response_model=_SchemaResponse[_SchemaJsonObject],
)
async def query_builtin_rules(
    rule_ids: Annotated[
        Optional[list[str]],
        Query(
            description=(
                "Exact built-in rule IDs to return. Repeat rule_ids in the query string; "
                "omit it to list every built-in rule."
            )
        ),
    ] = None,
    _: ApiPrincipal = Depends(get_current_active_user_async),
) -> _SchemaResponse[Any]:
    """返回内置规则及规则串语法。"""
    return _SchemaResponse(
        success=True,
        data=FilterRuleService.query_builtin(rule_ids),
    )


@router.get(  # type: ignore[misc]
    "/custom",
    summary="查询自定义过滤规则",
    response_model=_SchemaResponse[_SchemaJsonObject],
)
async def query_custom_rules(
    rule_ids: Annotated[
        Optional[list[str]],
        Query(
            description=(
                "Exact custom rule IDs to return. Repeat rule_ids in the query string; "
                "omit it to list every custom rule."
            )
        ),
    ] = None,
    include_group_refs: bool = True,
    _: ApiPrincipal = Depends(get_current_active_user_async),
) -> _SchemaResponse[Any]:
    """返回自定义规则和可选规则组引用。"""
    return _SchemaResponse(
        success=True,
        data=FilterRuleService.query_custom(
            rule_ids,
            include_group_refs=include_group_refs,
        ),
    )


@router.get(  # type: ignore[misc]
    "/groups",
    summary="查询过滤规则组",
    response_model=_SchemaResponse[_SchemaJsonObject],
)
async def query_rule_groups(
    group_names: Annotated[
        Optional[list[str]],
        Query(
            description=(
                "Exact rule-group names to return. Repeat group_names in the query string; "
                "omit it to list every group."
            )
        ),
    ] = None,
    include_usage: bool = True,
    _: ApiPrincipal = Depends(get_current_active_user_async),
    runtime: HostRuntime = Depends(get_host_runtime),
) -> _SchemaResponse[Any]:
    """返回规则组、解析层级和可选引用位置。"""
    return _SchemaResponse(
        success=True,
        data=await _service(runtime).query_groups(
            group_names,
            include_usage=include_usage,
        ),
    )


@router.post(  # type: ignore[misc]
    "/custom",
    summary="新增自定义过滤规则",
    response_model=_SchemaResponse[_SchemaJsonObject],
)
async def add_custom_rule(
    payload: _SchemaCustomFilterRuleCreateRequest,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
    runtime: HostRuntime = Depends(get_host_runtime),
) -> _SchemaResponse[Any]:
    """校验并新增一条自定义过滤规则。"""
    try:
        data = await _service(runtime).add_custom(**payload.model_dump())
    except ValueError as error:
        return _SchemaResponse(success=False, message=str(error))
    return _SchemaResponse(success=True, message=data.get("message"), data=data)


@router.put(  # type: ignore[misc]
    "/custom/reorder",
    summary="调整自定义过滤规则顺序",
    response_model=_SchemaResponse[_SchemaJsonObject],
)
async def reorder_custom_rules(
    payload: _SchemaCustomFilterRuleReorderRequest,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
    runtime: HostRuntime = Depends(get_host_runtime),
) -> _SchemaResponse[Any]:
    """只调整现有自定义规则顺序并拒绝过期集合覆盖。"""
    try:
        data = await _service(runtime).reorder_custom(
            payload.rule_ids,
            expected_rule_ids=payload.expected_rule_ids,
        )
    except ValueError as error:
        return _SchemaResponse(success=False, message=str(error))
    return _SchemaResponse(success=True, message=data.get("message"), data=data)


@router.put(  # type: ignore[misc]
    "/custom/{rule_id}",
    summary="更新自定义过滤规则",
    response_model=_SchemaResponse[_SchemaJsonObject],
)
async def update_custom_rule(
    rule_id: str,
    payload: _SchemaCustomFilterRuleUpdateRequest,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
    runtime: HostRuntime = Depends(get_host_runtime),
) -> _SchemaResponse[Any]:
    """更新自定义规则并在改名时原子重写引用。"""
    try:
        data = await _service(runtime).update_custom(
            current_rule_id=rule_id,
            **payload.model_dump(),
        )
    except ValueError as error:
        return _SchemaResponse(success=False, message=str(error))
    return _SchemaResponse(success=True, message=data.get("message"), data=data)


@router.delete(  # type: ignore[misc]
    "/custom/{rule_id}",
    summary="删除自定义过滤规则",
    response_model=_SchemaResponse[_SchemaJsonObject],
)
async def delete_custom_rule(
    rule_id: str,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
    runtime: HostRuntime = Depends(get_host_runtime),
) -> _SchemaResponse[Any]:
    """删除一条未被规则组引用的自定义过滤规则。"""
    try:
        data = await _service(runtime).delete_custom(rule_id)
    except ValueError as error:
        return _SchemaResponse(success=False, message=str(error))
    return _SchemaResponse(success=True, message=data.get("message"), data=data)


@router.post(  # type: ignore[misc]
    "/groups",
    summary="新增过滤规则组",
    response_model=_SchemaResponse[_SchemaJsonObject],
)
async def add_rule_group(
    payload: _SchemaFilterRuleGroupCreateRequest,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
    runtime: HostRuntime = Depends(get_host_runtime),
) -> _SchemaResponse[Any]:
    """新增一个已经完成语法与引用校验的规则组。"""
    try:
        data = await _service(runtime).add_group(**payload.model_dump())
    except ValueError as error:
        return _SchemaResponse(success=False, message=str(error))
    return _SchemaResponse(success=True, message=data.get("message"), data=data)


@router.put(  # type: ignore[misc]
    "/groups/reorder",
    summary="调整过滤规则组顺序",
    response_model=_SchemaResponse[_SchemaJsonObject],
)
async def reorder_rule_groups(
    payload: _SchemaFilterRuleGroupReorderRequest,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
    runtime: HostRuntime = Depends(get_host_runtime),
) -> _SchemaResponse[Any]:
    """只调整现有规则组顺序并拒绝过期集合覆盖。"""
    try:
        data = await _service(runtime).reorder_groups(
            payload.group_names,
            expected_group_names=payload.expected_group_names,
        )
    except ValueError as error:
        return _SchemaResponse(success=False, message=str(error))
    return _SchemaResponse(success=True, message=data.get("message"), data=data)


@router.put(  # type: ignore[misc]
    "/groups/{name}",
    summary="更新过滤规则组",
    response_model=_SchemaResponse[_SchemaJsonObject],
)
async def update_rule_group(
    name: str,
    payload: _SchemaFilterRuleGroupUpdateRequest,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
    runtime: HostRuntime = Depends(get_host_runtime),
) -> _SchemaResponse[Any]:
    """更新规则组并原子重写全部名称引用。"""
    try:
        data = await _service(runtime).update_group(
            current_name=name,
            **payload.model_dump(),
        )
    except ValueError as error:
        return _SchemaResponse(success=False, message=str(error))
    return _SchemaResponse(success=True, message=data.get("message"), data=data)


@router.delete(  # type: ignore[misc]
    "/groups/{name}",
    summary="删除过滤规则组",
    response_model=_SchemaResponse[_SchemaJsonObject],
)
async def delete_rule_group(
    name: str,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
    runtime: HostRuntime = Depends(get_host_runtime),
) -> _SchemaResponse[Any]:
    """删除规则组并原子清理全部全局和订阅引用。"""
    try:
        data = await _service(runtime).delete_group(name)
    except ValueError as error:
        return _SchemaResponse(success=False, message=str(error))
    return _SchemaResponse(success=True, message=data.get("message"), data=data)
