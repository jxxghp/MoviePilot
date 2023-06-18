from fastapi import APIRouter

from app.api.endpoints import login, user, site, message, webhook, subscribe, media, douban, search, plugin, tmdb

api_router = APIRouter()
api_router.include_router(login.router, tags=["login"])
api_router.include_router(user.router, prefix="/user", tags=["user"])
api_router.include_router(site.router, prefix="/site", tags=["site"])
api_router.include_router(message.router, prefix="/message", tags=["message"])
api_router.include_router(webhook.router, prefix="/webhook", tags=["webhook"])
api_router.include_router(subscribe.router, prefix="/subscribe", tags=["subscribe"])
api_router.include_router(media.router, prefix="/media", tags=["media"])
api_router.include_router(search.router, prefix="/search", tags=["search"])
api_router.include_router(douban.router, prefix="/douban", tags=["douban"])
api_router.include_router(tmdb.router, prefix="/tmdb", tags=["tmdb"])
api_router.include_router(plugin.router, prefix="/plugin", tags=["plugin"])
