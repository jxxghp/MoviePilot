"""Agent 本地工具目录的不可变快照与严格解析。"""

import hashlib
import json
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Optional

from pydantic import BaseModel

from app.agent.policy.contracts import ToolRevision


class ToolCatalogError(RuntimeError):
    """工具目录无法建立可信当前视图时的稳定失败。"""


class ToolIdentityAmbiguousError(ToolCatalogError):
    """同一工具名对应多个实现，无法进行严格解析。"""


def _stable_json(value: Any) -> str:
    """生成工具身份摘要使用的稳定 JSON。"""
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _schema_digest(tool: Any) -> str:
    """计算工具当前 Pydantic 参数契约摘要。"""
    args_schema = getattr(tool, "args_schema", None)
    if isinstance(args_schema, type) and issubclass(args_schema, BaseModel):
        schema = args_schema.model_json_schema()
    elif isinstance(args_schema, Mapping):
        schema = dict(args_schema)
    else:
        schema = {"type": "object", "properties": {}}
    return hashlib.sha256(_stable_json(schema).encode("utf-8")).hexdigest()


def _implementation_identity(tool: Any) -> str:
    """返回不依赖对象地址、可区分动态绑定的工具实现身份。"""
    tool_class = type(tool)
    implementation = f"{tool_class.__module__}.{tool_class.__qualname__}"
    binding = str(getattr(tool, "_agent_tool_binding", "") or "")
    return f"{implementation}:{binding}" if binding else implementation


@dataclass(frozen=True)
class ToolCatalogEntry:
    """绑定一次目录构造中精确工具实例的身份记录。"""

    name: str
    source: str
    identity: str
    description_digest: str
    schema_digest: str
    revision: ToolRevision
    tool: Any = field(repr=False, compare=False)


@dataclass(frozen=True)
class ToolCatalogSnapshot:
    """图构造、缓存签名和身份碰撞审计共享的本地工具事实源。"""

    entries: tuple[ToolCatalogEntry, ...]
    plugin_revision: int
    factory_revision: str
    _by_name: Mapping[str, tuple[ToolCatalogEntry, ...]] = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        """建立只读名称索引并保留所有冲突项。"""
        by_name: dict[str, list[ToolCatalogEntry]] = {}
        for entry in self.entries:
            by_name.setdefault(entry.name, []).append(entry)
        object.__setattr__(
            self,
            "_by_name",
            MappingProxyType(
                {name: tuple(matches) for name, matches in by_name.items()}
            ),
        )

    @classmethod
    def from_tools(
        cls,
        tools: list[Any],
        *,
        plugin_revision: int,
        factory_revision: str,
    ) -> "ToolCatalogSnapshot":
        """从已完成上下文注入的精确实例建立不可变目录。"""
        entries = []
        for tool in tools:
            name = str(getattr(tool, "name", "") or "")
            if not name:
                raise ToolCatalogError("工具缺少稳定名称")
            source = str(getattr(tool, "_agent_tool_source", "builtin"))
            schema_digest = _schema_digest(tool)
            description_digest = hashlib.sha256(
                str(getattr(tool, "description", "") or "").encode("utf-8")
            ).hexdigest()
            implementation = _implementation_identity(tool)
            revision = ToolRevision(
                implementation=implementation,
                factory=factory_revision,
                plugin=str(plugin_revision),
            )
            entries.append(
                ToolCatalogEntry(
                    name=name,
                    source=source,
                    identity=f"{source}:{implementation}:{schema_digest}",
                    description_digest=description_digest,
                    schema_digest=schema_digest,
                    revision=revision,
                    tool=tool,
                )
            )
        return cls(
            entries=tuple(entries),
            plugin_revision=plugin_revision,
            factory_revision=factory_revision,
        )

    @property
    def tools(self) -> list[Any]:
        """按目录顺序返回精确工具实例。"""
        return [entry.tool for entry in self.entries]

    @property
    def collisions(self) -> Mapping[str, tuple[ToolCatalogEntry, ...]]:
        """返回所有同名工具，不按注册顺序隐式选胜者。"""
        return MappingProxyType(
            {name: entries for name, entries in self._by_name.items() if len(entries) > 1}
        )

    @property
    def signature(self) -> tuple[Any, ...]:
        """返回可参与 Agent 图缓存的完整目录签名。"""
        return (
            self.factory_revision,
            self.plugin_revision,
            tuple(
                (
                    entry.name,
                    entry.identity,
                    entry.description_digest,
                    entry.schema_digest,
                )
                for entry in self.entries
            ),
        )

    def resolve_unique(self, name: str) -> Optional[ToolCatalogEntry]:
        """严格解析当前唯一实现；重名时拒绝继承 first/last-wins。"""
        entries = self._by_name.get(name, ())
        if len(entries) > 1:
            raise ToolIdentityAmbiguousError("TOOL_IDENTITY_AMBIGUOUS")
        return entries[0] if entries else None

    def select(self, tools: list[Any]) -> "ToolCatalogSnapshot":
        """为子图保留所选工具名的全部候选身份与 revision 语义。"""
        selected_names = {
            str(getattr(tool, "name", "") or "") for tool in tools
        }
        return ToolCatalogSnapshot(
            entries=tuple(
                entry for entry in self.entries if entry.name in selected_names
            ),
            plugin_revision=self.plugin_revision,
            factory_revision=self.factory_revision,
        )


__all__ = [
    "ToolCatalogEntry",
    "ToolCatalogError",
    "ToolCatalogSnapshot",
    "ToolIdentityAmbiguousError",
]
