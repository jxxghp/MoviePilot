from fastapi import FastAPI


def init_routers(app: FastAPI, api_prefix: str = "/api/v1"):
    """
    初始化路由

    :param app: 需要挂载路由的 FastAPI 应用
    :param api_prefix: v1 API 根路径，由启动组合根传入
    """
    from app.api.router_specs import API_V1_ROUTER_SPECS
    from app.api.servarr import arr_router
    from app.api.servcookie import cookie_router
    # 直接聚合端点路由，避免先构建兼容路由器再克隆到最终应用。
    for spec in API_V1_ROUTER_SPECS:
        app.include_router(
            spec.router,
            prefix=f"{api_prefix}{spec.prefix}",
            tags=list(spec.tags),
        )
    # Radarr、Sonarr路由
    app.include_router(arr_router, prefix="/api/v3")
    # CookieCloud路由
    app.include_router(cookie_router, prefix="/cookiecloud")
