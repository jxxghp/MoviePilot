"""宿主 Module 的渐进式集成质量清单。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ModuleQualityLevel(StrEnum):
    """描述模块当前接受的质量门禁等级。"""

    LEGACY = "legacy"
    ASSESSED = "assessed"


@dataclass(frozen=True, slots=True)
class ModuleQualityProfile:
    """记录一个模块已验证规则、维护者和精确豁免原因。"""

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

MODULE_QUALITY_PROFILES = {
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
}


def get_module_quality_profile(module: str) -> ModuleQualityProfile:
    """返回显式 profile；未迁移模块以带原因的 legacy 视图呈现。"""
    return MODULE_QUALITY_PROFILES.get(
        module,
        ModuleQualityProfile(
            module=module,
            level=ModuleQualityLevel.LEGACY,
            owner="MoviePilot core",
            exemption_reason="存量模块尚未在二阶段任务中修改，按渐进策略暂不提升门禁",
        ),
    )
