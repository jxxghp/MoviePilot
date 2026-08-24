"""插件来源准入与目标身份规划测试。"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.application.plugin.admission import (
    PluginInstallAdmissionRequest,
    PluginSourceAdmissionError,
    admit_plugin_install,
)
from app.application.plugin.identity import (
    PluginBindingBasis,
    PluginIdentity,
    PluginPayloadSourceType,
    TrustedPluginSourceType,
)
from app.application.plugin.source import (
    CandidateInventory,
    MarketRead,
    PluginLocalCandidate,
    PluginMarketCandidate,
)

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
OFFICIAL = "github:jxxghp/moviepilot-plugins"
THIRD_PARTY = "github:example/moviepilot-plugins"


def _online_candidate(
    *,
    source_key: str = OFFICIAL,
    source_type: TrustedPluginSourceType = TrustedPluginSourceType.OFFICIAL,
) -> PluginMarketCandidate:
    """构造一个可安装在线候选。"""
    owner_repo = source_key.removeprefix("github:")
    return PluginMarketCandidate(
        plugin_id="DemoPlugin",
        source_key=source_key,
        source_type=source_type,
        repo_url=f"https://github.com/{owner_repo}",
        package_generation="v3",
        plugin_version="2.0.0",
        dto={"system_version": ">=3.0.0", "v3": True, "v3t": False},
    )


def _identity() -> PluginIdentity:
    """构造已经绑定官方来源的旧载荷身份。"""
    return PluginIdentity(
        plugin_id="DemoPlugin",
        normalized_plugin_id="demoplugin",
        trusted_source_type=TrustedPluginSourceType.OFFICIAL,
        trusted_source_key=OFFICIAL,
        binding_basis=PluginBindingBasis.OFFICIAL_DEFAULT,
        payload_source_type=PluginPayloadSourceType.OFFICIAL,
        payload_source_key=OFFICIAL,
        declared_version="1.0.0",
        package_generation="v3",
        system_version=None,
        supports_v3=True,
        supports_v3t=None,
        payload_receipt="sha256:" + "0" * 64,
        revision=3,
        created_at=NOW,
        updated_at=NOW,
        bound_at=NOW,
        payload_applied_at=NOW,
    )


def _inventory(*candidates, local_candidates=()) -> CandidateInventory:
    """构造完整市场库存。"""
    return CandidateInventory(
        (MarketRead.present("https://github.com/example/plugins", candidates),),
        tuple(local_candidates),
    )


def test_same_source_update_preserves_binding_and_advances_payload() -> None:
    """同源更新只推进载荷事实，不改变既有可信绑定依据。"""
    current = _identity()
    admission = admit_plugin_install(
        _inventory(_online_candidate()),
        request=PluginInstallAdmissionRequest(
            plugin_id="DemoPlugin",
            generations=("v3", "v2", "v1"),
            requested_repo_url="https://github.com/jxxghp/MoviePilot-Plugins",
            explicit_source=False,
        ),
        identity=current,
        now=NOW,
    )

    target = admission.build_identity(
        payload_receipt="sha256:" + "1" * 64,
        applied_at=NOW,
    )

    assert target.binding_basis is PluginBindingBasis.OFFICIAL_DEFAULT
    assert target.trusted_source_key == OFFICIAL
    assert target.revision == 4
    assert target.declared_version == "2.0.0"


def test_first_online_binding_uses_payload_commit_time() -> None:
    """首次在线绑定在载荷提交时生效，不能早于身份创建时间。"""
    applied_at = NOW + timedelta(seconds=1)
    admission = admit_plugin_install(
        _inventory(_online_candidate()),
        request=PluginInstallAdmissionRequest(
            plugin_id="DemoPlugin",
            generations=("v3", "v2", "v1"),
            requested_repo_url="https://github.com/jxxghp/MoviePilot-Plugins",
            explicit_source=True,
        ),
        identity=None,
        now=NOW,
    )

    target = admission.build_identity(
        payload_receipt="sha256:" + "6" * 64,
        applied_at=applied_at,
    )

    assert target.created_at == applied_at
    assert target.updated_at == applied_at
    assert target.bound_at == applied_at
    assert target.payload_applied_at == applied_at


def test_force_semantics_cannot_authorize_source_change() -> None:
    """普通安装即使替换载荷，也不能选择不同于已绑定来源的仓库。"""
    with pytest.raises(PluginSourceAdmissionError, match="普通安装不能改变"):
        admit_plugin_install(
            _inventory(
                _online_candidate(),
                _online_candidate(
                    source_key=THIRD_PARTY,
                    source_type=TrustedPluginSourceType.THIRD_PARTY,
                ),
            ),
            request=PluginInstallAdmissionRequest(
                plugin_id="DemoPlugin",
                generations=("v3", "v2", "v1"),
                requested_repo_url="https://github.com/example/moviepilot-plugins",
                explicit_source=True,
            ),
            identity=_identity(),
            now=NOW,
        )


@pytest.mark.parametrize("revision", [None, 2, 4])
def test_source_change_requires_exact_identity_revision(revision: int | None) -> None:
    """显式换源必须携带当前身份的精确 revision。"""
    with pytest.raises(PluginSourceAdmissionError, match="revision"):
        admit_plugin_install(
            _inventory(
                _online_candidate(
                    source_key=THIRD_PARTY,
                    source_type=TrustedPluginSourceType.THIRD_PARTY,
                )
            ),
            request=PluginInstallAdmissionRequest(
                plugin_id="DemoPlugin",
                generations=("v3", "v2", "v1"),
                requested_repo_url="https://github.com/example/moviepilot-plugins",
                explicit_source=True,
                source_change=True,
                expected_revision=revision,
            ),
            identity=_identity(),
            now=NOW,
        )


def test_source_change_builds_explicit_transition() -> None:
    """合法换源把 trusted 与 payload 一起指向明确选择的新仓库。"""
    candidate = _online_candidate(
        source_key=THIRD_PARTY,
        source_type=TrustedPluginSourceType.THIRD_PARTY,
    )
    admission = admit_plugin_install(
        _inventory(candidate),
        request=PluginInstallAdmissionRequest(
            plugin_id="DemoPlugin",
            generations=("v3", "v2", "v1"),
            requested_repo_url=candidate.repo_url,
            explicit_source=True,
            source_change=True,
            expected_revision=3,
        ),
        identity=_identity(),
        now=NOW,
    )

    applied_at = NOW + timedelta(seconds=1)
    target = admission.build_identity(
        payload_receipt="sha256:" + "2" * 64,
        applied_at=applied_at,
    )

    assert target.binding_basis is PluginBindingBasis.EXPLICIT_SOURCE_CHANGE
    assert target.trusted_source_key == THIRD_PARTY
    assert target.payload_source_key == THIRD_PARTY
    assert target.bound_at == applied_at


def test_local_payload_preserves_existing_online_trust() -> None:
    """本地开发载荷覆盖时保留此前可信在线来源，便于之后同源恢复。"""
    local = PluginLocalCandidate(
        plugin_id="DemoPlugin",
        repo_url="local://DemoPlugin?package_version=v3",
        package_generation="v3",
        plugin_version="2.0.0-dev",
        dto={"v3": True},
    )
    admission = admit_plugin_install(
        _inventory(local_candidates=(local,)),
        request=PluginInstallAdmissionRequest(
            plugin_id="DemoPlugin",
            generations=("v3", "v2", "v1"),
            requested_repo_url=local.repo_url,
            explicit_source=True,
        ),
        identity=_identity(),
        now=NOW,
    )

    target = admission.build_identity(
        payload_receipt="sha256:" + "3" * 64,
        applied_at=NOW,
    )

    assert target.trusted_source_key == OFFICIAL
    assert target.binding_basis is PluginBindingBasis.OFFICIAL_DEFAULT
    assert target.payload_source_type is PluginPayloadSourceType.LOCAL
    assert target.payload_source_key is None


def test_first_local_payload_creates_local_only_identity() -> None:
    """首次本地安装不会伪造在线可信来源。"""
    local = PluginLocalCandidate(
        plugin_id="DemoPlugin",
        repo_url="local://DemoPlugin?package_version=v3",
        package_generation="v3",
        plugin_version="2.0.0-dev",
        dto={"v3": True},
    )
    admission = admit_plugin_install(
        _inventory(local_candidates=(local,)),
        request=PluginInstallAdmissionRequest(
            plugin_id="DemoPlugin",
            generations=("v3", "v2", "v1"),
            requested_repo_url=local.repo_url,
            explicit_source=True,
        ),
        identity=None,
        now=NOW,
    )

    target = admission.build_identity(
        payload_receipt="sha256:" + "4" * 64,
        applied_at=NOW,
    )

    assert target.binding_basis is PluginBindingBasis.LOCAL_ONLY
    assert target.trusted_source_type is TrustedPluginSourceType.UNKNOWN


def test_sanitized_local_reference_selects_configured_candidate_without_path() -> None:
    """脱敏本地来源标识仍能选择配置内候选，但公共投影不暴露路径。"""
    local = PluginLocalCandidate(
        plugin_id="DemoPlugin",
        repo_url=(
            "local://DemoPlugin?path=/private/secret/plugins&version=v3"
        ),
        package_generation="v3",
        plugin_version="2.0.0-dev",
        dto={"v3": True, "path": "/private/secret/plugins"},
    )

    admission = admit_plugin_install(
        _inventory(local_candidates=(local,)),
        request=PluginInstallAdmissionRequest(
            plugin_id="DemoPlugin",
            generations=("v3", "v2", "v1"),
            requested_repo_url="local://DemoPlugin?version=v3",
        ),
        identity=None,
        now=NOW,
    )

    assert admission.candidate is local
    public = admission.candidate.public_dict()
    assert public == {
        "plugin_id": "DemoPlugin",
        "source_type": "local",
        "package_generation": "v3",
        "plugin_version": "2.0.0-dev",
    }
    assert "/private/secret/plugins" not in str(public)


def test_legacy_identity_can_bind_explicit_online_source() -> None:
    """存量未绑定身份可在管理员明确选源后建立在线可信来源。"""
    legacy = replace(
        _identity(),
        trusted_source_type=TrustedPluginSourceType.UNKNOWN,
        trusted_source_key=None,
        binding_basis=PluginBindingBasis.LEGACY_UNBOUND,
        payload_source_type=PluginPayloadSourceType.UNKNOWN,
        payload_source_key=None,
        declared_version=None,
        package_generation=None,
        supports_v3=None,
        payload_receipt=None,
        bound_at=None,
        payload_applied_at=None,
    )
    candidate = _online_candidate(
        source_key=THIRD_PARTY,
        source_type=TrustedPluginSourceType.THIRD_PARTY,
    )
    admission = admit_plugin_install(
        _inventory(candidate),
        request=PluginInstallAdmissionRequest(
            plugin_id="DemoPlugin",
            generations=("v3", "v2", "v1"),
            requested_repo_url=candidate.repo_url,
            explicit_source=True,
        ),
        identity=legacy,
        now=NOW,
    )

    target = admission.build_identity(
        payload_receipt="sha256:" + "5" * 64,
        applied_at=NOW,
    )

    assert target.binding_basis is PluginBindingBasis.EXPLICIT_INSTALL
    assert target.trusted_source_key == THIRD_PARTY
    assert target.revision == 4


def test_source_change_rejects_local_payload_reference() -> None:
    """带 revision 的显式换源只能切换在线可信来源。"""
    local = PluginLocalCandidate(
        plugin_id="DemoPlugin",
        repo_url="local://DemoPlugin?path=/private/plugins&version=v3",
        package_generation="v3",
        plugin_version="2.0.0-dev",
    )
    current = _identity()

    with pytest.raises(
        PluginSourceAdmissionError,
        match="显式换源只接受在线插件仓库",
    ):
        admit_plugin_install(
            _inventory(local_candidates=(local,)),
            request=PluginInstallAdmissionRequest(
                plugin_id="DemoPlugin",
                generations=("v3", "v2", "v1"),
                requested_repo_url="local://DemoPlugin?version=v3",
                explicit_source=True,
                source_change=True,
                expected_revision=current.revision,
            ),
            identity=current,
            now=NOW,
        )
