from fastapi import APIRouter

from app.api.apiv1 import api_router


api_router_v2 = APIRouter()
api_router_v2.include_router(api_router)
