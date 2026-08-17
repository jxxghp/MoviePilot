from fastapi import FastAPI

from app.runtime.config import settings


def init_routers(app: FastAPI):
    """
    初始化路由
    """
    from app.api.router_specs import API_V1_ROUTER_SPECS
    from app.api.servarr import arr_router
    from app.api.servcookie import cookie_router
    # 直接聚合端点路由，避免先构建兼容路由器再克隆到最终应用。
    for spec in API_V1_ROUTER_SPECS:
        app.include_router(
            spec.router,
            prefix=f"{settings.API_V1_STR}{spec.prefix}",
            tags=list(spec.tags),
        )
    # Radarr、Sonarr路由
    app.include_router(arr_router, prefix="/api/v3")
    # CookieCloud路由
    app.include_router(cookie_router, prefix="/cookiecloud")
