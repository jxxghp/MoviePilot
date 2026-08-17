"""模块字符串方法契约清单的架构测试。"""

import json
from pathlib import Path

from app.runtime.extensions.module.contracts import (
    ModuleResultAggregation,
    get_module_method_contract,
    is_explicit_module_method,
)


RUNTIME_BASELINE = (
    Path(__file__).parent / "fixtures" / "architecture" / "runtime-contract-baseline.json"
)


def test_all_scanned_module_methods_resolve_a_contract() -> None:
    """架构快照中的所有字符串方法都必须能解析到稳定聚合规则。"""
    payload = json.loads(RUNTIME_BASELINE.read_text(encoding="utf-8"))
    methods = payload["run_module"]["methods"]

    assert methods
    for method in methods:
        contract = get_module_method_contract(method)
        assert contract.aggregation is ModuleResultAggregation.LEGACY
        assert contract.plugin_short_circuit is True


def test_high_frequency_capability_families_are_explicit() -> None:
    """媒体发现、识别、存储和消息族不能退回未分类 legacy 契约。"""
    expected_families = {
        "async_tmdb_discover": "tmdb",
        "async_douban_discover": "douban",
        "bangumi_info": "bangumi",
        "anilist_info": "anilist",
        "recognize_media": "media-recognition",
        "mediaserver_items": "media-server",
        "list_files": "storage",
        "finalize_message": "messaging",
        "scheduler_job": "scheduling",
    }

    for method, family in expected_families.items():
        assert is_explicit_module_method(method)
        assert get_module_method_contract(method).family == family


def test_unknown_plugin_method_keeps_legacy_compatibility() -> None:
    """第三方插件自定义方法仍应落入开放的 legacy 调度协议。"""
    contract = get_module_method_contract("third_party_custom_method")

    assert contract.family == "legacy"
    assert contract.supports_sync is True
    assert contract.supports_async is True
