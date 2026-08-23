"""模块字符串方法契约清单的架构测试。"""

import json
from pathlib import Path

from app.runtime.extensions.module.contracts import (
    ModuleErrorPolicy,
    ModuleExecutionMode,
    ModuleResultAggregation,
    ModuleResultShape,
    diagnose_module_callable,
    diagnose_module_result,
    get_module_method_contract,
    is_explicit_module_method,
    list_explicit_module_contracts,
)


RUNTIME_BASELINE = (
    Path(__file__).parent / "fixtures" / "architecture" / "runtime-contract-baseline.json"
)


def test_all_scanned_host_module_methods_have_explicit_v2_contracts() -> None:
    """架构快照中的全部宿主字符串方法都必须显式登记 V2 契约。"""
    payload = json.loads(RUNTIME_BASELINE.read_text(encoding="utf-8"))
    methods = set(payload["run_module"]["methods"])
    contracts = list_explicit_module_contracts()

    assert methods
    assert methods <= contracts.keys()
    for method in methods:
        contract = contracts[method]
        assert isinstance(contract.aggregation, ModuleResultAggregation)
        assert contract.input_contract != "legacy_args"
        assert contract.result_contract != "Any"
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


def test_contract_v2_freezes_every_observed_host_method() -> None:
    """全部已观察宿主能力必须具备可生成文档和诊断的完整 V2 字段。"""
    contracts = list_explicit_module_contracts()

    assert len(contracts) >= 211
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


def test_result_diagnostics_check_only_enabled_basic_shapes() -> None:
    """高频方法检查基础结果形状，业务对象合同仍留给逐族适配器。"""
    assert get_module_method_contract("list_files").result_shape is ModuleResultShape.LIST
    assert diagnose_module_result("list_files", [object()]) == ()
    assert diagnose_module_result("list_files", None) == ()
    assert diagnose_module_result("list_files", "legacy-value") == (
        "unexpected-result:list:str",
    )
    assert diagnose_module_result("mediaserver_play_url", "https://example.test") == ()
    assert diagnose_module_result("mediaserver_play_url", 7) == (
        "unexpected-result:string:int",
    )


def test_message_attachment_contracts_use_messaging_family() -> None:
    """消息附件下载能力不得因 download 前缀误归入下载器能力族。"""
    expected_parameters = {
        "download_telegram_file_bytes": ("file_id", "source"),
        "download_wechat_media_bytes": ("media_ref", "source"),
        "download_slack_file_to_data_url": ("file_url", "source"),
        "download_feishu_image_to_data_url": ("image_ref", "source"),
    }

    for method, parameters in expected_parameters.items():
        contract = get_module_method_contract(method)
        assert contract.family == "messaging"
        assert contract.aggregation is ModuleResultAggregation.FIRST_NON_EMPTY
        assert contract.required_parameters == parameters


def test_downloader_query_contracts_merge_provider_lists() -> None:
    """下载器查询能力应显式合并各 provider 的有序列表结果。"""
    for method in ("list_torrents", "downloader_info"):
        contract = get_module_method_contract(method)
        assert contract.family == "downloader"
        assert contract.aggregation is ModuleResultAggregation.ORDERED_LIST_MERGE
        assert contract.result_shape is ModuleResultShape.LIST


def test_heterogeneous_torrent_files_result_remains_legacy_compatible() -> None:
    """下载器文件集合尚未归一前不得声明虚假的列表聚合语义。"""
    contract = get_module_method_contract("torrent_files")

    assert contract.required_parameters == ("tid", "downloader")
    assert contract.result_contract == "DownloaderFileCollection | None"
    assert contract.aggregation is ModuleResultAggregation.LEGACY
    assert contract.result_shape is ModuleResultShape.ANY


def test_attachment_result_diagnostics_distinguish_bytes_and_strings() -> None:
    """附件契约应区分二进制内容和可展示字符串，偏差仍仅供诊断。"""
    assert diagnose_module_result("download_qq_file_bytes", b"content") == ()
    assert diagnose_module_result("download_qq_file_bytes", "content") == (
        "unexpected-result:bytes:str",
    )
    assert diagnose_module_result(
        "download_wechat_image_to_data_url",
        "data:image/png;base64,AA==",
    ) == ()


def test_unknown_plugin_result_keeps_unchecked_legacy_compatibility() -> None:
    """未知第三方方法的任意返回值继续不做结果形状诊断。"""
    assert diagnose_module_result("third_party_custom_method", object()) == ()
