from fastapi import APIRouter

from app.api.routers import API_V1_ROUTER_SPECS

api_router = APIRouter()
for spec in API_V1_ROUTER_SPECS:
    api_router.include_router(
        spec.router,
        prefix=spec.prefix,
        tags=list(spec.tags),
    )
