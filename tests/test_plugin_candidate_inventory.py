"""插件市场候选库存读取测试。"""

import pytest

from app.application.plugin.identity import TrustedPluginSourceType
from app.application.plugin.inventory import (
    LocalCandidateLoadResult,
    PluginCandidateInventoryReader,
    PluginIndexLoadResult,
)
from app.application.plugin.source import LocalCandidateReadStatus, MarketReadStatus

OFFICIAL_MARKET = "https://github.com/jxxghp/MoviePilot-Plugins"
THIRD_PARTY_MARKET = "https://github.com/example/moviepilot-plugins"


def test_load_reads_each_market_in_v3_v2_v1_order_and_keeps_all_facts() -> None:
    """每个市场的三代索引都应有独立读取记录，且同 ID 候选不能被合并。"""
    calls: list[tuple[str, str | None, bool]] = []

    def loader(market: str, package_version: str | None, force: bool):
        calls.append((market, package_version, force))
        return {
            "DemoPlugin": {
                "version": f"{package_version or '1'}.0.0",
                "v3": True,
            },
        }

    inventory = PluginCandidateInventoryReader(market_loader=loader).load(
        [OFFICIAL_MARKET, THIRD_PARTY_MARKET],
        force=True,
    )

    assert calls == [
        (OFFICIAL_MARKET, "v3", True),
        (OFFICIAL_MARKET, "v2", True),
        (OFFICIAL_MARKET, None, True),
        (THIRD_PARTY_MARKET, "v3", True),
        (THIRD_PARTY_MARKET, "v2", True),
        (THIRD_PARTY_MARKET, None, True),
    ]
    assert inventory.complete
    assert [(read.market, read.package_generation) for read in inventory.market_reads] == [
        (OFFICIAL_MARKET, "v3"),
        (OFFICIAL_MARKET, "v2"),
        (OFFICIAL_MARKET, "v1"),
        (THIRD_PARTY_MARKET, "v3"),
        (THIRD_PARTY_MARKET, "v2"),
        (THIRD_PARTY_MARKET, "v1"),
    ]
    assert len(inventory.candidates_for("demoplugin")) == 6


def test_only_v3_compatible_entries_are_candidates() -> None:
    """V3 明确排除项以及 V1 未声明兼容项不能进入候选库存。"""
    def loader(_market: str, package_version: str | None, _force: bool):
        if package_version == "v3":
            return {
                "V3Plugin": {"version": "3.0.0"},
                "ExcludedPlugin": {"version": "3.0.0", "v3": False},
            }
        if package_version == "v2":
            return {
                "SharedPlugin": {"version": "2.0.0"},
                "ExcludedPlugin": {"version": "2.0.0", "v3": False},
            }
        return {
            "DeclaredV3": {"version": "1.0.0", "v3": True},
            "DeclaredV2": {"version": "1.0.0", "v2": True},
            "Undeclared": {"version": "1.0.0"},
            "ExcludedPlugin": {"version": "1.0.0", "v3": False, "v2": True},
        }

    inventory = PluginCandidateInventoryReader(market_loader=loader).load(
        [THIRD_PARTY_MARKET]
    )

    assert {
        candidate.plugin_id
        for candidate in inventory.online_candidates
    } == {"V3Plugin", "SharedPlugin", "DeclaredV3", "DeclaredV2"}
    assert not inventory.candidates_for("ExcludedPlugin")
    assert not inventory.candidates_for("Undeclared")


def test_official_source_is_classified_and_public_candidate_uses_plugin_version() -> None:
    """官方仓库使用官方来源类型，候选公共字段与 Plugin schema 对齐。"""
    reader = PluginCandidateInventoryReader(
        market_loader=lambda *_args: {"DemoPlugin": {"version": "3.1.0"}},
    )

    candidate = reader.load([OFFICIAL_MARKET]).online_candidates[0]

    assert candidate.source_key == "github:jxxghp/moviepilot-plugins"
    assert candidate.source_type is TrustedPluginSourceType.OFFICIAL
    assert candidate.plugin_version == "3.1.0"
    assert candidate.public_dict() == {
        "plugin_id": "DemoPlugin",
        "source_key": "github:jxxghp/moviepilot-plugins",
        "source_type": "official",
        "repo_url": "https://github.com/jxxghp/MoviePilot-Plugins",
        "package_generation": "v3",
        "plugin_version": "3.1.0",
    }


def test_partial_generation_failure_blocks_tofu_but_keeps_successful_candidates() -> None:
    """某一代读取失败时保留其他代候选，但库存不能用于第三方 TOFU。"""
    def loader(_market: str, package_version: str | None, _force: bool):
        if package_version == "v2":
            return None
        return {"DemoPlugin": {"version": "3.0.0", "v3": True}}

    inventory = PluginCandidateInventoryReader(market_loader=loader).load(
        [THIRD_PARTY_MARKET]
    )

    assert len(inventory.candidates_for("DemoPlugin")) == 2
    assert not inventory.complete
    assert not inventory.can_use_for_tofu
    assert inventory.read_for(THIRD_PARTY_MARKET, "v2") is not None
    assert inventory.read_for(THIRD_PARTY_MARKET, "v2").error


def test_absent_generation_is_complete_without_creating_candidates() -> None:
    """确定不存在的代际索引属于完整库存，不应被误判为网络失败。"""

    def loader(_market: str, package_version: str | None, _force: bool):
        if package_version == "v2":
            return PluginIndexLoadResult.absent()
        return {"DemoPlugin": {"version": "3.0.0", "v3": True}}

    inventory = PluginCandidateInventoryReader(market_loader=loader).load(
        [THIRD_PARTY_MARKET]
    )
    absent = inventory.read_for(THIRD_PARTY_MARKET, "v2")

    assert absent is not None
    assert absent.status is MarketReadStatus.ABSENT
    assert absent.candidates == ()
    assert inventory.complete
    assert inventory.can_use_for_tofu
    assert len(inventory.candidates_for("DemoPlugin")) == 2


def test_empty_index_is_present_and_complete() -> None:
    """真实存在但为空的索引与 absent 保持可观察差异。"""
    inventory = PluginCandidateInventoryReader(
        market_loader=lambda *_args: PluginIndexLoadResult.present({}),
    ).load([THIRD_PARTY_MARKET])

    assert inventory.complete
    assert all(
        read.status is MarketReadStatus.PRESENT
        for read in inventory.market_reads
    )
    assert inventory.online_candidates == ()


def test_explicit_failed_result_blocks_tofu() -> None:
    """Adapter 明确报告失败时必须阻止唯一第三方来源 TOFU。"""

    def loader(_market: str, package_version: str | None, _force: bool):
        if package_version == "v2":
            return PluginIndexLoadResult.failed("timeout")
        return {"DemoPlugin": {"version": "3.0.0", "v3": True}}

    inventory = PluginCandidateInventoryReader(market_loader=loader).load(
        [THIRD_PARTY_MARKET]
    )

    assert not inventory.complete
    assert not inventory.can_use_for_tofu
    assert inventory.read_for(THIRD_PARTY_MARKET, "v2").status is MarketReadStatus.FAILED


def test_local_scan_preserves_absent_present_and_failed_states() -> None:
    """本地仓库扫描不能把未配置、空扫描和异常读取混为一谈。"""
    def market_loader(*_args):
        return {}

    absent = PluginCandidateInventoryReader(market_loader=market_loader).load(
        [THIRD_PARTY_MARKET]
    )
    present = PluginCandidateInventoryReader(
        market_loader=market_loader,
        local_candidate_loader=lambda: LocalCandidateLoadResult.present({}),
    ).load([THIRD_PARTY_MARKET])

    def failed_loader():
        raise OSError("local repository unavailable")

    failed = PluginCandidateInventoryReader(
        market_loader=market_loader,
        local_candidate_loader=failed_loader,
    ).load([THIRD_PARTY_MARKET])

    assert absent.local_read.status is LocalCandidateReadStatus.ABSENT
    assert present.local_read.status is LocalCandidateReadStatus.PRESENT
    assert present.local_read.candidates == ()
    assert failed.local_read.status is LocalCandidateReadStatus.FAILED
    assert failed.local_read.error == "local repository unavailable"


def test_local_candidates_never_expose_path_in_inventory_projection() -> None:
    """本地候选可参与库存，但公共投影永不携带本地仓库路径。"""
    reader = PluginCandidateInventoryReader(
        market_loader=lambda *_args: {},
        local_candidate_loader=lambda: {
            "LocalPlugin": {
                "version": "3.0.0",
                "package_version": "v3",
                "repo_url": "local://LocalPlugin?path=/private/local&version=v3",
                "path": "/private/local/plugins/LocalPlugin",
                "repo_path": "/private/local",
            },
        },
    )

    inventory = reader.load([OFFICIAL_MARKET])
    public = inventory.public_dict()

    assert inventory.local_candidates[0].plugin_id == "LocalPlugin"
    assert public["local_candidates"] == [{
        "plugin_id": "LocalPlugin",
        "source_type": "local",
        "package_generation": "v3",
        "plugin_version": "3.0.0",
    }]
    assert "/private/local" not in str(public)


def test_invalid_local_candidate_does_not_abort_online_inventory() -> None:
    """本地索引中的坏代际条目应被跳过，不能丢失在线库存。"""
    reader = PluginCandidateInventoryReader(
        market_loader=lambda *_args: {
            "OnlinePlugin": {"version": "3.0.0"},
        },
        local_candidate_loader=lambda: {
            "BrokenLocal": {
                "version": "1.0.0",
                "package_version": "v9",
            },
        },
    )

    inventory = reader.load([OFFICIAL_MARKET])

    assert [candidate.plugin_id for candidate in inventory.online_candidates] == [
        "OnlinePlugin",
        "OnlinePlugin",
    ]
    assert inventory.local_candidates == ()


def test_invalid_market_is_recorded_for_each_generation_without_network_call() -> None:
    """非法市场配置应形成三条失败事实，且不会调用市场读取端口。"""
    calls: list[object] = []

    def read(*_args):
        calls.append(True)
        return {}

    inventory = PluginCandidateInventoryReader(market_loader=read).load(
        ["https://example.com/not-github"]
    )

    assert calls == []
    assert len(inventory.market_reads) == 3
    assert all(not read.succeeded for read in inventory.market_reads)
    assert not inventory.complete


@pytest.mark.asyncio
async def test_async_loader_preserves_generation_facts() -> None:
    """异步读取端口与同步端口拥有相同的市场代际快照合同。"""
    calls: list[str | None] = []

    async def loader(_market: str, package_version: str | None, _force: bool):
        calls.append(package_version)
        return {"DemoPlugin": {"version": "3.0.0", "v3": True}}

    reader = PluginCandidateInventoryReader(
        market_loader=lambda *_args: {},
        async_market_loader=loader,
    )
    inventory = await reader.async_load([THIRD_PARTY_MARKET])

    assert calls == ["v3", "v2", None]
    assert inventory.complete
    assert [read.package_generation for read in inventory.market_reads] == [
        "v3", "v2", "v1"
    ]
