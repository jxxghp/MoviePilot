"""模块字符串方法契约清单的架构测试。"""

import json
from pathlib import Path

from app.runtime.extensions.module.contracts import (
    ModuleResultAggregation,
    get_module_method_contract,
    get_multi_source_contract,
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
        "match_media": "media-metadata",
        "media_detail": "media-metadata",
        "discover": "media-discovery",
        "recognize_media": "media-recognition",
        "media_exists": "media-library",
        "mediaserver_items": "media-server",
        "list_files": "storage",
        "finalize_message": "messaging",
        "scheduler_job": "scheduling",
        "torrent_files": "downloader",
    }

    for method, family in expected_families.items():
        assert is_explicit_module_method(method)
        assert get_module_method_contract(method).family == family


def test_source_prefixed_methods_no_longer_declare_a_dedicated_family() -> None:
    """六个多来源能力契约把数据源降为参数后，源前缀方法名退回未分类 legacy 契约。"""
    for method in (
        "tmdb_collection",
        "async_tmdb_episodes",
        "douban_info",
        "bangumi_info",
        "anilist_info",
        "tvdb_slug",
    ):
        assert not is_explicit_module_method(method)
        assert get_module_method_contract(method).family == "legacy"


def test_media_exists_declares_its_multi_source_protocol() -> None:
    """多来源存量判定的让出方式、收窄开关与取用规则必须成文可查。"""
    contract = get_multi_source_contract("media_exists")

    assert contract is not None
    assert len(contract.sources) == 2
    assert "None" in contract.abstain
    assert dict(contract.narrowing).keys() == {"server", "itemid", "LOCAL_EXISTS_SEARCH"}
    assert "并集" in contract.arbitration


def test_single_source_method_declares_no_multi_source_protocol() -> None:
    """单一来源能力不登记多来源协议。"""
    assert get_multi_source_contract("mediaserver_items") is None


def test_unknown_plugin_method_keeps_legacy_compatibility() -> None:
    """第三方插件自定义方法仍应落入开放的 legacy 调度协议。"""
    contract = get_module_method_contract("third_party_custom_method")

    assert contract.family == "legacy"
    assert contract.supports_sync is True
    assert contract.supports_async is True
