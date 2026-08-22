"""模块字符串方法契约清单的架构测试。"""

import json
from pathlib import Path

from app.runtime.extensions.module.contracts import (
    ModuleErrorPolicy,
    ModuleExecutionMode,
    ModuleResultAggregation,
    diagnose_module_callable,
    get_module_method_contract,
    is_explicit_module_method,
    list_explicit_module_contracts,
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
        assert isinstance(contract.aggregation, ModuleResultAggregation)
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


def test_contract_v2_freezes_at_least_twenty_high_value_methods() -> None:
    """首批能力必须具备可生成文档和诊断的完整 V2 字段。"""
    contracts = list_explicit_module_contracts()

    assert len(contracts) >= 20
    for contract in contracts.values():
        assert contract.version == 1
        assert contract.input_contract != "legacy_args"
        assert contract.result_contract
        assert contract.execution is ModuleExecutionMode.SYNC_OR_ASYNC
        assert contract.timeout_policy == "caller_budget"
        assert contract.error_policy is ModuleErrorPolicy.ISOLATE_PROVIDER
        assert contract.public_to_plugins is True


def test_signature_diagnostics_do_not_reject_legacy_callable() -> None:
    """无法检查的旧插件 callable 只产生诊断，仍由 dispatcher 决定是否执行。"""
    class _OpaqueCallable:
        """模拟 inspect 无法解析签名的第三方 callable。"""

        @property
        def __signature__(self):
            """模拟扩展对象不提供 Python signature。"""
            raise ValueError("opaque")

        def __call__(self):
            """保留可调用行为。"""
            return "ok"

    assert diagnose_module_callable("recognize_media", _OpaqueCallable()) == (
        "signature-unavailable",
    )
    assert _OpaqueCallable()() == "ok"


def test_signature_diagnostics_report_missing_contract_parameters() -> None:
    """显式 Contract 应能指出 provider 遗漏的宿主调用参数。"""
    def incomplete_storage_provider(fileitem):
        """模拟仍未接受 recursion 参数的旧存储 provider。"""
        return [fileitem]

    assert diagnose_module_callable(
        "list_files", incomplete_storage_provider
    ) == ("missing-parameter:recursion",)


def test_signature_diagnostics_accept_keyword_compatibility_provider() -> None:
    """带 **kwargs 的第三方 provider 继续兼容逐步扩展的输入契约。"""
    def compatible_provider(**kwargs):
        """模拟通过关键字参数保持前向兼容的第三方 provider。"""
        return kwargs

    assert diagnose_module_callable("snapshot_storage", compatible_provider) == ()
