"""插件候选事实与来源选择策略测试。"""

from app.application.plugin.identity import (
    PluginBindingBasis,
    PluginIdentity,
    PluginPayloadSourceType,
    TrustedPluginSourceType,
)
from app.application.plugin.source import (
    CandidateInventory,
    LocalCandidateRead,
    MarketRead,
    PluginLocalCandidate,
    PluginMarketCandidate,
    PluginSelectionStatus,
    select_plugin_candidate,
)

OFFICIAL_SOURCE = "github:jxxghp/moviepilot-plugins"
THIRD_PARTY_SOURCE = "github:example/moviepilot-plugins"
OTHER_SOURCE = "github:other/moviepilot-plugins"


def _online(
    source_key: str,
    *,
    source_type: TrustedPluginSourceType = TrustedPluginSourceType.THIRD_PARTY,
    version: str = "1.0.0",
    generation: str = "v3",
    plugin_id: str = "DemoPlugin",
    repo_url: str = "https://github.com/example/moviepilot-plugins",
) -> PluginMarketCandidate:
    """构造测试用在线候选。"""
    return PluginMarketCandidate(
        plugin_id=plugin_id,
        source_key=source_key,
        source_type=source_type,
        repo_url=repo_url,
        package_generation=generation,
        plugin_version=version,
        dto={"id": plugin_id, "version": version},
    )


def _inventory(*reads: MarketRead, local=()) -> CandidateInventory:
    """构造测试用候选快照。"""
    return CandidateInventory(tuple(reads), tuple(local))


def _identity(source_type: TrustedPluginSourceType, source_key: str) -> PluginIdentity:
    """构造已绑定在线来源身份。"""
    from datetime import datetime, timezone

    now = datetime(2026, 8, 25, tzinfo=timezone.utc)
    return PluginIdentity(
        plugin_id="DemoPlugin",
        normalized_plugin_id="demoplugin",
        trusted_source_type=source_type,
        trusted_source_key=source_key,
        binding_basis=PluginBindingBasis.OFFICIAL_DEFAULT
        if source_type is TrustedPluginSourceType.OFFICIAL
        else PluginBindingBasis.TOFU,
        payload_source_type=PluginPayloadSourceType.UNKNOWN,
        payload_source_key=None,
        declared_version=None,
        package_generation=None,
        system_version=None,
        supports_v3=None,
        supports_v3t=None,
        payload_receipt=None,
        revision=1,
        created_at=now,
        updated_at=now,
        bound_at=now,
        payload_applied_at=None,
    )


def test_cross_source_high_version_does_not_win() -> None:
    """已绑定来源过滤必须先于版本比较，跨源高版本不能覆盖允许来源。"""
    inventory = _inventory(
        MarketRead.present(
            "market-a",
            (
                _online(THIRD_PARTY_SOURCE, version="1.0.0"),
                _online(OTHER_SOURCE, version="9.0.0", repo_url="https://github.com/other/moviepilot-plugins"),
            ),
        ),
    )

    result = select_plugin_candidate(
        inventory,
        plugin_id="DemoPlugin",
        identity=_identity(TrustedPluginSourceType.THIRD_PARTY, THIRD_PARTY_SOURCE),
        generations=("v3", "v2", "v1"),
    )

    assert result.status is PluginSelectionStatus.SELECTED
    assert result.candidate is not None
    assert result.candidate.source_key == THIRD_PARTY_SOURCE
    assert result.candidate.plugin_version == "1.0.0"


def test_same_source_prefers_generation_then_version() -> None:
    """同源候选先按运行代际，再在同代内按声明版本选择。"""
    inventory = _inventory(
        MarketRead.present(
            "market-a",
            (
                _online(THIRD_PARTY_SOURCE, generation="v2", version="9.0.0"),
                _online(THIRD_PARTY_SOURCE, generation="v3", version="1.0.0"),
                _online(THIRD_PARTY_SOURCE, generation="v3", version="2.0.0"),
            ),
        ),
    )

    result = select_plugin_candidate(
        inventory,
        plugin_id="DemoPlugin",
        identity=_identity(TrustedPluginSourceType.THIRD_PARTY, THIRD_PARTY_SOURCE),
        generations=("v3", "v2", "v1"),
    )

    assert len(inventory.candidates_for("demoplugin")) == 3
    assert result.candidate is not None
    assert result.candidate.package_generation == "v3"
    assert result.candidate.plugin_version == "2.0.0"


def test_partial_market_failure_blocks_unique_third_party_tofu() -> None:
    """部分市场失败时即使当前可见一个第三方，也不能证明其唯一。"""
    inventory = _inventory(
        MarketRead.present("market-a", (_online(THIRD_PARTY_SOURCE),)),
        MarketRead.failure("market-b", "timeout"),
    )

    result = select_plugin_candidate(
        inventory,
        plugin_id="DemoPlugin",
        generations=("v3", "v2", "v1"),
    )

    assert inventory.complete is False
    assert inventory.can_use_for_tofu is False
    assert result.status is PluginSelectionStatus.INCOMPLETE


def test_local_scan_failure_blocks_automatic_selection_but_explicit_source_continues() -> None:
    """本地扫描失败时自动路径闭锁，管理员明确选在线来源仍可继续。"""
    inventory = CandidateInventory(
        (
            MarketRead.present(
                "market-a",
                (_online(THIRD_PARTY_SOURCE),),
            ),
        ),
        local_read=LocalCandidateRead.failure("local repository unavailable"),
    )

    automatic = select_plugin_candidate(
        inventory,
        plugin_id="DemoPlugin",
        generations=("v3", "v2", "v1"),
    )
    explicit = select_plugin_candidate(
        inventory,
        plugin_id="DemoPlugin",
        generations=("v3", "v2", "v1"),
        requested_source_key=THIRD_PARTY_SOURCE,
        explicit_source=True,
    )

    assert automatic.status is PluginSelectionStatus.INCOMPLETE
    assert explicit.status is PluginSelectionStatus.SELECTED
    assert explicit.candidate is not None
    assert explicit.candidate.source_key == THIRD_PARTY_SOURCE


def test_uninstalled_unique_and_multiple_sources_are_distinct() -> None:
    """未安装插件允许完整快照中的唯一来源，多来源必须返回冲突。"""
    unique = select_plugin_candidate(
        _inventory(MarketRead.present("market-a", (_online(THIRD_PARTY_SOURCE),))),
        plugin_id="DemoPlugin",
        generations=("v3", "v2", "v1"),
    )
    conflict = select_plugin_candidate(
        _inventory(
            MarketRead.present(
                "market-a",
                (_online(THIRD_PARTY_SOURCE), _online(OTHER_SOURCE)),
            ),
        ),
        plugin_id="DemoPlugin",
        generations=("v3", "v2", "v1"),
    )

    assert unique.status is PluginSelectionStatus.SELECTED
    assert conflict.status is PluginSelectionStatus.CONFLICT
    assert set(conflict.conflict_source_keys) == {THIRD_PARTY_SOURCE, OTHER_SOURCE}


def test_official_candidate_is_selectable_and_local_projection_hides_path() -> None:
    """官方来源可正常选择，本地公共投影不能泄漏仓库路径或 metadata。"""
    official = select_plugin_candidate(
        _inventory(
            MarketRead.present(
                "official-market",
                (_online(
                    OFFICIAL_SOURCE,
                    source_type=TrustedPluginSourceType.OFFICIAL,
                    repo_url="https://github.com/jxxghp/moviepilot-plugins",
                ),),
            ),
        ),
        plugin_id="DemoPlugin",
        generations=("v3", "v2", "v1"),
    )
    local = PluginLocalCandidate(
        plugin_id="DemoPlugin",
        repo_url="local://DemoPlugin?path=/private/secret/plugins",
        package_generation="v3",
        plugin_version="3.0.0",
        dto={"path": "/private/secret/plugins"},
    )

    local_result = select_plugin_candidate(
        _inventory(MarketRead.present("official-market", ()), local=(local,)),
        plugin_id="DemoPlugin",
        generations=("v3", "v2", "v1"),
    )

    assert official.status is PluginSelectionStatus.SELECTED
    assert official.candidate is not None
    assert official.candidate.source_type is TrustedPluginSourceType.OFFICIAL
    assert local.payload_source_type is PluginPayloadSourceType.LOCAL
    assert local.source_type is PluginPayloadSourceType.LOCAL
    assert local.source_key is None
    assert local_result.candidate is local
    public = local_result.public_dict()
    assert "/private/secret/plugins" not in str(public)
    assert "repo_url" not in public["candidate"]
