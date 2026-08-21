"""Module Integration Quality Scale 渐进门禁测试。"""

from pathlib import Path

from app.runtime.extensions.module.quality import (
    MODULE_QUALITY_PROFILES,
    QUALITY_RULES,
    ModuleQualityLevel,
    get_module_quality_profile,
)


MODULE_ROOT = Path(__file__).parents[1] / "app" / "modules"


def test_every_module_has_quality_view_with_owner_and_reason() -> None:
    """所有存量模块都必须能解析为 assessed 或有理由的 legacy profile。"""
    modules = {
        path.name
        for path in MODULE_ROOT.iterdir()
        if path.is_dir() and not path.name.startswith(("_", "."))
    }

    assert modules
    for module in modules:
        profile = get_module_quality_profile(module)
        assert profile.owner
        assert profile.level in ModuleQualityLevel
        if profile.level is ModuleQualityLevel.LEGACY:
            assert profile.exemption_reason


def test_assessed_profiles_only_use_declared_rules() -> None:
    """显式 profile 不得通过拼写新规则绕过统一质量维度。"""
    for profile in MODULE_QUALITY_PROFILES.values():
        assert profile.verified_rules
        assert profile.verified_rules <= QUALITY_RULES


def test_bangumi_changed_slice_meets_required_quality_rules() -> None:
    """本轮修改的 Bangumi 模块必须满足配置快照切片相关门禁。"""
    profile = get_module_quality_profile("bangumi")

    assert profile.level is ModuleQualityLevel.ASSESSED
    assert {
        "fake-client-or-fixture",
        "zero-real-network-tests",
        "reload-stop-idempotent",
        "module-contract-v2",
        "owner-declared",
    } <= profile.verified_rules
