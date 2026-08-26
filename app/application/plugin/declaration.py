"""已提交插件载荷的版本化 package 声明快照。"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

_SCHEMA_VERSION = 1
_RUNTIME_FIELD_PATTERN = re.compile(r"^v[1-9][0-9]*t?$")
_MANIFEST_TEXT_FIELDS = (
    "name",
    "description",
    "icon",
    "author",
    "system_version",
)


def _optional_text(
    value: object,
    *,
    max_length: int | None = None,
) -> str | None:
    """把外部可选文本规范为非空字符串。"""
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if max_length is not None and len(normalized) > max_length:
        raise ValueError(f"插件声明文本长度不能超过 {max_length}")
    return normalized or None


def _labels(value: object) -> list[str]:
    """把历史逗号字符串和字符串数组统一为有序标签列表。"""
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, list):
        items = value
    else:
        return []
    return [
        normalized
        for item in items
        if isinstance(item, str) and (normalized := item.strip())
    ]


def _normalize_manifest(value: object) -> dict[str, object]:
    """仅保留当前载荷展示和声明消费者需要的 package 字段。"""
    if not isinstance(value, Mapping):
        return {}
    manifest: dict[str, object] = {}
    for field_name in _MANIFEST_TEXT_FIELDS:
        if normalized := _optional_text(
            value.get(field_name),
            max_length=128 if field_name == "system_version" else None,
        ):
            manifest[field_name] = normalized
    labels = _labels(value.get("labels"))
    if labels:
        manifest["labels"] = labels
    level = value.get("level")
    if isinstance(level, int) and not isinstance(level, bool):
        manifest["level"] = level
    release = value.get("release")
    if isinstance(release, bool):
        manifest["release"] = release
    return manifest


def _normalize_runtime(value: object) -> dict[str, bool]:
    """保留可向后扩展的宿主代际和运行时变体布尔声明。"""
    if not isinstance(value, Mapping):
        return {}
    return {
        str(field_name): field_value
        for field_name, field_value in value.items()
        if (
            isinstance(field_name, str)
            and _RUNTIME_FIELD_PATTERN.fullmatch(field_name)
            and isinstance(field_value, bool)
        )
    }


def _encode(value: Mapping[str, object]) -> str:
    """生成不可从外部原地修改的稳定内部表示。"""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


@dataclass(frozen=True, slots=True)
class PluginDeclaredMetadata:
    """封装一份受限、版本化且不参与来源授信的 package 声明。"""

    _encoded: str = field(repr=False)

    def __post_init__(self) -> None:
        """拒绝绕过工厂构造的非规范内部状态。"""
        normalized = self._normalize_storage(json.loads(self._encoded))
        object.__setattr__(self, "_encoded", _encode(normalized))

    @classmethod
    def from_package(
        cls,
        package: Mapping[str, Any],
        *,
        declaration_version: str | None,
        manifest_matches_payload: bool,
    ) -> "PluginDeclaredMetadata":
        """从安装候选生成与载荷一起提交的受限声明快照。"""
        runtime = _normalize_runtime(package)
        value = {
            "schema_version": _SCHEMA_VERSION,
            "declaration_version": _optional_text(declaration_version),
            "manifest_matches_payload": manifest_matches_payload,
            "manifest": _normalize_manifest(package),
            "runtime": runtime,
        }
        return cls(_encode(value))

    @classmethod
    def from_storage(cls, value: object) -> "PluginDeclaredMetadata":
        """校验数据库 JSON，并忽略当前合同未消费的额外字段。"""
        return cls(_encode(cls._normalize_storage(value)))

    @staticmethod
    def _normalize_storage(value: object) -> dict[str, object]:
        """验证快照结构，同时宽容丢弃非法可选展示字段。"""
        if not isinstance(value, Mapping):
            raise ValueError("插件声明快照必须是 JSON 对象")
        if value.get("schema_version") != _SCHEMA_VERSION:
            raise ValueError("插件声明快照 schema_version 不受支持")
        manifest_matches_payload = value.get("manifest_matches_payload")
        if not isinstance(manifest_matches_payload, bool):
            raise ValueError("插件声明快照必须说明 manifest 是否对应当前载荷")
        return {
            "schema_version": _SCHEMA_VERSION,
            "declaration_version": _optional_text(
                value.get("declaration_version")
            ),
            "manifest_matches_payload": manifest_matches_payload,
            "manifest": _normalize_manifest(value.get("manifest")),
            "runtime": _normalize_runtime(value.get("runtime")),
        }

    @property
    def declaration_version(self) -> str | None:
        """返回生成该快照的 package 条目版本。"""
        return _optional_text(self.to_json().get("declaration_version"))

    @property
    def manifest_matches_payload(self) -> bool:
        """说明该 package 声明是否与当前已提交载荷对应。"""
        return bool(self.to_json()["manifest_matches_payload"])

    def runtime_support(self, runtime_name: str) -> bool | None:
        """读取一个规范运行时声明；缺省保持未声明语义。"""
        if not _RUNTIME_FIELD_PATTERN.fullmatch(runtime_name):
            raise ValueError("运行时声明必须使用 v<数字> 或 v<数字>t")
        runtime = self.to_json().get("runtime")
        if not isinstance(runtime, dict):
            return None
        value = runtime.get(runtime_name)
        return value if isinstance(value, bool) else None

    def display_fallback(self, *, installed_version: str) -> dict[str, str]:
        """生成插件加载失败时可安全补齐的展示字段。"""
        manifest = self.to_json().get("manifest")
        fallback = {
            "plugin_version": installed_version,
        }
        if not isinstance(manifest, dict):
            return fallback
        display_fields = {
            "name": "plugin_name",
            "description": "plugin_desc",
            "icon": "plugin_icon",
            "author": "plugin_author",
        }
        for source_name, target_name in display_fields.items():
            value = manifest.get(source_name)
            if isinstance(value, str):
                fallback[target_name] = value
        labels = manifest.get("labels")
        if isinstance(labels, list):
            label = ",".join(item for item in labels if isinstance(item, str))
            if label:
                fallback["plugin_label"] = label
        return fallback

    def to_json(self) -> dict[str, object]:
        """返回可持久化且与内部状态相互隔离的 JSON 副本。"""
        value: object = json.loads(self._encoded)
        if not isinstance(value, dict):
            raise ValueError("插件声明快照内部状态必须是 JSON 对象")
        return {
            str(field_name): field_value
            for field_name, field_value in value.items()
        }
