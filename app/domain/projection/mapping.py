"""来源投影共享的不可变映射构造机制。"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any


def _copy_value(value: Any) -> Any:
    """递归复制只读容器，避免 ``mappingproxy`` 等对象破坏投影合同。"""
    if isinstance(value, Mapping):
        return {_copy_value(key): _copy_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_copy_value(item) for item in value)
    if isinstance(value, set):
        return {_copy_value(item) for item in value}
    if isinstance(value, frozenset):
        return frozenset(_copy_value(item) for item in value)
    return deepcopy(value)


class ProjectionBuilder:
    """在不修改来源和当前快照的前提下累积字段投影。"""

    def __init__(self, current: Mapping[str, Any]) -> None:
        """保存当前领域字段快照，后续读取同时考虑已投影值。"""
        self._current = current
        self._changes: dict[str, Any] = {}

    def get(self, name: str, default: Any = None) -> Any:
        """返回已投影值，未投影时回退到当前字段快照。"""
        return self._changes.get(name, self._current.get(name, default))

    def set(self, name: str, value: Any) -> None:
        """记录字段的新值，并隔离可变来源对象。"""
        self._changes[name] = _copy_value(value)

    def set_missing(self, name: str, value: Any) -> None:
        """仅当当前字段为空时记录来源值。"""
        if not self.get(name):
            self.set(name, value)

    def fill_missing(
        self,
        source: Mapping[str, Any],
        *,
        skip: frozenset[str] = frozenset(),
    ) -> None:
        """按历史同类型规则补齐当前快照中存在的空字段。"""
        for name, value in source.items():
            if name in skip or not value or name not in self._current:
                continue
            current_value = self.get(name)
            if current_value:
                continue
            if current_value is None or type(current_value) is type(value):
                self.set(name, value)

    def build(self) -> dict[str, Any]:
        """返回与当前快照隔离的投影结果。"""
        return dict(self._changes)
