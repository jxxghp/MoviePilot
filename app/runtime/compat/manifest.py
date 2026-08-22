from dataclasses import dataclass
from typing import Dict, Set


@dataclass(frozen=True, slots=True)
class ModuleAlias:
    """描述一个旧模块路径到 canonical 模块的精确映射。"""

    target: str
    replacement: str
    introduced: str
    owner: str
    is_package: bool = False


@dataclass(frozen=True, slots=True)
class SymbolAlias:
    """描述合成兼容包公开符号的精确来源。"""

    target_module: str
    target_name: str
    replacement: str


# 编排链的旧根 app.chain 的全部子模块；插件按具体链类直接导入，逐个精确登记。
_ORCHESTRATION_MODULES = (
    "acoustid",
    "agent",
    "anilist",
    "bangumi",
    "dashboard",
    "douban",
    "download",
    "interaction",
    "listenbrainz",
    "lrclib",
    "media",
    "mediaserver",
    "message",
    "musicbrainz",
    "notification",
    "ports",
    "ports.dispatch",
    "ports.download",
    "ports.library",
    "ports.metadata",
    "ports.parsing",
    "ports.search",
    "ports.system",
    "ports.transfer",
    "recommend",
    "scraping",
    "search",
    "site",
    "storage",
    "subscribe",
    "system",
    "theaudiodb",
    "tmdb",
    "torrents",
    "transfer",
    "tvdb",
    "user",
    "webhook",
    "_interaction",
    "_messaging",
    "_music",
    "_recognition",
    "_transfer",
)
_ORCHESTRATION_PACKAGES = {"ports"}

# 只登记已经删除旧物理源码、并完成 canonical 路径验证的模块。
MODULE_ALIASES: Dict[str, ModuleAlias] = {
    "app.chain": ModuleAlias(
        target="app.application.orchestration",
        replacement="app.application.orchestration",
        introduced="v3.0.0",
        owner="application",
        is_package=True,
    ),
    **{
        f"app.chain.{name}": ModuleAlias(
            target=f"app.application.orchestration.{name}",
            replacement=f"app.application.orchestration.{name}",
            introduced="v3.0.0",
            owner="application",
            is_package=name in _ORCHESTRATION_PACKAGES,
        )
        for name in _ORCHESTRATION_MODULES
    },
    "app.chain.media_interaction": ModuleAlias(
        target="app.application.orchestration.interaction",
        replacement="app.application.orchestration.interaction",
        introduced="v3.0.0",
        owner="application",
    ),
    "app.log": ModuleAlias(
        target="app.sdk.logging",
        replacement="app.sdk.logging",
        introduced="v3.0.0",
        owner="sdk",
    ),
    "app.domain.string": ModuleAlias(
        target="app.sdk.string",
        replacement="app.sdk.utilities",
        introduced="v3.0.0",
        owner="sdk",
    ),
    # 规则域收敛为单一事实来源 app/application/rules.py：
    # filter_rules.py（内置规则集 + RuleParser）更名而来，filter.py（RuleHelper）并入
    "app.application.filter": ModuleAlias(
        target="app.application.rules",
        replacement="app.application.rules",
        introduced="v3.0.0",
        owner="application",
    ),
    "app.application.filter_rules": ModuleAlias(
        target="app.application.rules",
        replacement="app.application.rules",
        introduced="v3.0.0",
        owner="application",
    ),
    # 媒体库文件系统模块的 canonical 位置是 app/modules/medialibrary
    "app.modules.filemanager": ModuleAlias(
        target="app.modules.medialibrary",
        replacement="app.modules.medialibrary",
        introduced="v3.0.0",
        owner="modules",
        is_package=True,
    ),
    # 整理编排属于宿主固有业务规则，落在 app/application/transferhandler.py
    "app.modules.filemanager.transhandler": ModuleAlias(
        target="app.application.transferhandler",
        replacement="app.application.transferhandler",
        introduced="v3.0.0",
        owner="application",
    ),
    # 存储后端升为一级模块，各自独立成包；存储基类与传输进度回调并入模块样板基类包
    **{
        f"app.modules.filemanager.storages.{name}": ModuleAlias(
            target=f"app.modules.{package}.{name}",
            replacement=f"app.modules.{package}.{name}",
            introduced="v3.0.0",
            owner="modules",
        )
        for name, package in (
            ("alipan", "alipan"),
            ("alist", "alist"),
            ("alistgo", "alistgo"),
            ("local", "localstorage"),
            ("rclone", "rclone"),
            ("smb", "smb"),
            ("u115", "u115"),
        )
    },
    "app.db.agentchat_oper": ModuleAlias(
        target="app.db.oper.agentchat",
        replacement="app.db.oper.agentchat",
        introduced="v3.0.0",
        owner="db",
    ),
    "app.db.agenttask_oper": ModuleAlias(
        target="app.db.oper.agenttask",
        replacement="app.db.oper.agenttask",
        introduced="v3.0.0",
        owner="db",
    ),
    "app.db.downloadfailure_oper": ModuleAlias(
        target="app.db.oper.downloadfailure",
        replacement="app.db.oper.downloadfailure",
        introduced="v3.0.0",
        owner="db",
    ),
    "app.db.downloadhistory_oper": ModuleAlias(
        target="app.db.oper.downloadhistory",
        replacement="app.db.oper.downloadhistory",
        introduced="v3.0.0",
        owner="db",
    ),
    "app.db.init": ModuleAlias(
        target="app.startup.database_initializer",
        replacement="app.startup.database_initializer",
        introduced="v3.0.0",
        owner="startup",
    ),
    "app.db.mediaserver_oper": ModuleAlias(
        target="app.db.oper.mediaserver",
        replacement="app.db.oper.mediaserver",
        introduced="v3.0.0",
        owner="db",
    ),
    "app.db.message_oper": ModuleAlias(
        target="app.db.oper.message",
        replacement="app.db.oper.message",
        introduced="v3.0.0",
        owner="db",
    ),
    "app.db.plugindata_oper": ModuleAlias(
        target="app.db.oper.plugindata",
        replacement="app.db.oper.plugindata",
        introduced="v3.0.0",
        owner="db",
    ),
    "app.db.site_oper": ModuleAlias(
        target="app.db.oper.site",
        replacement="app.db.oper.site",
        introduced="v3.0.0",
        owner="db",
    ),
    "app.db.subscribe_oper": ModuleAlias(
        target="app.sdk._legacy.subscribe",
        replacement="app.application.subscription.write.add_subscribe",
        introduced="v3.0.0",
        owner="sdk",
    ),
    "app.db.subscribehistory_oper": ModuleAlias(
        target="app.db.oper.subscribehistory",
        replacement="app.db.oper.subscribehistory",
        introduced="v3.0.0",
        owner="db",
    ),
    "app.db.systemconfig_oper": ModuleAlias(
        target="app.db.oper.systemconfig",
        replacement="app.db.oper.systemconfig",
        introduced="v3.0.0",
        owner="db",
    ),
    "app.db.transferhistory_oper": ModuleAlias(
        target="app.sdk._legacy.history",
        replacement="app.application.history",
        introduced="v3.0.0",
        owner="sdk",
    ),
    "app.db.transferpending_oper": ModuleAlias(
        target="app.db.oper.transferpending",
        replacement="app.db.oper.transferpending",
        introduced="v3.0.0",
        owner="db",
    ),
    "app.db.user_oper": ModuleAlias(
        target="app.sdk._legacy.user",
        replacement="app.db.oper.user 或 app.api.deps",
        introduced="v3.0.0",
        owner="sdk",
    ),
    "app.db.userconfig_oper": ModuleAlias(
        target="app.db.oper.userconfig",
        replacement="app.db.oper.userconfig",
        introduced="v3.0.0",
        owner="db",
    ),
    "app.db.workflow_oper": ModuleAlias(
        target="app.db.oper.workflow",
        replacement="app.db.oper.workflow",
        introduced="v3.0.0",
        owner="db",
    ),
    "app.command": ModuleAlias(
        target="app.runtime.command",
        replacement="app.runtime.command",
        introduced="v3.0.0",
        owner="runtime",
    ),
    "app.utils.crypto": ModuleAlias(
        target="app.foundation.crypto",
        replacement="app.sdk.utilities",
        introduced="v3.0.0",
        owner="foundation",
    ),
    "app.utils.dom": ModuleAlias(
        target="app.foundation.dom",
        replacement="app.sdk.utilities",
        introduced="v3.0.0",
        owner="foundation",
    ),
    "app.utils.identity": ModuleAlias(
        target="app.foundation.identity",
        replacement="app.foundation.identity",
        introduced="v3.0.0",
        owner="foundation",
    ),
    "app.utils.ip": ModuleAlias(
        target="app.adapters.network.ip",
        replacement="app.sdk.network",
        introduced="v3.0.0",
        owner="adapters",
    ),
    "app.utils.jieba": ModuleAlias(
        target="app.foundation.text",
        replacement="app.sdk.utilities",
        introduced="v3.0.0",
        owner="foundation",
    ),
    "app.utils.object": ModuleAlias(
        target="app.foundation.reflection",
        replacement="app.sdk.utilities",
        introduced="v3.0.0",
        owner="foundation",
    ),
    "app.utils.otp": ModuleAlias(
        target="app.application.security.otp",
        replacement="app.sdk.utilities",
        introduced="v3.0.0",
        owner="application",
    ),
    "app.utils.singleton": ModuleAlias(
        target="app.foundation.singleton",
        replacement="app.sdk.utilities",
        introduced="v3.0.0",
        owner="foundation",
    ),
    "app.utils.structures": ModuleAlias(
        target="app.foundation.collections",
        replacement="app.foundation.collections",
        introduced="v3.0.0",
        owner="foundation",
    ),
    "app.utils.timer": ModuleAlias(
        target="app.runtime.scheduling",
        replacement="app.sdk.utilities",
        introduced="v3.0.0",
        owner="runtime",
    ),
    "app.utils.tokens": ModuleAlias(
        target="app.domain.tokens",
        replacement="app.sdk.media",
        introduced="v3.0.0",
        owner="domain",
    ),
    "app.utils.zhconv": ModuleAlias(
        target="app.foundation.text",
        replacement="app.foundation.text",
        introduced="v3.0.0",
        owner="foundation",
    ),
    "app.utils.stdio": ModuleAlias(
        target="app.adapters.system.stdio",
        replacement="app.adapters.system.stdio",
        introduced="v3.0.0",
        owner="adapters",
    ),
    "app.utils.system": ModuleAlias(
        target="app.adapters.system.host",
        replacement="app.sdk.utilities",
        introduced="v3.0.0",
        owner="adapters",
    ),
    "app.utils.coalesce": ModuleAlias(
        target="app.runtime.coalesce",
        replacement="app.runtime.coalesce",
        introduced="v3.0.0",
        owner="runtime",
    ),
    "app.utils.common": ModuleAlias(
        target="app.sdk.utilities",
        replacement="app.sdk.utilities",
        introduced="v3.0.0",
        owner="sdk",
    ),
    # 防抖器只为旧插件而存在，宿主零调用方，canonical 侧无对应实现，故驻留兼容面。
    "app.utils.debounce": ModuleAlias(
        target="app.runtime.compat.debounce",
        replacement="app.runtime.compat.debounce",
        introduced="v3.0.0",
        owner="runtime",
    ),
    "app.utils.gc": ModuleAlias(
        target="app.runtime.gc",
        replacement="app.runtime.gc",
        introduced="v3.0.0",
        owner="runtime",
    ),
    "app.utils.http": ModuleAlias(
        target="app.adapters.network.http",
        replacement="app.sdk.network",
        introduced="v3.0.0",
        owner="foundation",
    ),
    "app.utils.limit": ModuleAlias(
        target="app.runtime.rate",
        replacement="app.runtime.rate",
        introduced="v3.0.0",
        owner="runtime",
    ),
    "app.utils.media": ModuleAlias(
        target="app.sdk.media",
        replacement="app.sdk.media",
        introduced="v3.0.0",
        owner="sdk",
    ),
    "app.utils.mixins": ModuleAlias(
        target="app.runtime.reload",
        replacement="app.runtime.reload",
        introduced="v3.0.0",
        owner="runtime",
    ),
    "app.utils.rust_accel": ModuleAlias(
        target="app.adapters.system.rust",
        replacement="app.adapters.system.rust",
        introduced="v3.0.0",
        owner="adapters",
    ),
    "app.utils.security": ModuleAlias(
        target="app.application.security.url",
        replacement="app.sdk.network",
        introduced="v3.0.0",
        owner="application",
    ),
    "app.utils.site": ModuleAlias(
        target="app.domain.site",
        replacement="app.sdk.network",
        introduced="v3.0.0",
        owner="domain",
    ),
    "app.utils.string": ModuleAlias(
        target="app.sdk.string",
        replacement="app.sdk.utilities",
        introduced="v3.0.0",
        owner="sdk",
    ),
    "app.utils.url": ModuleAlias(
        target="app.foundation.url",
        replacement="app.sdk.network",
        introduced="v3.0.0",
        owner="foundation",
    ),
    "app.utils.web": ModuleAlias(
        target="app.adapters.external.location",
        replacement="app.sdk.network",
        introduced="v3.0.0",
        owner="adapters",
    ),
    "app.core.auth": ModuleAlias(
        target="app.application.security.auth",
        replacement="app.application.security.auth",
        introduced="v3.0.0",
        owner="application",
    ),
    "app.core.auth_bridge": ModuleAlias(
        target="app.application.security.auth",
        replacement="app.application.security.auth",
        introduced="v3.0.0",
        owner="application",
    ),
    "app.core.cache": ModuleAlias(
        target="app.sdk.cache",
        replacement="app.sdk.cache",
        introduced="v3.0.0",
        owner="sdk",
    ),
    "app.core.config": ModuleAlias(
        target="app.runtime.config",
        replacement="app.sdk.config",
        introduced="v3.0.0",
        owner="runtime",
    ),
    "app.core.context": ModuleAlias(
        target="app.domain.context",
        replacement="app.sdk.media",
        introduced="v3.0.0",
        owner="domain",
    ),
    "app.core.event": ModuleAlias(
        target="app.runtime.events",
        replacement="app.sdk.events",
        introduced="v3.0.0",
        owner="runtime",
    ),
    "app.core.meta.customization": ModuleAlias(
        target="app.domain.meta.customization",
        replacement="app.sdk.media",
        introduced="v3.0.0",
        owner="domain",
    ),
    "app.core.meta.infopath": ModuleAlias(
        target="app.domain.meta.infopath",
        replacement="app.sdk.media",
        introduced="v3.0.0",
        owner="domain",
    ),
    "app.core.meta.metaanime": ModuleAlias(
        target="app.domain.meta.metaanime",
        replacement="app.sdk.media",
        introduced="v3.0.0",
        owner="domain",
    ),
    "app.core.meta.metabase": ModuleAlias(
        target="app.domain.meta.metabase",
        replacement="app.sdk.media",
        introduced="v3.0.0",
        owner="domain",
    ),
    "app.core.meta.metamusic": ModuleAlias(
        target="app.domain.meta.metamusic",
        replacement="app.sdk.media",
        introduced="v3.0.0",
        owner="domain",
    ),
    "app.core.meta.metavideo": ModuleAlias(
        target="app.domain.meta.metavideo",
        replacement="app.sdk.media",
        introduced="v3.0.0",
        owner="domain",
    ),
    "app.core.meta.releasegroup": ModuleAlias(
        target="app.domain.meta.releasegroup",
        replacement="app.sdk.media",
        introduced="v3.0.0",
        owner="domain",
    ),
    "app.core.meta.streamingplatform": ModuleAlias(
        target="app.domain.meta.streamingplatform",
        replacement="app.sdk.media",
        introduced="v3.0.0",
        owner="domain",
    ),
    "app.core.meta.words": ModuleAlias(
        target="app.domain.meta.words",
        replacement="app.sdk.media",
        introduced="v3.0.0",
        owner="domain",
    ),
    "app.core.metainfo": ModuleAlias(
        target="app.domain.metainfo",
        replacement="app.sdk.media",
        introduced="v3.0.0",
        owner="domain",
    ),
    "app.core.module": ModuleAlias(
        target="app.runtime.extensions.module_manager",
        replacement="app.sdk.plugins",
        introduced="v3.0.0",
        owner="runtime",
    ),
    "app.core.plugin": ModuleAlias(
        target="app.runtime.extensions.plugin_manager",
        replacement="app.sdk.plugins",
        introduced="v3.0.0",
        owner="runtime",
    ),
    "app.core.security": ModuleAlias(
        target="app.sdk.security",
        replacement="app.sdk.security",
        introduced="v3.0.0",
        owner="sdk",
    ),
    "app.helper.agent": ModuleAlias(
        target="app.application.messaging.agent", replacement="app.application.messaging.agent",
        introduced="v3.0.0", owner="application",
    ),
    "app.helper.audio": ModuleAlias(
        target="app.application.audio", replacement="app.application.audio",
        introduced="v3.0.0", owner="application",
    ),
    "app.helper.browser": ModuleAlias(
        target="app.adapters.network.browser", replacement="app.adapters.network.browser",
        introduced="v3.0.0", owner="adapters",
    ),
    "app.helper.cloudflare": ModuleAlias(
        target="app.adapters.network.cloudflare", replacement="app.adapters.network.cloudflare",
        introduced="v3.0.0", owner="adapters",
    ),
    "app.helper.cookie": ModuleAlias(
        target="app.application.security.cookie", replacement="app.application.security.cookie",
        introduced="v3.0.0", owner="application",
    ),
    "app.helper.cookiecloud": ModuleAlias(
        target="app.adapters.external.cookiecloud", replacement="app.adapters.external.cookiecloud",
        introduced="v3.0.0", owner="adapters",
    ),
    "app.helper.directory": ModuleAlias(
        target="app.application.directory", replacement="app.application.directory",
        introduced="v3.0.0", owner="application",
    ),
    "app.helper.display": ModuleAlias(
        target="app.adapters.system.display", replacement="app.adapters.system.display",
        introduced="v3.0.0", owner="adapters",
    ),
    "app.helper.doh": ModuleAlias(
        target="app.adapters.network.doh", replacement="app.adapters.network.doh",
        introduced="v3.0.0", owner="adapters",
    ),
    "app.helper.downloader": ModuleAlias(
        target="app.application.downloader", replacement="app.sdk.services",
        introduced="v3.0.0", owner="application",
    ),
    "app.helper.format": ModuleAlias(
        target="app.application.formatting", replacement="app.application.formatting",
        introduced="v3.0.0", owner="application",
    ),
    "app.helper.image": ModuleAlias(
        target="app.application.image", replacement="app.application.image",
        introduced="v3.0.0", owner="application",
    ),
    "app.helper.interaction": ModuleAlias(
        target="app.application.messaging.interaction", replacement="app.application.messaging.interaction",
        introduced="v3.0.0", owner="application",
    ),
    "app.helper.locale": ModuleAlias(
        target="app.runtime.localization", replacement="app.sdk.utilities",
        introduced="v3.0.0", owner="runtime",
    ),
    "app.helper.market": ModuleAlias(
        target="app.adapters.external.market",
        replacement="app.adapters.external.market",
        introduced="v3.0.0", owner="adapters",
    ),
    "app.helper.mediaserver": ModuleAlias(
        target="app.application.mediaserver", replacement="app.sdk.services",
        introduced="v3.0.0", owner="application",
    ),
    "app.helper.message": ModuleAlias(
        target="app.application.messaging.message", replacement="app.application.messaging.message",
        introduced="v3.0.0", owner="application",
    ),
    "app.helper.module": ModuleAlias(
        target="app.foundation.reflection", replacement="app.foundation.reflection",
        introduced="v3.0.0", owner="foundation",
    ),
    "app.helper.nfo": ModuleAlias(
        target="app.domain.scraper", replacement="app.sdk.media",
        introduced="v3.0.0", owner="domain",
    ),
    "app.helper.notification": ModuleAlias(
        target="app.application.notification", replacement="app.sdk.services",
        introduced="v3.0.0", owner="application",
    ),
    "app.helper.ocr": ModuleAlias(
        target="app.adapters.external.ocr", replacement="app.adapters.external.ocr",
        introduced="v3.0.0", owner="adapters",
    ),
    "app.helper.package": ModuleAlias(
        target="app.adapters.system.package",
        replacement="app.adapters.system.package",
        introduced="v3.0.0", owner="adapters",
    ),
    "app.helper.passkey": ModuleAlias(
        target="app.application.security.passkey", replacement="app.application.security.passkey",
        introduced="v3.0.0", owner="application",
    ),
    "app.helper.plugin": ModuleAlias(
        target="app.adapters.external.market",
        replacement="app.adapters.external.market",
        introduced="v3.0.0", owner="adapters",
    ),
    "app.helper.progress": ModuleAlias(
        target="app.runtime.progress", replacement="app.runtime.progress",
        introduced="v3.0.0", owner="runtime",
    ),
    "app.helper.redis": ModuleAlias(
        target="app.adapters.cache.redis", replacement="app.adapters.cache.redis",
        introduced="v3.0.0", owner="adapters",
    ),
    "app.helper.resource": ModuleAlias(
        target="app.adapters.system.resource",
        replacement="app.adapters.system.resource",
        introduced="v3.0.0", owner="adapters",
    ),
    "app.helper.rss": ModuleAlias(
        target="app.application.rss", replacement="app.sdk.network",
        introduced="v3.0.0", owner="application",
    ),
    "app.helper.rule": ModuleAlias(
        target="app.application.rules", replacement="app.sdk.services",
        introduced="v3.0.0", owner="application",
    ),
    "app.helper.scraper": ModuleAlias(
        target="app.domain.scraper", replacement="app.domain.scraper",
        introduced="v3.0.0", owner="domain",
    ),
    "app.helper.server": ModuleAlias(
        target="app.adapters.external.server", replacement="app.adapters.external.server",
        introduced="v3.0.0", owner="adapters",
    ),
    "app.helper.service": ModuleAlias(
        target="app.runtime.extensions.service_registry",
        replacement="app.sdk.services",
        introduced="v3.0.0", owner="runtime",
    ),
    "app.helper.sites": ModuleAlias(
        target="app.application.site.sites", replacement="app.sdk.network",
        introduced="v3.0.0", owner="application",
    ),
    "app.helper.skill": ModuleAlias(
        target="app.agent.skills.registry",
        replacement="app.agent.skills.registry",
        introduced="v3.0.0", owner="agent",
    ),
    "app.helper.storage": ModuleAlias(
        target="app.application.storage", replacement="app.sdk.services",
        introduced="v3.0.0", owner="application",
    ),
    "app.helper.system": ModuleAlias(
        target="app.runtime.state", replacement="app.sdk.services",
        introduced="v3.0.0", owner="runtime",
    ),
    "app.helper.thread": ModuleAlias(
        target="app.runtime.thread", replacement="app.runtime.thread",
        introduced="v3.0.0", owner="runtime",
    ),
    "app.helper.torrent": ModuleAlias(
        target="app.application.torrent", replacement="app.application.torrent",
        introduced="v3.0.0", owner="application",
    ),
    "app.helper.transferhistory": ModuleAlias(
        target="app.application.history",
        replacement="app.application.history",
        introduced="v3.0.0", owner="application",
    ),
    "app.helper.twofa": ModuleAlias(
        target="app.application.security.twofactor", replacement="app.application.security.twofactor",
        introduced="v3.0.0", owner="application",
    ),
    "app.helper.webpush": ModuleAlias(
        target="app.api.endpoints.message", replacement="app.api.endpoints.message",
        introduced="v3.0.0", owner="api",
    ),
    "app.helper.wallpaper": ModuleAlias(
        target="app.application.image", replacement="app.application.image",
        introduced="v3.0.0", owner="application",
    ),
    "app.helper.llm": ModuleAlias(
        target="app.agent.llm", replacement="app.agent.llm",
        introduced="v3.0.0", owner="agent", is_package=True,
    ),
}

# 需要合成包级符号的旧路径单独登记；它们不是 canonical 模块别名。
PACKAGE_ALIASES: Dict[str, ModuleAlias] = {
    "app.core.meta": ModuleAlias(
        target="app.domain.meta",
        replacement="app.sdk.media",
        introduced="v3.0.0",
        owner="domain",
        is_package=True,
    ),
    "app.modules.filemanager.storages": ModuleAlias(
        target="app.modules._base.storage",
        replacement="app.modules._base.storage",
        introduced="v3.0.0",
        owner="modules",
        is_package=True,
    ),
}

# 旧父包完全迁空后才登记；迁移中的物理父包继续由 PathFinder 处理。
VIRTUAL_PACKAGES: Set[str] = {
    "app.chain",
    "app.core",
    "app.helper",
    "app.modules.filemanager.storages",
    "app.utils",
}

# 旧包 __init__.py 曾公开的符号在这里显式声明，禁止模糊转发。
PACKAGE_EXPORTS: Dict[str, Dict[str, SymbolAlias]] = {
    "app.core.meta": {
        "MetaBase": SymbolAlias(
            target_module="app.domain.meta.metabase",
            target_name="MetaBase",
            replacement="app.sdk.media.MetaBase",
        ),
        "MetaVideo": SymbolAlias(
            target_module="app.domain.meta.metavideo",
            target_name="MetaVideo",
            replacement="app.sdk.media.MetaVideo",
        ),
        "MetaAnime": SymbolAlias(
            target_module="app.domain.meta.metaanime",
            target_name="MetaAnime",
            replacement="app.sdk.media.MetaAnime",
        ),
        "MetaMusic": SymbolAlias(
            target_module="app.domain.meta.metamusic",
            target_name="MetaMusic",
            replacement="app.sdk.media.MetaMusic",
        ),
        "MusicNameContext": SymbolAlias(
            target_module="app.domain.meta.metamusic",
            target_name="MusicNameContext",
            replacement="app.sdk.media.MusicNameContext",
        ),
        "MusicNameParseResult": SymbolAlias(
            target_module="app.domain.meta.metamusic",
            target_name="MusicNameParseResult",
            replacement="app.sdk.media.MusicNameParseResult",
        ),
        "MusicNameParser": SymbolAlias(
            target_module="app.domain.meta.metamusic",
            target_name="MusicNameParser",
            replacement="app.sdk.media.MusicNameParser",
        ),
        "MusicNamePattern": SymbolAlias(
            target_module="app.domain.meta.metamusic",
            target_name="MusicNamePattern",
            replacement="app.sdk.media.MusicNamePattern",
        ),
        "MusicNamePatternMatch": SymbolAlias(
            target_module="app.domain.meta.metamusic",
            target_name="MusicNamePatternMatch",
            replacement="app.sdk.media.MusicNamePatternMatch",
        ),
        "MusicNameRegistry": SymbolAlias(
            target_module="app.domain.meta.metamusic",
            target_name="MusicNameRegistry",
            replacement="app.sdk.media.MusicNameRegistry",
        ),
    },
    "app.modules.filemanager.storages": {
        "StorageBase": SymbolAlias(
            target_module="app.modules._base.storage",
            target_name="StorageBase",
            replacement="app.modules._base.storage.StorageBase",
        ),
        "transfer_process": SymbolAlias(
            target_module="app.modules._base.storage",
            target_name="transfer_process",
            replacement="app.modules._base.storage.transfer_process",
        ),
    },
}

# 物理模块仍存在、仅部分公开符号迁走时，由导入器在标准 Loader 执行后叠加惰性符号路由。
# canonical 源码不反向依赖兼容层，目标符号也只在旧调用方真正取用时加载。

# message/notification 命名统一后的旧符号映射：
# 通知渠道能力归 notification（NotificationChannel），消息收发归 message（Message/MessageType）。
# 旧名在 app.schemas、app.schemas.message 两个入口都曾公开，共用同一份映射。
_MESSAGE_NOTIFICATION_SYMBOL_ALIASES: Dict[str, SymbolAlias] = {
    "MessageChannel": SymbolAlias(
        target_module="app.schemas.types",
        target_name="NotificationChannel",
        replacement="app.schemas.types.NotificationChannel",
    ),
    "NotificationType": SymbolAlias(
        target_module="app.schemas.types",
        target_name="MessageType",
        replacement="app.schemas.types.MessageType",
    ),
    "Notification": SymbolAlias(
        target_module="app.schemas.message",
        target_name="Message",
        replacement="app.schemas.message.Message",
    ),
    "CommingMessage": SymbolAlias(
        target_module="app.schemas.message",
        target_name="IncomingMessage",
        replacement="app.schemas.message.IncomingMessage",
    ),
    "NotificationHistoryItem": SymbolAlias(
        target_module="app.schemas.message",
        target_name="MessageHistoryItem",
        replacement="app.schemas.message.MessageHistoryItem",
    ),
    **{
        old: SymbolAlias(
            target_module="app.schemas.message",
            target_name=new,
            replacement=f"app.schemas.message.{new}",
        )
        for old, new in (
            ("NotificationClearScope", "MessageClearScope"),
            ("NotificationClearBefore", "MessageClearBefore"),
            ("NotificationClearData", "MessageClearData"),
        )
    },
    **{
        name: SymbolAlias(
            target_module="app.schemas.notification",
            target_name=name,
            replacement=f"app.schemas.notification.{name}",
        )
        for name in (
            "ChannelCapability",
            "ChannelCapabilities",
            "ChannelCapabilityManager",
        )
    },
}

SYMBOL_ALIASES: Dict[str, Dict[str, SymbolAlias]] = {
    # app.plugins 是扩展的安装挂载点而不是宿主包：它要能被容器卷整体覆盖，因此目录里
    # 没有 __init__.py，是个命名空间包。存量扩展写的 from app.plugins import _PluginBase
    # 由这里解析，目录被覆盖也不影响。
    "app.plugins": {
        "_PluginBase": SymbolAlias(
            target_module="app.sdk.extension",
            target_name="_PluginBase",
            replacement="app.sdk.extension._PluginBase",
        ),
        "PluginChian": SymbolAlias(
            target_module="app.sdk.extension",
            target_name="PluginChian",
            replacement="app.sdk.extension.PluginChian",
        ),
        "plugin_instance_path": SymbolAlias(
            target_module="app.runtime.extensions.lifecycle.paths",
            target_name="plugin_instance_path",
            replacement="app.runtime.extensions.lifecycle.paths.plugin_instance_path",
        ),
    },
    "app.agent.orchestrator": {
        "AgentChain": SymbolAlias(
            target_module="app.application.orchestration.agent",
            target_name="AgentChain",
            replacement="app.application.orchestration.agent.AgentChain",
        ),
        "ReplyMode": SymbolAlias(
            target_module="app.schemas.types",
            target_name="ReplyMode",
            replacement="app.schemas.types.ReplyMode",
        ),
    },
    # 刮削能力从 MediaChain 拆出为独立 ScrapingChain 后，
    # media 模块级公开的刮削选项与策略配置由 scraping 模块提供
    "app.application.orchestration.media": {
        name: SymbolAlias(
            target_module="app.application.orchestration.scraping",
            target_name=name,
            replacement=f"app.application.orchestration.scraping.{name}",
        )
        for name in ("ScrapingChain", "ScrapingOption", "ScrapingConfig")
    },
    "app.application.orchestration.message": {
        "MediaInteractionChain": SymbolAlias(
            target_module="app.application.orchestration.interaction",
            target_name="MediaInteractionChain",
            replacement="app.application.orchestration.interaction.MediaInteractionChain",
        ),
    },
    "app.domain.media": {
        name: SymbolAlias(
            target_module="app.schemas.media",
            target_name=name,
            replacement=f"app.schemas.media.{name}",
        )
        for name in (
            "MEDIA_SOURCE_ALIASES",
            "MEDIA_SOURCE_PREFIXES",
            "normalize_media_source",
            "parse_media_key",
            "resolve_media_identity",
            "normalize_media_identity_payload",
            "build_media_key",
        )
    },
    # 媒体库文件系统模块的能力类名为 MediaLibraryModule；
    # 整理编排落在 app/application/transferhandler.py，本模块不持有该类
    "app.modules.medialibrary": {
        "FileManagerModule": SymbolAlias(
            target_module="app.modules.medialibrary",
            target_name="MediaLibraryModule",
            replacement="app.modules.medialibrary.MediaLibraryModule",
        ),
        "TransHandler": SymbolAlias(
            target_module="app.application.transferhandler",
            target_name="TransHandler",
            replacement="app.application.transferhandler.TransHandler",
        ),
    },
    "app.schemas": {
        **{
            name: SymbolAlias(
                target_module="app.sdk._legacy.transfer",
                target_name=name,
                replacement=f"app.application.transfer.{name}",
            )
            for name in ("TransferTask", "TransferQueue")
        },
        **_MESSAGE_NOTIFICATION_SYMBOL_ALIASES,
    },
    "app.schemas.transfer": {
        **{
            name: SymbolAlias(
                target_module="app.sdk._legacy.transfer",
                target_name=name,
                replacement=f"app.application.transfer.{name}",
            )
            for name in ("TransferTask", "TransferQueue")
        },
        "DownloadHistory": SymbolAlias(
            target_module="app.schemas.history",
            target_name="DownloadHistory",
            replacement="app.schemas.history.DownloadHistory",
        ),
        "TransferDirectoryConf": SymbolAlias(
            target_module="app.schemas.system",
            target_name="TransferDirectoryConf",
            replacement="app.schemas.system.TransferDirectoryConf",
        ),
        "TmdbEpisode": SymbolAlias(
            target_module="app.schemas.tmdb",
            target_name="TmdbEpisode",
            replacement="app.schemas.tmdb.TmdbEpisode",
        ),
        "MediaType": SymbolAlias(
            target_module="app.schemas.types",
            target_name="MediaType",
            replacement="app.schemas.types.MediaType",
        ),
    },
    "app.schemas.agent": {
        "ReplyMode": SymbolAlias(
            target_module="app.schemas.types",
            target_name="ReplyMode",
            replacement="app.schemas.types.ReplyMode",
        ),
    },
    "app.sdk.logging": {
        name: SymbolAlias(
            target_module="app.runtime.log",
            target_name=name,
            replacement=f"app.runtime.log.{name}",
        )
        for name in (
            "CustomFormatter",
            "LogConfigModel",
            "LogEntry",
            "LogSettings",
            "LoggerManager",
            "NonBlockingFileHandler",
            "configure_log_settings",
            "configure_log_writer",
            "log_settings",
        )
    },
    # message/notification 命名统一：通知渠道能力归 notification，消息收发归 message
    "app.schemas.types": {
        "MessageChannel": SymbolAlias(
            target_module="app.schemas.types",
            target_name="NotificationChannel",
            replacement="app.schemas.types.NotificationChannel",
        ),
        "NotificationType": SymbolAlias(
            target_module="app.schemas.types",
            target_name="MessageType",
            replacement="app.schemas.types.MessageType",
        ),
    },
    "app.schemas.message": _MESSAGE_NOTIFICATION_SYMBOL_ALIASES,
}
