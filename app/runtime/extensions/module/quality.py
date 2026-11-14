"""
宿主 Module 的渐进式集成质量清单。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ModuleQualityLevel(StrEnum):
    """
    描述模块当前接受的质量门禁等级。
    """

    # 保留历史实现，按已登记豁免约束维护
    LEGACY = "legacy"
    # 已依据登记规则完成质量评估
    ASSESSED = "assessed"


@dataclass(frozen=True, slots=True)
class ModuleQualityProfile:
    """
    记录一个模块已验证规则、维护者和精确豁免原因。

    :param module: 被评估的模块名称
    :param level: 当前接受的质量门禁等级
    :param owner: 负责维护评估结论的责任方
    :param verified_rules: 已经验证符合的规则标识集合
    :param exemption_reason: 精确豁免原因；没有豁免时为 None
    """

    module: str
    level: ModuleQualityLevel
    owner: str
    verified_rules: frozenset[str] = frozenset()
    exemption_reason: str | None = None


QUALITY_RULES = frozenset(
    {
        "fake-client-or-fixture",
        "zero-real-network-tests",
        "sync-async-boundary",
        "no-blocking-io-in-event-loop",
        "auth-rate-timeout-offline-semantics",
        "bounded-concurrency-or-polling",
        "reload-stop-idempotent",
        "module-contract-v2",
        "sensitive-log-redaction",
        "owner-declared",
    }
)

COMMON_ASSESSED_RULES = frozenset(
    {
        "zero-real-network-tests",
        "no-blocking-io-in-event-loop",
        "module-contract-v2",
        "owner-declared",
    }
)

# 这些模块均已纳入全局真实网络守卫、async 阻塞扫描和 V2 方法契约门禁。
# 模块专属的鉴权、限流、并发和生命周期规则仍按 profile 中的精确豁免逐项补强，
# 但不再使用无法区分“未审查”和“已审查有缺口”的 legacy 状态。
BASELINE_ASSESSED_MODULES = frozenset(
    {
        "acoustid",
        "amll",
        "anilist",
        "discord",
        "douban",
        "emby",
        "fanart",
        "feishu",
        "filemanager",
        "filter",
        "imdb",
        "indexer",
        "jellyfin",
        "listenbrainz",
        "lrclib",
        "vyo",
        "musicbrainz",
        "navidrome",
        "plex",
        "postgresql",
        "qbittorrent",
        "qqbot",
        "redis",
        "rtorrent",
        "slack",
        "subtitle",
        "synologychat",
        "telegram",
        "theaudiodb",
        "themoviedb",
        "thetvdb",
        "transmission",
        "trimemedia",
        "ugreen",
        "vocechat",
        "webpush",
        "wechat",
        "wechatclawbot",
        "zspace",
    }
)

MODULE_QUALITY_PROFILES = {
    module: ModuleQualityProfile(
        module=module,
        level=ModuleQualityLevel.ASSESSED,
        owner="MoviePilot core",
        verified_rules=COMMON_ASSESSED_RULES,
        exemption_reason=(
            "已完成宿主通用网络、异步阻塞、V2 方法契约与维护责任门禁；"
            "模块专属鉴权、限流、并发、敏感日志及 reload/stop 语义只在有对应能力时适用，"
            "继续由各模块专项测试证明"
        ),
    )
    for module in BASELINE_ASSESSED_MODULES
}

MODULE_QUALITY_PROFILES.update({
    "apppush": ModuleQualityProfile(
        module="apppush",
        level=ModuleQualityLevel.ASSESSED,
        owner="MoviePilot core",
        verified_rules=COMMON_ASSESSED_RULES | frozenset({
            "fake-client-or-fixture", "sensitive-log-redaction", "reload-stop-idempotent",
            "auth-rate-timeout-offline-semantics", "sync-async-boundary",
        }),
        exemption_reason="同步单向 HTTP 中转，无轮询、长连接或入站回调；设备配额和投递并发由私有 Server 约束",
    ),
    "bangumi": ModuleQualityProfile(
        module="bangumi",
        level=ModuleQualityLevel.ASSESSED,
        owner="MoviePilot core",
        verified_rules=frozenset(
            {
                "fake-client-or-fixture",
                "zero-real-network-tests",
                "sync-async-boundary",
                "reload-stop-idempotent",
                "module-contract-v2",
                "sensitive-log-redaction",
                "owner-declared",
            }
        ),
        exemption_reason=(
            "外部 Bangumi API 的限流与并发策略仍沿用通用 HTTP adapter；"
            "本轮仅对配置快照改动面启用 assessed 门禁"
        ),
    ),
    "dingtalk": ModuleQualityProfile(
        module="dingtalk",
        level=ModuleQualityLevel.ASSESSED,
        owner="MoviePilot core",
        verified_rules=frozenset(
            {
                "fake-client-or-fixture",
                "zero-real-network-tests",
                "reload-stop-idempotent",
                "module-contract-v2",
                "sensitive-log-redaction",
                "owner-declared",
            }
        ),
        exemption_reason=(
            "钉钉自定义机器人仅提供同步出站 Webhook，不包含长连接、轮询或入站回调"
        ),
    ),
})


def get_module_quality_profile(module: str) -> ModuleQualityProfile:
    """
    返回显式 profile；未迁移模块以带原因的 legacy 视图呈现。
    """
    return MODULE_QUALITY_PROFILES.get(
        module,
        ModuleQualityProfile(
            module=module,
            level=ModuleQualityLevel.LEGACY,
            owner="MoviePilot core",
            exemption_reason="存量模块尚未在二阶段任务中修改，按渐进策略暂不提升门禁",
        ),
    )
