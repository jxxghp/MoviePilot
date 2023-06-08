from fastapi import APIRouter

from app.api.endpoints import login, users, sites, messages, webhooks, subscribes, media, douban

api_router = APIRouter()
api_router.include_router(login.router, tags=["login"])
api_router.include_router(users.router, prefix="/users", tags=["users"])
api_router.include_router(sites.router, prefix="/sites", tags=["sites"])
api_router.include_router(messages.router, prefix="/messages", tags=["messages"])
api_router.include_router(webhooks.router, prefix="/webhooks", tags=["webhooks"])
api_router.include_router(subscribes.router, prefix="/subscribes", tags=["subscribes"])
api_router.include_router(media.router, prefix="/media", tags=["media"])
api_router.include_router(douban.router, prefix="/douban", tags=["douban"])
