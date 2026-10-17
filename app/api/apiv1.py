"""
组装第一版 REST API 入口，挂载统一响应策略及各领域路由。
"""

from fastapi import APIRouter

from app.api.routers import API_V1_ROUTER_SPECS

api_router = APIRouter()
for spec in API_V1_ROUTER_SPECS:
    api_router.include_router(
        spec.router,
        prefix=spec.prefix,
        tags=list(spec.tags),
    )
