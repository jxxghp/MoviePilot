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


# 只登记已经删除旧物理源码、并完成 canonical 路径验证的模块。
MODULE_ALIASES: Dict[str, ModuleAlias] = {
    "app.log": ModuleAlias(
        target="app.sdk.logging",
        replacement="app.sdk.logging",
        introduced="v3.0.0",
        owner="sdk",
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
        target="app.infrastructure.network",
        replacement="app.sdk.network",
        introduced="v3.0.0",
        owner="infrastructure",
    ),
    "app.utils.jieba": ModuleAlias(
        target="app.foundation.jieba",
        replacement="app.sdk.utilities",
        introduced="v3.0.0",
        owner="foundation",
    ),
    "app.utils.object": ModuleAlias(
        target="app.foundation.object",
        replacement="app.sdk.utilities",
        introduced="v3.0.0",
        owner="foundation",
    ),
    "app.utils.otp": ModuleAlias(
        target="app.security.otp",
        replacement="app.sdk.utilities",
        introduced="v3.0.0",
        owner="security",
    ),
    "app.utils.singleton": ModuleAlias(
        target="app.foundation.singleton",
        replacement="app.sdk.utilities",
        introduced="v3.0.0",
        owner="foundation",
    ),
    "app.utils.structures": ModuleAlias(
        target="app.foundation.structures",
        replacement="app.foundation.structures",
        introduced="v3.0.0",
        owner="foundation",
    ),
    "app.utils.timer": ModuleAlias(
        target="app.platform.scheduling",
        replacement="app.sdk.utilities",
        introduced="v3.0.0",
        owner="platform",
    ),
    "app.utils.tokens": ModuleAlias(
        target="app.domain.tokens",
        replacement="app.sdk.media",
        introduced="v3.0.0",
        owner="domain",
    ),
    "app.utils.zhconv": ModuleAlias(
        target="app.foundation.zhconv",
        replacement="app.foundation.zhconv",
        introduced="v3.0.0",
        owner="foundation",
    ),
    "app.utils.stdio": ModuleAlias(
        target="app.infrastructure.stdio",
        replacement="app.infrastructure.stdio",
        introduced="v3.0.0",
        owner="infrastructure",
    ),
    "app.utils.system": ModuleAlias(
        target="app.infrastructure.system",
        replacement="app.sdk.utilities",
        introduced="v3.0.0",
        owner="infrastructure",
    ),
    "app.utils.coalesce": ModuleAlias(
        target="app.platform.coalesce",
        replacement="app.platform.coalesce",
        introduced="v3.0.0",
        owner="platform",
    ),
    "app.utils.common": ModuleAlias(
        target="app.sdk.utilities",
        replacement="app.sdk.utilities",
        introduced="v3.0.0",
        owner="sdk",
    ),
    "app.utils.debounce": ModuleAlias(
        target="app.platform.debounce",
        replacement="app.platform.debounce",
        introduced="v3.0.0",
        owner="platform",
    ),
    "app.utils.gc": ModuleAlias(
        target="app.platform.gc",
        replacement="app.platform.gc",
        introduced="v3.0.0",
        owner="platform",
    ),
    "app.utils.http": ModuleAlias(
        target="app.foundation.http",
        replacement="app.sdk.network",
        introduced="v3.0.0",
        owner="foundation",
    ),
    "app.utils.limit": ModuleAlias(
        target="app.platform.rate",
        replacement="app.platform.rate",
        introduced="v3.0.0",
        owner="platform",
    ),
    "app.utils.media": ModuleAlias(
        target="app.domain.media",
        replacement="app.domain.media",
        introduced="v3.0.0",
        owner="domain",
    ),
    "app.utils.mixins": ModuleAlias(
        target="app.platform.reload",
        replacement="app.platform.reload",
        introduced="v3.0.0",
        owner="platform",
    ),
    "app.utils.rust_accel": ModuleAlias(
        target="app.infrastructure.rust",
        replacement="app.infrastructure.rust",
        introduced="v3.0.0",
        owner="infrastructure",
    ),
    "app.utils.security": ModuleAlias(
        target="app.security.url",
        replacement="app.sdk.network",
        introduced="v3.0.0",
        owner="security",
    ),
    "app.utils.site": ModuleAlias(
        target="app.domain.site",
        replacement="app.sdk.network",
        introduced="v3.0.0",
        owner="domain",
    ),
    "app.utils.string": ModuleAlias(
        target="app.domain.string",
        replacement="app.sdk.utilities",
        introduced="v3.0.0",
        owner="domain",
    ),
    "app.utils.url": ModuleAlias(
        target="app.foundation.url",
        replacement="app.sdk.network",
        introduced="v3.0.0",
        owner="foundation",
    ),
    "app.utils.web": ModuleAlias(
        target="app.integrations.location",
        replacement="app.sdk.network",
        introduced="v3.0.0",
        owner="integrations",
    ),
    "app.core.auth": ModuleAlias(
        target="app.security.auth",
        replacement="app.security.auth",
        introduced="v3.0.0",
        owner="security",
    ),
    "app.core.auth_bridge": ModuleAlias(
        target="app.security.auth",
        replacement="app.security.auth",
        introduced="v3.0.0",
        owner="security",
    ),
    "app.core.cache": ModuleAlias(
        target="app.sdk.cache",
        replacement="app.sdk.cache",
        introduced="v3.0.0",
        owner="sdk",
    ),
    "app.core.config": ModuleAlias(
        target="app.platform.config",
        replacement="app.sdk.config",
        introduced="v3.0.0",
        owner="platform",
    ),
    "app.core.context": ModuleAlias(
        target="app.domain.context",
        replacement="app.sdk.media",
        introduced="v3.0.0",
        owner="domain",
    ),
    "app.core.event": ModuleAlias(
        target="app.platform.events",
        replacement="app.sdk.events",
        introduced="v3.0.0",
        owner="platform",
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
        target="app.extensions.module_manager",
        replacement="app.sdk.plugins",
        introduced="v3.0.0",
        owner="extensions",
    ),
    "app.core.plugin": ModuleAlias(
        target="app.extensions.plugin_manager",
        replacement="app.sdk.plugins",
        introduced="v3.0.0",
        owner="extensions",
    ),
    "app.core.security": ModuleAlias(
        target="app.security.access",
        replacement="app.security.access",
        introduced="v3.0.0",
        owner="security",
    ),
    "app.helper.agent": ModuleAlias(
        target="app.messaging.agent", replacement="app.messaging.agent",
        introduced="v3.0.0", owner="messaging",
    ),
    "app.helper.audio": ModuleAlias(
        target="app.services.audio", replacement="app.services.audio",
        introduced="v3.0.0", owner="services",
    ),
    "app.helper.browser": ModuleAlias(
        target="app.infrastructure.browser", replacement="app.infrastructure.browser",
        introduced="v3.0.0", owner="infrastructure",
    ),
    "app.helper.cloudflare": ModuleAlias(
        target="app.infrastructure.cloudflare", replacement="app.infrastructure.cloudflare",
        introduced="v3.0.0", owner="infrastructure",
    ),
    "app.helper.cookie": ModuleAlias(
        target="app.security.cookie", replacement="app.security.cookie",
        introduced="v3.0.0", owner="security",
    ),
    "app.helper.cookiecloud": ModuleAlias(
        target="app.integrations.cookiecloud", replacement="app.integrations.cookiecloud",
        introduced="v3.0.0", owner="integrations",
    ),
    "app.helper.directory": ModuleAlias(
        target="app.services.directory", replacement="app.services.directory",
        introduced="v3.0.0", owner="services",
    ),
    "app.helper.display": ModuleAlias(
        target="app.infrastructure.display", replacement="app.infrastructure.display",
        introduced="v3.0.0", owner="infrastructure",
    ),
    "app.helper.doh": ModuleAlias(
        target="app.infrastructure.doh", replacement="app.infrastructure.doh",
        introduced="v3.0.0", owner="infrastructure",
    ),
    "app.helper.downloader": ModuleAlias(
        target="app.services.downloader", replacement="app.sdk.services",
        introduced="v3.0.0", owner="services",
    ),
    "app.helper.format": ModuleAlias(
        target="app.services.formatting", replacement="app.services.formatting",
        introduced="v3.0.0", owner="services",
    ),
    "app.helper.image": ModuleAlias(
        target="app.services.image", replacement="app.services.image",
        introduced="v3.0.0", owner="services",
    ),
    "app.helper.interaction": ModuleAlias(
        target="app.messaging.interaction", replacement="app.messaging.interaction",
        introduced="v3.0.0", owner="messaging",
    ),
    "app.helper.locale": ModuleAlias(
        target="app.platform.localization", replacement="app.sdk.utilities",
        introduced="v3.0.0", owner="platform",
    ),
    "app.helper.market": ModuleAlias(
        target="app.integrations.market",
        replacement="app.integrations.market",
        introduced="v3.0.0", owner="integrations",
    ),
    "app.helper.mediaserver": ModuleAlias(
        target="app.services.mediaserver", replacement="app.sdk.services",
        introduced="v3.0.0", owner="services",
    ),
    "app.helper.message": ModuleAlias(
        target="app.messaging.message", replacement="app.messaging.message",
        introduced="v3.0.0", owner="messaging",
    ),
    "app.helper.module": ModuleAlias(
        target="app.foundation.module", replacement="app.foundation.module",
        introduced="v3.0.0", owner="foundation",
    ),
    "app.helper.nfo": ModuleAlias(
        target="app.domain.scraper", replacement="app.sdk.media",
        introduced="v3.0.0", owner="domain",
    ),
    "app.helper.notification": ModuleAlias(
        target="app.services.notification", replacement="app.sdk.services",
        introduced="v3.0.0", owner="services",
    ),
    "app.helper.ocr": ModuleAlias(
        target="app.integrations.ocr", replacement="app.integrations.ocr",
        introduced="v3.0.0", owner="integrations",
    ),
    "app.helper.package": ModuleAlias(
        target="app.infrastructure.package",
        replacement="app.infrastructure.package",
        introduced="v3.0.0", owner="infrastructure",
    ),
    "app.helper.passkey": ModuleAlias(
        target="app.security.passkey", replacement="app.security.passkey",
        introduced="v3.0.0", owner="security",
    ),
    "app.helper.plugin": ModuleAlias(
        target="app.integrations.market",
        replacement="app.integrations.market",
        introduced="v3.0.0", owner="integrations",
    ),
    "app.helper.progress": ModuleAlias(
        target="app.platform.progress", replacement="app.platform.progress",
        introduced="v3.0.0", owner="platform",
    ),
    "app.helper.redis": ModuleAlias(
        target="app.infrastructure.redis", replacement="app.infrastructure.redis",
        introduced="v3.0.0", owner="infrastructure",
    ),
    "app.helper.resource": ModuleAlias(
        target="app.infrastructure.resource",
        replacement="app.infrastructure.resource",
        introduced="v3.0.0", owner="infrastructure",
    ),
    "app.helper.rss": ModuleAlias(
        target="app.infrastructure.rss", replacement="app.infrastructure.rss",
        introduced="v3.0.0", owner="infrastructure",
    ),
    "app.helper.rule": ModuleAlias(
        target="app.services.filter", replacement="app.sdk.services",
        introduced="v3.0.0", owner="services",
    ),
    "app.helper.scraper": ModuleAlias(
        target="app.domain.scraper", replacement="app.domain.scraper",
        introduced="v3.0.0", owner="domain",
    ),
    "app.helper.server": ModuleAlias(
        target="app.integrations.server", replacement="app.integrations.server",
        introduced="v3.0.0", owner="integrations",
    ),
    "app.helper.service": ModuleAlias(
        target="app.extensions.service_registry",
        replacement="app.sdk.services",
        introduced="v3.0.0", owner="extensions",
    ),
    "app.helper.sites": ModuleAlias(
        target="app.infrastructure.sites", replacement="app.infrastructure.sites",
        introduced="v3.0.0", owner="infrastructure",
    ),
    "app.helper.skill": ModuleAlias(
        target="app.agent.skills.registry",
        replacement="app.agent.skills.registry",
        introduced="v3.0.0", owner="agent",
    ),
    "app.helper.storage": ModuleAlias(
        target="app.services.storage", replacement="app.sdk.services",
        introduced="v3.0.0", owner="services",
    ),
    "app.helper.system": ModuleAlias(
        target="app.platform.runtime", replacement="app.sdk.services",
        introduced="v3.0.0", owner="platform",
    ),
    "app.helper.thread": ModuleAlias(
        target="app.platform.thread", replacement="app.platform.thread",
        introduced="v3.0.0", owner="platform",
    ),
    "app.helper.torrent": ModuleAlias(
        target="app.services.torrent", replacement="app.services.torrent",
        introduced="v3.0.0", owner="services",
    ),
    "app.helper.transferhistory": ModuleAlias(
        target="app.services.history",
        replacement="app.services.history",
        introduced="v3.0.0", owner="services",
    ),
    "app.helper.twofa": ModuleAlias(
        target="app.security.twofactor", replacement="app.security.twofactor",
        introduced="v3.0.0", owner="security",
    ),
    "app.helper.webpush": ModuleAlias(
        target="app.api.endpoints.message", replacement="app.api.endpoints.message",
        introduced="v3.0.0", owner="api",
    ),
    "app.helper.wallpaper": ModuleAlias(
        target="app.services.image", replacement="app.services.image",
        introduced="v3.0.0", owner="services",
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
}

# 旧父包完全迁空后才登记；迁移中的物理父包继续由 PathFinder 处理。
VIRTUAL_PACKAGES: Set[str] = {"app.core", "app.helper", "app.utils"}

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
}
