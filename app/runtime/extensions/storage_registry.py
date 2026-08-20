"""存储后端注册表：按存储标识查得可用的存储实现。

登记由各存储模块的生命周期驱动：模块启动时把自己承载的后端登记进来，
模块停止时注销。整理编排需要成对的源、目标存储操作对象，无法经分发取得，
本表即为这类按标识直取的唯一入口。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Optional

from app.runtime.extensions.contract import ExtensionDistribution
from app.runtime.log import logger
from app.schemas.file import FileURI


def storage_backend_identity(backend: Any) -> Optional[str]:
    """
    读取存储后端声明的存储标识

    :param backend: 存储后端类或实例，标识取自其 schema 声明
    :return: 存储标识；未声明标识时为 None
    """
    schema = getattr(backend, "schema", None)
    if not schema:
        return None
    identity = getattr(schema, "value", schema)
    identity = str(identity).strip() if identity is not None else ""
    return identity or None


@dataclass(frozen=True, slots=True)
class StorageBackendEntry:
    """存储后端在注册表中的一条登记。"""

    storage_id: str
    backend: Any
    distribution: ExtensionDistribution
    owner: Optional[str] = None

    def supports(self, method: Optional[str] = None) -> bool:
        """
        判断后端是否提供指定操作

        :param method: 操作方法名，为空表示不限定操作
        :return: 未限定操作或后端提供该操作时为 True
        """
        return not method or hasattr(self.backend, method)

    def create(self) -> Any:
        """
        构造后端的操作对象

        :return: 存储操作对象
        """
        return self.backend()


def build_storage_entry(backend: Any,
                        distribution: ExtensionDistribution,
                        owner: Optional[str] = None,
                        storage_id: Optional[str] = None) -> Optional[StorageBackendEntry]:
    """
    构造登记项，标识优先取调用方显式给定的值，否则从后端声明推导

    显式标识用于登记方持有自己一套声明数据、不依赖内省后端类取得标识的场景——
    例如按 ``StorageDeclaration.schema`` 登记的扩展存储，其标识来自声明字段而非
    ``impl.schema``，两者允许不同。

    :param backend: 存储后端类
    :param distribution: 后端的发行方式
    :param owner: 提供该后端的扩展标识
    :param storage_id: 显式指定的存储标识，为空时从后端的 schema 属性推导
    :return: 登记项；标识缺失或无法作为路径前缀时为 None
    """
    identity = (storage_id or "").strip() or storage_backend_identity(backend)
    source = owner or getattr(backend, "__name__", backend)
    if not identity:
        logger.error(f"【存储】{source} 未声明存储标识，无法登记")
        return None
    if not FileURI.is_storage_scheme(identity):
        logger.error(f"【存储】{source} 的存储标识 {identity} 不能作为路径前缀，无法登记")
        return None
    return StorageBackendEntry(
        storage_id=identity,
        backend=backend,
        distribution=distribution,
        owner=owner,
    )


class StorageBackendRegistry:
    """按存储标识登记存储后端。"""

    def __init__(self) -> None:
        """创建登记表。"""
        self._lock = threading.RLock()
        self._entries: dict[str, StorageBackendEntry] = {}
        # 内建后端最近一次登记的快照，覆盖登记被撤销时据此还原内建取值
        self._builtin_entries: dict[str, StorageBackendEntry] = {}

    def register(self, backend: Any,
                 distribution: ExtensionDistribution = ExtensionDistribution.BUILTIN,
                 owner: Optional[str] = None,
                 storage_id: Optional[str] = None) -> Optional[str]:
        """
        登记一个存储后端，同标识重复登记以最新一次为准

        :param backend: 存储后端类
        :param distribution: 后端的发行方式
        :param owner: 提供该后端的扩展标识
        :param storage_id: 显式指定的存储标识，为空时从后端的 schema 属性推导
        :return: 登记成功的存储标识；登记失败时为 None
        """
        entry = build_storage_entry(backend, distribution, owner, storage_id)
        if not entry:
            return None
        with self._lock:
            self._entries[entry.storage_id] = entry
            if distribution == ExtensionDistribution.BUILTIN:
                self._builtin_entries[entry.storage_id] = entry
        return entry.storage_id

    def unregister(self, storage_id: str, owner: Optional[str] = None) -> bool:
        """
        注销指定存储标识的后端

        内建后端注销即真正腾空标识；覆盖了内建后端的登记被注销后，该标识按其
        最近一次内建登记的快照还原，不会因扩展停用而让内建后端整体消失。

        给出 ``owner`` 时只注销当前仍归属该登记方的条目。标识被更晚的登记接管后，
        原登记方停自己那一份不应连带把接管方踢掉——内建模块重启即属此列。

        :param storage_id: 存储标识
        :param owner: 注销方标识，为空时不校验归属
        :return: 该标识原本已登记且归属校验通过时为 True
        """
        with self._lock:
            if owner is not None:
                current = self._entries.get(storage_id)
                if current is None or current.owner != owner:
                    return False
            return self._unregister_locked(storage_id)

    def _unregister_locked(self, storage_id: str) -> bool:
        """
        在已持有锁的前提下注销一个存储标识，供内部批量操作复用

        :param storage_id: 存储标识
        :return: 该标识原本已登记时为 True
        """
        removed = self._entries.pop(storage_id, None)
        if removed is None:
            return False
        if removed.distribution == ExtensionDistribution.BUILTIN:
            self._builtin_entries.pop(storage_id, None)
        else:
            builtin_entry = self._builtin_entries.get(storage_id)
            if builtin_entry is not None:
                self._entries[storage_id] = builtin_entry
        return True

    def unregister_owner(self, owner: str) -> tuple[str, ...]:
        """
        注销指定登记方当前仍生效的全部存储标识

        条目一旦被更晚的登记覆盖，owner 随之更新为新的登记方，因此本方法只回收
        标识当前仍归属该登记方的条目，不会波及后来居上、已接管同一标识的登记方。

        :param owner: 登记方标识
        :return: 被注销的存储标识元组
        """
        with self._lock:
            owned = tuple(
                storage_id for storage_id, entry in self._entries.items()
                if entry.owner == owner
            )
            for storage_id in owned:
                self._unregister_locked(storage_id)
            return owned

    def entries(self) -> tuple[StorageBackendEntry, ...]:
        """
        列出当前可用的全部登记项

        :return: 登记项元组，按登记顺序排列
        """
        with self._lock:
            return tuple(self._entries.values())

    def storage_ids(self) -> tuple[str, ...]:
        """
        列出当前可用的全部存储标识

        :return: 存储标识元组
        """
        return tuple(entry.storage_id for entry in self.entries())

    def find(self, storage_id: str,
             method: Optional[str] = None) -> Optional[StorageBackendEntry]:
        """
        查找指定存储标识的登记项

        :param storage_id: 存储标识
        :param method: 需要后端提供的操作方法名，为空表示不限定操作
        :return: 登记项；未登记或不提供该操作时为 None
        """
        if not storage_id:
            return None
        with self._lock:
            entry = self._entries.get(storage_id)
        if not entry or not entry.supports(method):
            return None
        return entry

    def resolve(self, storage_id: str, method: Optional[str] = None) -> Optional[Any]:
        """
        取得指定存储标识的操作对象

        :param storage_id: 存储标识
        :param method: 需要后端提供的操作方法名，为空表示不限定操作
        :return: 存储操作对象；未登记或不提供该操作时为 None
        """
        entry = self.find(storage_id, method)
        return entry.create() if entry else None

    def diagnose(self) -> list[dict[str, Any]]:
        """
        输出只读的登记诊断信息

        :return: 每个存储标识的标识、发行方式与提供方
        """
        return [
            {
                "storage": entry.storage_id,
                "distribution": entry.distribution.value,
                "owner": entry.owner,
            }
            for entry in self.entries()
        ]


storage_backend_registry = StorageBackendRegistry()
