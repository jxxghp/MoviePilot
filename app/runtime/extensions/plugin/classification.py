"""插件媒体来源分类字段和受控事实的运行时注册表。"""

from __future__ import annotations

import re
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, cast

from pydantic import ValidationError

from app.domain.classification.fields import (
    VALUE_TYPE_OPERATORS,
    classification_fact_matches_definition,
)
from app.domain.classification.sources import BUILTIN_CLASSIFICATION_SOURCES
from app.schemas.category import (
    ClassificationFactValue,
    ClassificationFieldDefinition,
    ClassificationMediaType,
)
from app.schemas.event import MediaSourceInfo
from app.schemas.types import MEDIA_SOURCE_IDENTIFIER_PATTERN

_FIELD_SEGMENT_PATTERN = re.compile(MEDIA_SOURCE_IDENTIFIER_PATTERN)


class PluginClassificationDeclarationError(ValueError):
    """表示插件分类字段声明违反宿主命名空间或类型约束。"""


@dataclass(frozen=True, slots=True)
class _RegisteredMediaSource:
    """保存一个插件媒体来源及其按局部字段路径索引的声明。"""

    plugin_id: str
    source: MediaSourceInfo
    fields: dict[str, ClassificationFieldDefinition]


class PluginClassificationRegistry:
    """原子维护启用插件声明，并校验识别结果提供的扩展分类事实。"""

    def __init__(self, log: Any) -> None:
        """保存日志端口并初始化空的插件和来源索引。"""
        self._logger = log
        self._lock = threading.RLock()
        self._sources: dict[str, _RegisteredMediaSource] = {}
        self._plugin_sources: dict[str, tuple[str, ...]] = {}

    def replace(
        self,
        plugin_id: str,
        declarations: Iterable[Mapping[str, Any] | MediaSourceInfo],
    ) -> None:
        """完整替换一个插件的来源声明，任一声明无效时保持旧快照不变。"""
        normalized_plugin_id = str(plugin_id or "").strip()
        if not normalized_plugin_id:
            raise PluginClassificationDeclarationError("插件 ID 不能为空")
        candidates = tuple(
            self._normalize_source(normalized_plugin_id, declaration)
            for declaration in declarations
        )
        source_ids = tuple(item.source.media_source.value for item in candidates)
        if len(set(source_ids)) != len(source_ids):
            raise PluginClassificationDeclarationError(
                f"插件 {normalized_plugin_id} 重复声明了相同媒体来源"
            )

        with self._lock:
            owned = set(self._plugin_sources.get(normalized_plugin_id, ()))
            for source_id in source_ids:
                current = self._sources.get(source_id)
                if current is not None and source_id not in owned:
                    raise PluginClassificationDeclarationError(
                        f"媒体来源 {source_id} 已由插件 {current.plugin_id} 注册"
                    )
            next_sources = {
                source_id: item
                for source_id, item in self._sources.items()
                if source_id not in owned
            }
            next_sources.update(
                (item.source.media_source.value, item) for item in candidates
            )
            self._sources = next_sources
            if source_ids:
                self._plugin_sources[normalized_plugin_id] = source_ids
            else:
                self._plugin_sources.pop(normalized_plugin_id, None)

    def remove(self, plugin_id: str) -> None:
        """移除一个插件拥有的全部来源和字段声明，重复调用保持幂等。"""
        normalized_plugin_id = str(plugin_id or "").strip()
        with self._lock:
            source_ids = self._plugin_sources.pop(normalized_plugin_id, ())
            for source_id in source_ids:
                self._sources.pop(source_id, None)

    def sources(self, plugin_id: str | None = None) -> list[dict[str, Any]]:
        """返回当前有效来源声明的 JSON 隔离快照。"""
        with self._lock:
            selected = self._selected_sources(plugin_id)
            return [
                {
                    **item.source.model_dump(mode="json"),
                    "plugin_id": item.plugin_id,
                }
                for item in selected
            ]

    def fields(
        self,
        plugin_id: str | None = None,
    ) -> tuple[ClassificationFieldDefinition, ...]:
        """返回当前有效扩展字段的深拷贝稳定快照。"""
        with self._lock:
            return tuple(
                field.model_copy(deep=True)
                for item in self._selected_sources(plugin_id)
                for field in item.source.classification_fields
            )

    def facts(
        self,
        media: Any,
    ) -> dict[str, dict[str, ClassificationFactValue]]:
        """校验媒体对象携带的局部事实，只返回当前来源已登记且类型兼容的值。"""
        source_id = _enum_text(getattr(media, "media_source", None))
        raw_facts = getattr(media, "classification_facts", None)
        if not source_id or not raw_facts:
            return {}
        if not isinstance(raw_facts, Mapping):
            self._warn(
                "classification_extension_facts_invalid",
                source_id,
                "classification_facts 必须是字段到 JSON 值的映射",
            )
            return {}
        with self._lock:
            registered = self._sources.get(source_id)
            if registered is None:
                self._warn(
                    "classification_extension_source_unregistered",
                    source_id,
                    "当前来源没有有效的插件分类字段声明",
                )
                return {}
            media_type = _classification_media_type(getattr(media, "type", None))
            if media_type is None or media_type not in {
                cast(ClassificationMediaType, item.value)
                for item in registered.source.media_types
            }:
                self._warn(
                    "classification_extension_media_type_mismatch",
                    source_id,
                    f"媒体类型 {_enum_text(getattr(media, 'type', None)) or '未知'} 不在来源声明中",
                )
                return {}
            accepted: dict[str, ClassificationFactValue] = {}
            prefix = f"extensions.{source_id}."
            for raw_key, value in raw_facts.items():
                field_id = str(raw_key or "").strip()
                if not field_id.startswith(prefix):
                    self._warn(
                        "classification_extension_namespace_mismatch",
                        source_id,
                        f"字段 {field_id or '<empty>'} 必须位于 {prefix} 命名空间",
                    )
                    continue
                key = field_id[len(prefix):]
                definition = registered.fields.get(key)
                if definition is None:
                    self._warn(
                        "classification_extension_field_unregistered",
                        source_id,
                        f"字段 {field_id} 未登记",
                    )
                    continue
                if media_type not in definition.media_types:
                    self._warn(
                        "classification_extension_field_media_type_mismatch",
                        source_id,
                        f"字段 {field_id} 不支持媒体类型 {media_type}",
                    )
                    continue
                if not classification_fact_matches_definition(value, definition):
                    self._warn(
                        "classification_extension_value_invalid",
                        source_id,
                        f"字段 {field_id} 的值不符合 {definition.value_type} 声明",
                    )
                    continue
                accepted[key] = cast(ClassificationFactValue, value)
            return {source_id: accepted} if accepted else {}

    def _selected_sources(
        self,
        plugin_id: str | None,
    ) -> tuple[_RegisteredMediaSource, ...]:
        """按注册顺序选择全部来源或指定插件拥有的来源。"""
        if plugin_id is None:
            return tuple(
                item
                for _source_id, item in sorted(
                    self._sources.items(),
                    key=lambda entry: (entry[1].plugin_id.casefold(), entry[0]),
                )
            )
        normalized_plugin_id = str(plugin_id).strip()
        return tuple(
            self._sources[source_id]
            for source_id in self._plugin_sources.get(normalized_plugin_id, ())
            if source_id in self._sources
        )

    @staticmethod
    def _normalize_source(
        plugin_id: str,
        declaration: Mapping[str, Any] | MediaSourceInfo,
    ) -> _RegisteredMediaSource:
        """校验并补齐一个来源声明中的命名空间、媒体类型和支持等级。"""
        try:
            source = MediaSourceInfo.model_validate(declaration)
        except ValidationError as error:
            raise PluginClassificationDeclarationError(
                f"插件 {plugin_id} 的媒体来源声明无效：{error}"
            ) from error
        source_id = source.media_source.value
        if not source.name.strip():
            raise PluginClassificationDeclarationError(
                f"插件 {plugin_id} 的媒体来源 {source_id} 缺少显示名称"
            )
        if source_id in BUILTIN_CLASSIFICATION_SOURCES:
            raise PluginClassificationDeclarationError(
                f"插件 {plugin_id} 不能覆盖内置媒体来源 {source_id}"
            )
        source_media_types = [
            cast(ClassificationMediaType, media_type.value)
            for media_type in source.media_types
        ]
        if not source_media_types:
            raise PluginClassificationDeclarationError(
                f"插件 {plugin_id} 的媒体来源 {source_id} 至少支持一种媒体类型"
            )
        normalized_fields: list[ClassificationFieldDefinition] = []
        local_fields: dict[str, ClassificationFieldDefinition] = {}
        prefix = f"extensions.{source_id}."
        for raw_field in source.classification_fields:
            field = raw_field.model_copy(deep=True)
            if not field.id.startswith(prefix):
                raise PluginClassificationDeclarationError(
                    f"扩展字段 {field.id} 必须位于 {prefix} 命名空间"
                )
            local_key = field.id[len(prefix):]
            if not local_key or any(
                not _FIELD_SEGMENT_PATTERN.fullmatch(segment)
                for segment in local_key.split(".")
            ):
                raise PluginClassificationDeclarationError(
                    f"扩展字段 {field.id} 缺少有效的来源内字段路径"
                )
            if not field.label.strip():
                raise PluginClassificationDeclarationError(
                    f"扩展字段 {field.id} 缺少显示名称"
                )
            if local_key in local_fields:
                raise PluginClassificationDeclarationError(
                    f"插件 {plugin_id} 重复声明了字段 {field.id}"
                )
            media_types = field.media_types or source_media_types
            if not set(media_types).issubset(source_media_types):
                raise PluginClassificationDeclarationError(
                    f"扩展字段 {field.id} 的媒体类型超出来源 {source_id} 声明"
                )
            allowed_operators = set(VALUE_TYPE_OPERATORS[field.value_type])
            if not field.operators:
                raise PluginClassificationDeclarationError(
                    f"扩展字段 {field.id} 必须声明至少一个操作符"
                )
            if not set(field.operators).issubset(allowed_operators):
                raise PluginClassificationDeclarationError(
                    f"扩展字段 {field.id} 声明了与 {field.value_type} 不兼容的操作符"
                )
            normalized = field.model_copy(
                deep=True,
                update={
                    "media_types": list(media_types),
                    "source_support": {source_id: "extension"},
                },
            )
            normalized_fields.append(normalized)
            local_fields[local_key] = normalized
        normalized_source = source.model_copy(
            deep=True,
            update={"classification_fields": normalized_fields},
        )
        return _RegisteredMediaSource(
            plugin_id=plugin_id,
            source=normalized_source,
            fields=local_fields,
        )

    def _warn(self, code: str, source_id: str, message: str) -> None:
        """以稳定诊断码记录被忽略的插件分类事实。"""
        self._logger.warning(
            f"忽略插件分类事实 [{code}] 来源={source_id}：{message}"
        )


def _enum_text(value: object) -> str:
    """把字符串枚举或普通值规范为去空白文本。"""
    if isinstance(value, Enum):
        value = value.value
    return str(value or "").strip()


def _classification_media_type(value: object) -> ClassificationMediaType | None:
    """把运行时媒体类型转换为分类协议支持的中文枚举值。"""
    normalized = _enum_text(value)
    if normalized not in {"电影", "电视剧", "音乐"}:
        return None
    return cast(ClassificationMediaType, normalized)
