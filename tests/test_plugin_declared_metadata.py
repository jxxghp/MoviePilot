"""插件 package 声明快照和值对象展示回退测试。"""

from datetime import datetime, timezone

import pytest

from app.application.plugin.catalog import apply_declared_metadata_fallback
from app.application.plugin.declaration import PluginDeclaredMetadata
from app.application.plugin.identity import (
    PluginBindingBasis,
    PluginIdentity,
    PluginPayloadSourceType,
    TrustedPluginSourceType,
)
from app.schemas.plugin import Plugin, PluginRuntimeStatus

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
OFFICIAL_SOURCE = "github:jxxghp/moviepilot-plugins"


def _metadata(
    package: dict[str, object],
    *,
    version: str = "1.0.0",
    matches_payload: bool = True,
) -> PluginDeclaredMetadata:
    """构造一份可用于身份测试的 package 声明快照。"""
    return PluginDeclaredMetadata.from_package(
        package,
        declaration_version=version,
        manifest_matches_payload=matches_payload,
    )


def _identity(
    metadata: PluginDeclaredMetadata,
    *,
    version: str = "1.0.0",
    plugin_id: str = "DemoPlugin",
) -> PluginIdentity:
    """构造带已提交声明快照的官方在线身份。"""
    return PluginIdentity(
        plugin_id=plugin_id,
        normalized_plugin_id=plugin_id.lower(),
        trusted_source_type=TrustedPluginSourceType.OFFICIAL,
        trusted_source_key=OFFICIAL_SOURCE,
        binding_basis=PluginBindingBasis.OFFICIAL_DEFAULT,
        payload_source_type=PluginPayloadSourceType.OFFICIAL,
        payload_source_key=OFFICIAL_SOURCE,
        declared_version=version,
        package_generation="v3",
        declared_metadata=metadata,
        payload_receipt="sha256:" + "0" * 64,
        revision=1,
        created_at=NOW,
        updated_at=NOW,
        bound_at=NOW,
        payload_applied_at=NOW,
    )


def test_declared_metadata_round_trips_package_and_storage_without_extra_fields() -> None:
    """package 声明保存后应可稳定往返，并丢弃未纳入合同的字段。"""
    package = {
        "name": "Demo",
        "description": "A demo plugin",
        "icon": "demo.svg",
        "author": "MoviePilot",
        "system_version": ">=3.0.0",
        "labels": "alpha, beta",
        "level": 2,
        "release": True,
        "v4": True,
        "v4t": False,
        "unknown_field": "ignored",
        "repo_url": "https://github.com/attacker/repo",
        "source": "github:attacker/repo",
        "token": "secret",
        "plugin_public_key": "public-key",
    }

    snapshot = _metadata(package, version="2.0.0")
    restored = PluginDeclaredMetadata.from_storage(snapshot.to_json())
    stored = restored.to_json()

    assert restored == snapshot
    assert restored.declaration_version == "2.0.0"
    assert restored.manifest_matches_payload is True
    assert restored.runtime_support("v4") is True
    assert restored.runtime_support("v4t") is False
    assert stored["manifest"] == {
        "author": "MoviePilot",
        "description": "A demo plugin",
        "icon": "demo.svg",
        "labels": ["alpha", "beta"],
        "level": 2,
        "name": "Demo",
        "release": True,
        "system_version": ">=3.0.0",
    }
    assert stored["runtime"] == {"v4": True, "v4t": False}
    assert "unknown_field" not in str(stored)
    assert "attacker" not in str(stored)
    assert "secret" not in str(stored)
    assert "public-key" not in str(stored)


def test_declared_metadata_preserves_system_version_storage_limit() -> None:
    """系统版本声明继续遵守旧数据库列的长度合同，保证迁移可降级。"""
    with pytest.raises(ValueError, match="长度不能超过 128"):
        _metadata({"system_version": "v" * 129})


def test_declared_metadata_ignores_unknown_runtime_shapes_and_is_deep_copied() -> None:
    """未知代际和外部可变对象不能污染已提交快照。"""
    labels = ["alpha"]
    package = {
        "name": "Demo",
        "labels": labels,
        "v4": True,
        "v4t": False,
        "v5": "not-a-bool",
        "vX": True,
    }
    snapshot = _metadata(package)
    labels.append("mutated-after-build")
    exported = snapshot.to_json()
    exported["manifest"]["labels"].append("mutated-export")

    assert snapshot.to_json()["manifest"]["labels"] == ["alpha"]
    assert snapshot.runtime_support("v4") is True
    assert snapshot.runtime_support("v4t") is False
    assert snapshot.runtime_support("v5") is None


def test_declared_metadata_records_current_and_historical_release_truth() -> None:
    """当前 package 与历史 Release 的声明对应关系必须可区分。"""
    current = _metadata({"name": "Demo", "v3": True}, version="2.0.0")
    historical = _metadata(
        {"name": "Demo", "v3": True},
        version="1.0.0",
        matches_payload=False,
    )

    assert current.manifest_matches_payload is True
    assert historical.manifest_matches_payload is False
    assert historical.declaration_version == "1.0.0"


def test_declared_metadata_fallback_is_batch_safe_and_preserves_runtime_fields() -> None:
    """批量展示回退只补空展示字段，不覆盖运行态或另一插件身份。"""
    first = _identity(
        _metadata(
            {
                "name": "Demo from snapshot",
                "description": "Saved description",
                "icon": "saved.svg",
                "author": "Saved author",
                "labels": ["saved", "plugin"],
            },
            version="2.0.0",
        ),
        version="2.0.0",
        plugin_id="DemoPlugin",
    )
    second = _identity(
        _metadata({"name": "Other from snapshot"}, version="3.0.0"),
        version="3.0.0",
        plugin_id="OtherPlugin",
    )
    plugins = [
        Plugin(
            id="DemoPlugin",
            plugin_name="DemoPlugin",
            plugin_version=None,
            runtime_status=PluginRuntimeStatus.LOAD_FAILED,
            state=True,
            plugin_desc=None,
        ),
        Plugin(
            id="OtherPlugin",
            plugin_name="Runtime name",
            plugin_desc="Runtime description",
            plugin_version="loaded-version",
            runtime_status=PluginRuntimeStatus.READY,
        ),
        Plugin(id="MissingPlugin", plugin_name="MissingPlugin"),
    ]

    result = apply_declared_metadata_fallback(
        plugins,
        {"demoplugin": first, "otherplugin": second},
    )

    assert result[0].plugin_name == "Demo from snapshot"
    assert result[0].plugin_desc == "Saved description"
    assert result[0].plugin_icon == "saved.svg"
    assert result[0].plugin_author == "Saved author"
    assert result[0].plugin_version == "2.0.0"
    assert result[0].plugin_label == "saved,plugin"
    assert result[0].runtime_status is PluginRuntimeStatus.LOAD_FAILED
    assert result[0].state is True
    assert result[1].plugin_name == "Runtime name"
    assert result[1].plugin_desc == "Runtime description"
    assert result[1].plugin_version == "loaded-version"
    assert result[1].runtime_status is PluginRuntimeStatus.READY
    assert result[2].plugin_name == "MissingPlugin"
