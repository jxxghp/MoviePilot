from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.runtime.capabilities.errors import CapabilityManifestError
from app.runtime.capabilities.model import (
    ActivationPolicy,
    SelectorSchema,
)
from app.runtime.capabilities.registry import CapabilityRegistry


_BASE_MANIFEST = """
schema_version = 1
id = "sample.capability"
kind = "sample"
entrypoint = "sample_implementation:SampleCapability"
depends_on = []

[metadata]
name = "Sample capability"
priority = 10

[activation]
policy = "when_configured"
watch = ["sample.config"]

[activation.selector]
kind = "configured"
key = "sample.config"
enabled = true
"""


def _write_manifest(root: Path, content: str = _BASE_MANIFEST, name: str = "sample") -> Path:
    manifest_dir = root / name
    manifest_dir.mkdir(parents=True)
    manifest_path = manifest_dir / "capability.toml"
    manifest_path.write_text(content.strip() + "\n", encoding="utf-8")
    return manifest_path


def _discover(root: Path) -> CapabilityRegistry:
    return CapabilityRegistry.discover(
        roots=[root],
        kinds={"sample"},
        selector_schemas={
            "configured": SelectorSchema(
                required_fields=frozenset({"key", "enabled"}),
            )
        },
    )


def test_discovery_reads_toml_without_importing_entrypoint(tmp_path: Path) -> None:
    """能力发现只能读取声明，不能执行 entrypoint 对应的 Python 模块。"""
    _write_manifest(tmp_path)
    (tmp_path / "sample_implementation.py").write_text(
        "raise AssertionError('entrypoint must not be imported during discovery')\n",
        encoding="utf-8",
    )
    sys.modules.pop("sample_implementation", None)

    registry = _discover(tmp_path)

    spec = registry.get_spec("sample.capability")
    assert spec is not None
    assert spec.activation is ActivationPolicy.WHEN_CONFIGURED
    assert spec.selector is not None
    assert spec.selector.kind == "configured"
    assert spec.selector.config == {"key": "sample.config", "enabled": True}
    assert spec.watch == ("sample.config",)
    assert spec.depends_on == ()
    assert "sample_implementation" not in sys.modules


def test_discovered_specs_are_recursively_immutable(tmp_path: Path) -> None:
    """Registry 暴露的声明及嵌套 metadata/selector 都不能被调用方改写。"""
    _write_manifest(tmp_path)
    spec = _discover(tmp_path).require_spec("sample.capability")

    with pytest.raises(TypeError):
        spec.metadata["name"] = "changed"
    with pytest.raises(TypeError):
        spec.selector.config["key"] = "changed"  # type: ignore[union-attr]
    with pytest.raises(AttributeError):
        spec.watch.append("changed")  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("replacement", "match"),
    [
        ("schema_version = 1", "缺少字段"),
        (_BASE_MANIFEST.replace("schema_version = 1", "schema_version = 2"), "schema_version"),
        (_BASE_MANIFEST.replace("schema_version = 1", "schema_version = 1.0"), "schema_version"),
        (_BASE_MANIFEST.replace('id = "sample.capability"', 'id = "bad id"'), "id"),
        (_BASE_MANIFEST.replace('kind = "sample"', 'kind = "unknown"'), "kind"),
        (
            _BASE_MANIFEST.replace(
                'entrypoint = "sample_implementation:SampleCapability"',
                'entrypoint = "sample_implementation.SampleCapability"',
            ),
            "entrypoint",
        ),
        (_BASE_MANIFEST.replace('kind = "configured"', 'kind = "unknown"'), "selector"),
        (_BASE_MANIFEST.replace('enabled = true', 'extra = true'), "selector"),
        (_BASE_MANIFEST.replace("depends_on = []", 'depends_on = ["other"]'), "depends_on"),
        (_BASE_MANIFEST.replace("watch =", "unknown_field = true\nwatch ="), "activation"),
    ],
)
def test_registry_fails_closed_for_invalid_manifest(
    tmp_path: Path,
    replacement: str,
    match: str,
) -> None:
    """未知或不完整声明必须阻止 Registry 构建，不能静默丢失能力。"""
    _write_manifest(tmp_path, replacement)

    with pytest.raises(CapabilityManifestError, match=match):
        _discover(tmp_path)


def test_selector_presence_must_match_activation_policy(tmp_path: Path) -> None:
    """只有 when_configured 声明可以且必须携带配置 selector。"""
    bootstrap = _BASE_MANIFEST.replace(
        'policy = "when_configured"', 'policy = "bootstrap"'
    )
    _write_manifest(tmp_path, bootstrap)

    with pytest.raises(CapabilityManifestError, match="selector"):
        _discover(tmp_path)


def test_registry_rejects_duplicate_ids_across_roots(tmp_path: Path) -> None:
    """多个声明根出现相同 capability ID 时必须 fail closed。"""
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    _write_manifest(first_root)
    _write_manifest(second_root)

    with pytest.raises(CapabilityManifestError, match="重复"):
        CapabilityRegistry.discover(
            roots=[first_root, second_root],
            kinds={"sample"},
            selector_schemas={
                "configured": SelectorSchema(
                    required_fields=frozenset({"key", "enabled"}),
                )
            },
        )


def test_registry_rejects_root_without_manifest(tmp_path: Path) -> None:
    """注册了声明根却没有任何 manifest 时应直接失败。"""
    with pytest.raises(CapabilityManifestError, match="capability.toml"):
        _discover(tmp_path)


def test_current_host_module_manifests_follow_the_strict_nested_schema() -> None:
    """仓内 Host Module 声明必须全部通过同一套嵌套 schema。"""
    modules_root = Path(__file__).parents[1] / "app" / "modules"
    imported_before = set(sys.modules)
    registry = CapabilityRegistry.discover(
        roots=[modules_root],
        kinds={"host_module"},
        selector_schemas={
            "system_config_item": SelectorSchema(
                required_fields=frozenset({
                    "key",
                    "match_field",
                    "match_value",
                    "enabled_field",
                }),
            ),
            "setting_truthy": SelectorSchema(
                required_fields=frozenset({"key"}),
            ),
        },
    )

    specs = registry.list_specs()
    declared_directories = {spec.source.parent for spec in specs}
    module_directories = {
        path
        for path in modules_root.iterdir()
        if path.is_dir() and (path / "__init__.py").is_file()
        # 下划线前缀目录是内部基础包（如 _base），不是 host module，不参与清单校验
        and not path.name.startswith("_")
    }
    entrypoint_modules = {spec.entrypoint.split(":", maxsplit=1)[0] for spec in specs}

    assert declared_directories == module_directories
    assert all(spec.kind == "host_module" for spec in specs)
    assert not ((set(sys.modules) - imported_before) & entrypoint_modules)
