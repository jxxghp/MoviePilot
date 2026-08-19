from __future__ import annotations

import re
import tomllib
from pathlib import Path
from types import MappingProxyType
from typing import Any, Collection, Iterable, Mapping

from app.runtime.capabilities.errors import (
    CapabilityManifestError,
    UnknownCapabilityError,
)
from app.runtime.capabilities.model import (
    ActivationPolicy,
    CapabilitySpec,
    SelectorSchema,
    SelectorSpec,
)


_SCHEMA_VERSION = 1
_MANIFEST_NAME = "capability.toml"
_TOP_LEVEL_FIELDS = frozenset({
    "schema_version",
    "id",
    "kind",
    "entrypoint",
    "metadata",
    "activation",
    "depends_on",
})
_REQUIRED_FIELDS = _TOP_LEVEL_FIELDS
_ACTIVATION_FIELDS = frozenset({"policy", "watch", "selector"})
_ACTIVATION_REQUIRED_FIELDS = frozenset({"policy", "watch"})
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
_KIND_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_ENTRYPOINT_PATTERN = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*"
    r":[A-Za-z_][A-Za-z0-9_]*$"
)


def _freeze(value: Any, *, field: str) -> Any:
    """把 TOML 容器递归转换为不可变结构，并拒绝非配置标量。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return tuple(_freeze(item, field=field) for item in value)
    if isinstance(value, dict):
        if not all(isinstance(key, str) and key for key in value):
            raise CapabilityManifestError(f"{field} 包含非法键")
        return MappingProxyType({
            key: _freeze(item, field=f"{field}.{key}")
            for key, item in value.items()
        })
    raise CapabilityManifestError(f"{field} 包含不支持的 TOML 值类型 {type(value).__name__}")


def _string_list(value: Any, *, field: str, path: Path) -> tuple[str, ...]:
    """校验无重复的非空字符串列表。"""
    if not isinstance(value, list):
        raise CapabilityManifestError(f"{path}: {field} 必须是字符串数组")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise CapabilityManifestError(f"{path}: {field} 只能包含非空字符串")
    normalized = tuple(item.strip() for item in value)
    if len(set(normalized)) != len(normalized):
        raise CapabilityManifestError(f"{path}: {field} 不能包含重复值")
    return normalized


class CapabilityRegistry:
    """只读取 data-only manifest 的不可变能力注册表。"""

    def __init__(
        self,
        specs: Mapping[str, CapabilitySpec],
        *,
        kinds: Collection[str],
        selector_schemas: Mapping[str, SelectorSchema],
    ) -> None:
        self._specs = MappingProxyType(dict(specs))
        self._kinds = frozenset(kinds)
        self._selector_schemas = MappingProxyType(dict(selector_schemas))

    @classmethod
    def discover(
        cls,
        roots: Iterable[Path | str],
        *,
        kinds: Collection[str],
        selector_schemas: Mapping[str, SelectorSchema],
    ) -> "CapabilityRegistry":
        """扫描全部声明根；任何根或 manifest 非法都会阻止 Registry 构建。"""
        normalized_kinds = frozenset(kinds)
        if not normalized_kinds:
            raise CapabilityManifestError("至少需要注册一个 capability kind")
        for kind in normalized_kinds:
            if not isinstance(kind, str) or not _KIND_PATTERN.fullmatch(kind):
                raise CapabilityManifestError(f"非法 capability kind：{kind!r}")

        normalized_selectors = dict(selector_schemas)
        for selector_type, schema in normalized_selectors.items():
            if not isinstance(selector_type, str) or not _KIND_PATTERN.fullmatch(selector_type):
                raise CapabilityManifestError(f"非法 selector type：{selector_type!r}")
            if not isinstance(schema, SelectorSchema):
                raise CapabilityManifestError(f"selector {selector_type} 未提供 SelectorSchema")
            overlap = schema.required_fields & schema.optional_fields
            if overlap:
                raise CapabilityManifestError(
                    f"selector {selector_type} 字段同时声明为 required/optional：{sorted(overlap)}"
                )

        specs: dict[str, CapabilitySpec] = {}
        normalized_roots = tuple(Path(root) for root in roots)
        if not normalized_roots:
            raise CapabilityManifestError("至少需要一个 capability 声明根")
        for root in normalized_roots:
            if not root.is_dir():
                raise CapabilityManifestError(f"声明根不存在或不是目录：{root}")
            manifests = sorted(root.rglob(_MANIFEST_NAME))
            if not manifests:
                raise CapabilityManifestError(f"声明根没有 {_MANIFEST_NAME}：{root}")
            for manifest_path in manifests:
                spec = cls._load_manifest(
                    manifest_path,
                    kinds=normalized_kinds,
                    selector_schemas=normalized_selectors,
                )
                previous = specs.get(spec.id)
                if previous:
                    raise CapabilityManifestError(
                        f"capability id 重复：{spec.id}，来源 {previous.source} 与 {spec.source}"
                    )
                specs[spec.id] = spec
        return cls(
            specs,
            kinds=normalized_kinds,
            selector_schemas=normalized_selectors,
        )

    @classmethod
    def _load_manifest(
        cls,
        path: Path,
        *,
        kinds: Collection[str],
        selector_schemas: Mapping[str, SelectorSchema],
    ) -> CapabilitySpec:
        try:
            with path.open("rb") as file:
                data = tomllib.load(file)
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise CapabilityManifestError(f"无法读取 {path}：{error}") from error

        unknown_fields = set(data) - _TOP_LEVEL_FIELDS
        if unknown_fields:
            raise CapabilityManifestError(f"{path}: 未知字段 {sorted(unknown_fields)}")
        missing_fields = _REQUIRED_FIELDS - set(data)
        if missing_fields:
            raise CapabilityManifestError(f"{path}: 缺少字段 {sorted(missing_fields)}")

        schema_version = data["schema_version"]
        if type(schema_version) is not int or schema_version != _SCHEMA_VERSION:
            raise CapabilityManifestError(
                f"{path}: 不支持 schema_version={schema_version!r}"
            )

        capability_id = data["id"]
        if not isinstance(capability_id, str) or not _IDENTIFIER_PATTERN.fullmatch(capability_id):
            raise CapabilityManifestError(f"{path}: 非法 capability id={capability_id!r}")

        kind = data["kind"]
        if not isinstance(kind, str) or kind not in kinds:
            raise CapabilityManifestError(f"{path}: 未注册 capability kind={kind!r}")

        entrypoint = data["entrypoint"]
        if not isinstance(entrypoint, str) or not _ENTRYPOINT_PATTERN.fullmatch(entrypoint):
            raise CapabilityManifestError(f"{path}: 非法 entrypoint={entrypoint!r}")

        metadata = data["metadata"]
        if not isinstance(metadata, dict):
            raise CapabilityManifestError(f"{path}: metadata 必须是 table")
        name = metadata.get("name")
        if not isinstance(name, str) or not name.strip():
            raise CapabilityManifestError(f"{path}: metadata.name 必须是非空字符串")
        immutable_metadata = _freeze(metadata, field="metadata")

        activation_data = data["activation"]
        if not isinstance(activation_data, dict):
            raise CapabilityManifestError(f"{path}: activation 必须是 table")
        unknown_activation_fields = set(activation_data) - _ACTIVATION_FIELDS
        missing_activation_fields = _ACTIVATION_REQUIRED_FIELDS - set(activation_data)
        if unknown_activation_fields or missing_activation_fields:
            raise CapabilityManifestError(
                f"{path}: activation 字段非法，missing={sorted(missing_activation_fields)} "
                f"unknown={sorted(unknown_activation_fields)}"
            )
        try:
            activation = ActivationPolicy(activation_data["policy"])
        except (TypeError, ValueError) as error:
            raise CapabilityManifestError(
                f"{path}: 非法 activation.policy={activation_data['policy']!r}"
            ) from error

        selector = cls._parse_selector(
            path,
            activation=activation,
            data=activation_data.get("selector"),
            selector_schemas=selector_schemas,
        )
        watch = _string_list(activation_data["watch"], field="activation.watch", path=path)
        depends_on = _string_list(data["depends_on"], field="depends_on", path=path)
        if depends_on:
            raise CapabilityManifestError(
                f"{path}: 当前 schema 不支持非空 depends_on={list(depends_on)!r}"
            )

        return CapabilitySpec(
            schema_version=schema_version,
            id=capability_id,
            kind=kind,
            entrypoint=entrypoint,
            activation=activation,
            metadata=immutable_metadata,
            selector=selector,
            watch=watch,
            depends_on=depends_on,
            source=path,
        )

    @staticmethod
    def _parse_selector(
        path: Path,
        *,
        activation: ActivationPolicy,
        data: Any,
        selector_schemas: Mapping[str, SelectorSchema],
    ) -> SelectorSpec | None:
        if activation is not ActivationPolicy.WHEN_CONFIGURED:
            if data is not None:
                raise CapabilityManifestError(
                    f"{path}: activation={activation.value} 时不允许 selector"
                )
            return None
        if not isinstance(data, dict):
            raise CapabilityManifestError(f"{path}: when_configured 必须提供 selector table")
        selector_kind = data.get("kind")
        if not isinstance(selector_kind, str) or selector_kind not in selector_schemas:
            raise CapabilityManifestError(f"{path}: 未注册 selector kind={selector_kind!r}")
        config = {key: value for key, value in data.items() if key != "kind"}
        schema = selector_schemas[selector_kind]
        missing = schema.required_fields - set(config)
        unknown = set(config) - schema.required_fields - schema.optional_fields
        if missing or unknown:
            raise CapabilityManifestError(
                f"{path}: selector {selector_kind} 字段非法，"
                f"missing={sorted(missing)} unknown={sorted(unknown)}"
            )
        immutable_config = _freeze(config, field="selector")
        if schema.validator:
            try:
                schema.validator(immutable_config)
            except Exception as error:
                raise CapabilityManifestError(
                    f"{path}: selector {selector_kind} 校验失败：{error}"
                ) from error
        return SelectorSpec(kind=selector_kind, config=immutable_config)

    @property
    def kinds(self) -> frozenset[str]:
        """返回该 Registry 接受的 capability kind。"""
        return self._kinds

    def get_spec(self, capability_id: str) -> CapabilitySpec | None:
        """查询声明；不存在时返回 None。"""
        return self._specs.get(capability_id)

    def require_spec(self, capability_id: str) -> CapabilitySpec:
        """查询必需声明；不存在时给出稳定的领域错误。"""
        spec = self.get_spec(capability_id)
        if spec is None:
            raise UnknownCapabilityError(f"未知 capability：{capability_id}")
        return spec

    def list_specs(self) -> tuple[CapabilitySpec, ...]:
        """按 ID 返回稳定排序的声明快照。"""
        return tuple(self._specs[key] for key in sorted(self._specs))
