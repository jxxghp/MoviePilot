from typing import NamedTuple

from fastapi import APIRouter

from app.api.endpoints import (
    agent,
    anilist,
    anthropic,
    auth,
    bangumi,
    command,
    dashboard,
    discover,
    douban,
    download,
    filterrule,
    history,
    llm,
    metaparser,
    login,
    mcp,
    media,
    mediaserver,
    message,
    mfa,
    music,
    notification,
    openai,
    plugin,
    recommend,
    search,
    service,
    site,
    storage,
    subscribe,
    system,
    tmdb,
    torrent,
    transfer,
    user,
    webhook,
    workflow,
)


class RouterSpec(NamedTuple):
    """声明一个 v1 端点路由器及其公开路径元数据。"""

    router: APIRouter  # 原始端点路由器
    prefix: str  # 相对 v1 根路径的前缀
    tags: tuple[str, ...]  # 追加到端点 OpenAPI 操作的标签


API_V1_ROUTER_SPECS: tuple[RouterSpec, ...] = (
    RouterSpec(auth.router, "/auth", ("auth",)),
    RouterSpec(login.router, "/login", ("login",)),
    RouterSpec(user.router, "/user", ("user",)),
    RouterSpec(mfa.router, "/mfa", ("mfa",)),
    RouterSpec(site.router, "/site", ("site",)),
    RouterSpec(message.router, "/message", ("message",)),
    RouterSpec(agent.router, "/message/agent", ("agent",)),
    RouterSpec(webhook.router, "/webhook", ("webhook",)),
    RouterSpec(subscribe.router, "/subscribe", ("subscribe",)),
    RouterSpec(music.router, "/music", ("music",)),
    RouterSpec(media.router, "/media", ("media",)),
    RouterSpec(search.router, "/search", ("search",)),
    RouterSpec(douban.router, "/douban", ("douban",)),
    RouterSpec(tmdb.router, "/tmdb", ("tmdb",)),
    RouterSpec(history.router, "/history", ("history",)),
    RouterSpec(system.router, "/system", ("system",)),
    RouterSpec(metaparser.router, "/metaparser", ("metaparser",)),
    RouterSpec(filterrule.router, "/filterrule", ("filterrule",)),
    RouterSpec(command.router, "/command", ("command",)),
    RouterSpec(notification.router, "/notification", ("notification",)),
    RouterSpec(llm.router, "/llm", ("llm",)),
    RouterSpec(plugin.router, "/plugin", ("plugin",)),
    RouterSpec(download.router, "/download", ("download",)),
    RouterSpec(dashboard.router, "/dashboard", ("dashboard",)),
    RouterSpec(storage.router, "/storage", ("storage",)),
    RouterSpec(service.router, "/service", ("service",)),
    RouterSpec(transfer.router, "/transfer", ("transfer",)),
    RouterSpec(mediaserver.router, "/mediaserver", ("mediaserver",)),
    RouterSpec(bangumi.router, "/bangumi", ("bangumi",)),
    RouterSpec(anilist.router, "/anilist", ("anilist",)),
    RouterSpec(discover.router, "/discover", ("discover",)),
    RouterSpec(recommend.router, "/recommend", ("recommend",)),
    RouterSpec(workflow.router, "/workflow", ("workflow",)),
    RouterSpec(torrent.router, "/torrent", ("torrent",)),
    RouterSpec(mcp.router, "/mcp", ("mcp",)),
    RouterSpec(openai.router, "/openai/v1", ("openai",)),
    RouterSpec(anthropic.router, "/anthropic/v1", ("anthropic",)),
)
