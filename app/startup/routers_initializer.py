from fastapi import FastAPI

from app.core.config import settings


def init_routers(app: FastAPI):
    """
    初始化路由
    """
    from app.api.apiv1 import api_router
    from app.api.apiv2 import api_router_v2
    from app.api.apiv2_utils import API_V2_STR, configure_v2_openapi
    from app.api.servarr import arr_router
    from app.api.servcookie import cookie_router
    # API路由
    app.include_router(api_router, prefix=settings.API_V1_STR)
    # v2 API复用v1路由，仅在响应出口统一封装
    app.include_router(api_router_v2, prefix=API_V2_STR)
    configure_v2_openapi(app)
    # Radarr、Sonarr路由
    app.include_router(arr_router, prefix="/api/v3")
    # CookieCloud路由
    app.include_router(cookie_router, prefix="/cookiecloud")
