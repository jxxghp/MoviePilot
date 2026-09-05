"""订阅维护命令端点。"""

from typing import Any

from fastapi import Depends

from app.api.dependencies.auth import (
    get_current_active_user,
    get_current_active_user_async,
)
from app.api.dependencies.subscription import (
    get_search_subscriptions_command,
    get_subscription_mutation_service,
)
from app.api.principal import ApiPrincipal
from app.api.response import ResponseAPIRouter
from app.application.scheduling import get_scheduler
from app.application.subscription.mutation import (
    SubscriptionActor,
    SubscriptionMutationService,
)
from app.application.subscription.search import (
    SearchSubscriptionsCommand,
    SubscribeSearchActor,
)
from app.schemas.response import Response

router = ResponseAPIRouter()


@router.get(  # type: ignore[misc]
    "/refresh",
    summary="刷新订阅（兼容入口）",
    response_model=Response[None],
    include_in_schema=False,
    deprecated=True,
)
@router.post(  # type: ignore[misc]
    "/refresh", summary="刷新订阅", response_model=Response[None]
)
def refresh_subscribes(
    current_user: ApiPrincipal = Depends(get_current_active_user),
) -> Any:
    """刷新所有订阅。"""
    if not current_user.is_superuser:
        return Response(success=False, message="订阅不存在")
    get_scheduler().start("subscribe_refresh")
    return Response(success=True)


@router.get(  # type: ignore[misc]
    "/reset/{subid}",
    summary="重置订阅（兼容入口）",
    response_model=Response[None],
    include_in_schema=False,
    deprecated=True,
)
@router.post(  # type: ignore[misc]
    "/reset/{subid}", summary="重置订阅", response_model=Response[None]
)
async def reset_subscribes(
    subid: int,
    mutation: SubscriptionMutationService = Depends(get_subscription_mutation_service),
    current_user: ApiPrincipal = Depends(get_current_active_user_async),
) -> Any:
    """重置一个订阅。"""
    actor = SubscriptionActor(
        name=current_user.name,
        is_superuser=current_user.is_superuser,
    )
    change = await mutation.reset(subid, actor)
    if change:
        return Response(success=True)
    return Response(success=False, message="订阅不存在")


@router.get(  # type: ignore[misc]
    "/check",
    summary="刷新订阅 TMDB 信息（兼容入口）",
    response_model=Response[None],
    include_in_schema=False,
    deprecated=True,
)
@router.post(  # type: ignore[misc]
    "/check", summary="刷新订阅 TMDB 信息", response_model=Response[None]
)
def check_subscribes(
    current_user: ApiPrincipal = Depends(get_current_active_user),
) -> Any:
    """刷新订阅 TMDB 信息。"""
    if not current_user.is_superuser:
        return Response(success=False, message="订阅不存在")
    get_scheduler().start("subscribe_tmdb")
    return Response(success=True)


@router.get(  # type: ignore[misc]
    "/search",
    summary="搜索所有订阅（兼容入口）",
    response_model=Response[None],
    include_in_schema=False,
    deprecated=True,
)
@router.post(  # type: ignore[misc]
    "/search", summary="搜索所有订阅", response_model=Response[None]
)
async def search_subscribes(
    command: SearchSubscriptionsCommand = Depends(get_search_subscriptions_command),
    current_user: ApiPrincipal = Depends(get_current_active_user_async),
) -> Any:
    """搜索当前用户可管理的全部订阅。"""
    await command.execute(
        SubscribeSearchActor(
            username=current_user.name,
            is_superuser=current_user.is_superuser,
        )
    )
    return Response(success=True)


@router.get(  # type: ignore[misc]
    "/search/{subscribe_id}",
    summary="搜索订阅（兼容入口）",
    response_model=Response[None],
    include_in_schema=False,
    deprecated=True,
)
@router.post(  # type: ignore[misc]
    "/search/{subscribe_id}",
    summary="搜索订阅",
    response_model=Response[None],
)
async def search_subscribe(
    subscribe_id: int,
    command: SearchSubscriptionsCommand = Depends(get_search_subscriptions_command),
    current_user: ApiPrincipal = Depends(get_current_active_user_async),
) -> Any:
    """根据订阅编号搜索一个订阅。"""
    found = await command.execute(
        SubscribeSearchActor(
            username=current_user.name,
            is_superuser=current_user.is_superuser,
        ),
        subscribe_id=subscribe_id,
    )
    if not found:
        return Response(success=False, message="订阅不存在")
    return Response(success=True)
