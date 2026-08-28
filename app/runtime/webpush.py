"""进程内 Web Push 订阅登记表。"""

from __future__ import annotations

import builtins
import threading
from collections.abc import Mapping
from typing import Any


class WebPushRegistry:
    """按 endpoint 原子维护 Web Push 订阅快照。"""

    def __init__(self) -> None:
        """初始化空订阅表和兼容锁。"""
        self._items: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def list(self) -> list[dict[str, Any]]:
        """返回当前订阅的浅拷贝，调用方不能改写 registry 容器。"""
        with self._lock:
            return list(self._items)

    def upsert(self, subscription: Mapping[str, Any]) -> None:
        """按 endpoint 新增或替换订阅；缺少 endpoint 时忽略。"""
        endpoint = subscription.get("endpoint") if subscription else None
        if not endpoint:
            return
        stored = dict(subscription)
        with self._lock:
            for index, current in enumerate(self._items):
                if current.get("endpoint") == endpoint:
                    self._items[index] = stored
                    return
            self._items.append(stored)

    def remove(self, subscription: Mapping[str, Any]) -> bool:
        """按 endpoint 删除订阅，并返回是否实际删除。"""
        endpoint = subscription.get("endpoint") if subscription else None
        if not endpoint:
            return False
        with self._lock:
            before = len(self._items)
            self._items[:] = [
                current
                for current in self._items
                if current.get("endpoint") != endpoint
            ]
            return len(self._items) != before

    def clear(self) -> None:
        """清空 registry，供生命周期重启和隔离测试使用。"""
        with self._lock:
            self._items.clear()

    @property
    def compat_items(self) -> builtins.list[dict[str, Any]]:
        """返回旧 ABI 使用的原始可变列表；canonical 代码不得调用。"""
        return self._items

    @property
    def compat_lock(self) -> threading.Lock:
        """返回与旧原始列表配套的锁；canonical 代码不得调用。"""
        return self._lock


webpush_registry = WebPushRegistry()
