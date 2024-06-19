from fastapi import APIRouter

from app.api.endpoints import login, user, site, message, webhook, subscribe, \
    media, douban, search, plugin, tmdb, history, system, download, dashboard, \
    local, transfer, mediaserver, bangumi, aliyun, u115

api_router = APIRouter()
api_router.include_router(login.router, prefix="/login", tags=["login"])
api_router.include_router(user.router, prefix="/user", tags=["user"])
api_router.include_router(site.router, prefix="/site", tags=["site"])
api_router.include_router(message.router, prefix="/message", tags=["message"])
api_router.include_router(webhook.router, prefix="/webhook", tags=["webhook"])
api_router.include_router(subscribe.router, prefix="/subscribe", tags=["subscribe"])
api_router.include_router(media.router, prefix="/media", tags=["media"])
api_router.include_router(search.router, prefix="/search", tags=["search"])
api_router.include_router(douban.router, prefix="/douban", tags=["douban"])
api_router.include_router(tmdb.router, prefix="/tmdb", tags=["tmdb"])
api_router.include_router(history.router, prefix="/history", tags=["history"])
api_router.include_router(system.router, prefix="/system", tags=["system"])
api_router.include_router(plugin.router, prefix="/plugin", tags=["plugin"])
api_router.include_router(download.router, prefix="/download", tags=["download"])
api_router.include_router(dashboard.router, prefix="/dashboard", tags=["dashboard"])
api_router.include_router(local.router, prefix="/local", tags=["local"])
api_router.include_router(transfer.router, prefix="/transfer", tags=["transfer"])
api_router.include_router(mediaserver.router, prefix="/mediaserver", tags=["mediaserver"])
api_router.include_router(bangumi.router, prefix="/bangumi", tags=["bangumi"])
api_router.include_router(aliyun.router, prefix="/aliyun", tags=["aliyun"])
api_router.include_router(u115.router, prefix="/u115", tags=["115"])
