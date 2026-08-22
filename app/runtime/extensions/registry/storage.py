"""存储后端注册表：按存储令牌查得可用的存储实现。

登记由各存储模块的生命周期驱动：模块启动时把自己承载的后端登记进来，
模块停止时注销。整理编排需要成对的源、目标存储操作对象，无法经分发取得，
本表即为这类按标识直取的唯一入口。

同一存储标识下可登记多个具名实例，登记键为 ``(存储标识, 实例名)``。未给出实例名的
登记占据该标识的裸令牌位，裸令牌 ``u115`` 即落到它；具名登记以 ``u115@work``
形式的令牌取用。覆盖与内建快照还原按整条登记键进行，不同实例之间互不影响。

本表只回答地址问题：某个令牌指的实体是谁。「调用没指定存储时用哪个」是族级默认调用
目标，落服务实例配置表的专列，与本表无关；本表里的 ``bare_token_target`` 是兼容指针，
只在令牌缺实例段时参与裁决，判据与退场路径见 ``app.application.storage_config``。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from app.runtime.extensions.contract.extension import ExtensionDistribution
from app.runtime.extensions.contract.instance import describe_instance_candidates
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


def create_storage_backend(backend: Any, instance: Optional[str] = None,
                           bare_token_target: bool = False) -> Any:
    """
    构造服务于指定存储实例的操作对象

    实例归属优先经构造参数交给后端：按实例区分连接的后端在初始化时就要用归属读配置，
    晚一步交付会拿裸令牌那一份的账号连上去。后端不接受该参数时退回无参构造再标注归属，
    未按实例区分连接的后端因此无须改动构造签名。

    :param backend: 存储后端类
    :param instance: 实例名，None 表示该存储类型的裸令牌位
    :param bare_token_target: 该实例是否承接所属存储类型的裸令牌
    :return: 存储操作对象
    """
    if getattr(type(backend), "accepts_storage_instance", False):
        storage = backend(storage_instance=instance)
    else:
        storage = backend()
        if hasattr(storage, "storage_instance"):
            storage.storage_instance = instance
    if hasattr(storage, "storage_is_bare_token"):
        storage.storage_is_bare_token = bool(bare_token_target) or instance is None
    return storage


def storage_instance_factory(backend: Any) -> Any:
    """
    把存储后端类包成服务实例类型目录接受的实例工厂

    这是存储族的宿主默认工厂：声明只给出后端类、不给 ``factory`` 时宿主用它构造实例，
    扩展作者因此一行工厂都不用写。存储的构造协议与三族不同——配置不经构造参数传入，
    后端按自己的实例归属懒读配置，这样存储配置才能在运行期改写后重连；把这条规则
    包在宿主里而不是要求每个作者手写，是为了让归属交付只有一处实现。

    :param backend: 存储后端类
    :return: 接收单条实例配置并返回存储操作对象的工厂
    """

    def factory(conf: Any) -> Any:
        """按单条实例配置构造该实例的存储操作对象。"""
        return create_storage_backend(
            backend,
            (getattr(conf, "name", None) or "").strip() or None,
            bool(getattr(conf, "bare_token_target", False)),
        )

    return factory


@dataclass(frozen=True, slots=True)
class StorageBackendEntry:
    """存储后端在注册表中的一条登记。

    配置界面二选一：``config_form`` 为 vuetify 模式，``config_component``
    为 vue 模式的已解析组件描述（组件名加联邦远程入口）；内建登记与未声明
    界面的扩展登记二者均为 None，此时前端沿用内建类型的渲染方式，不视为异常。

    :param config_form: 登记方为该存储标识声明的专属配置界面，形状为
        (组件树, 默认数据) 二元组
    :param config_component: 登记方为该存储标识声明的 vue 模式配置组件，
        形状为 ``{"component": 组件名, "remote": 联邦远程入口描述}``
    :param instance: 实例名，为 None 表示本条登记占据该存储标识的裸令牌位
    :param bare_token_target: 本条具名登记是否承接该存储标识的裸令牌
    """

    storage_id: str
    backend: Any
    distribution: ExtensionDistribution
    owner: Optional[str] = None
    config_form: Optional[Tuple[List[Dict[str, Any]], Dict[str, Any]]] = None
    config_component: Optional[Dict[str, Any]] = None
    instance: Optional[str] = None
    bare_token_target: bool = False

    @property
    def storage(self) -> str:
        """
        本条登记的存储令牌

        :return: 存储令牌，未具名实例即裸存储标识
        """
        return FileURI.join_storage(self.storage_id, self.instance)

    @property
    def key(self) -> Tuple[str, Optional[str]]:
        """
        本条登记在注册表中的键

        :return: (存储标识, 实例名) 二元组
        """
        return self.storage_id, self.instance

    def supports(self, method: Optional[str] = None) -> bool:
        """
        判断后端是否提供指定操作

        :param method: 操作方法名，为空表示不限定操作
        :return: 未限定操作或后端提供该操作时为 True
        """
        return not method or hasattr(self.backend, method)

    def create(self) -> Any:
        """
        构造后端的操作对象，并交付本条登记所属的实例归属

        交付归属是必需的：同一后端类可能被同一存储标识的多个实例位共用，不交付则取出
        的对象不知道自己服务哪个实例，读写配置会落到裸令牌那一份上。

        :return: 存储操作对象
        """
        return create_storage_backend(self.backend, self.instance, self.bare_token_target)


def build_storage_entry(backend: Any,
                        distribution: ExtensionDistribution,
                        owner: Optional[str] = None,
                        storage_id: Optional[str] = None,
                        config_form: Optional[Tuple[List[Dict[str, Any]], Dict[str, Any]]] = None,
                        config_component: Optional[Dict[str, Any]] = None,
                        instance: Optional[str] = None,
                        bare_token_target: bool = False
                        ) -> Optional[StorageBackendEntry]:
    """
    构造登记项，标识优先取调用方显式给定的值，否则从后端声明推导

    显式标识用于登记方持有自己一套声明数据、不依赖内省后端类取得标识的场景——
    例如按 ``ServiceInstanceDeclaration.type`` 登记的扩展存储，其标识来自声明字段
    而非 ``impl.schema``，两者允许不同。标识只承载存储类型，实例名另经 ``instance``
    给出，带实例分隔符的标识按非法处理。

    :param backend: 存储后端类
    :param distribution: 后端的发行方式
    :param owner: 提供该后端的扩展标识
    :param storage_id: 显式指定的存储标识，为空时从后端的 schema 属性推导
    :param config_form: 登记方为该标识声明的专属配置界面（vuetify 模式），
        不给出时该标识没有专属界面
    :param config_component: 登记方为该标识声明的已解析 vue 模式配置组件，
        不给出时该标识没有专属界面
    :param instance: 实例名，为空表示登记为该标识的裸令牌位
    :param bare_token_target: 该具名实例是否承接该标识的裸令牌，未具名时不生效
    :return: 登记项；标识缺失、无法作为路径前缀或实例名不合法时为 None
    """
    identity = (storage_id or "").strip() or storage_backend_identity(backend)
    source = owner or getattr(backend, "__name__", backend)
    if not identity:
        logger.error(f"【存储】{source} 未声明存储标识，无法登记")
        return None
    if not FileURI.is_storage_scheme(identity):
        logger.error(f"【存储】{source} 的存储标识 {identity} 不能作为路径前缀，无法登记")
        return None
    instance_name = (instance or "").strip() or None
    if instance_name is not None and not FileURI.is_storage_instance(instance_name):
        logger.error(f"【存储】{source} 的存储实例名 {instance_name} 不合法，无法登记")
        return None
    return StorageBackendEntry(
        storage_id=identity,
        backend=backend,
        distribution=distribution,
        owner=owner,
        config_form=config_form,
        config_component=config_component,
        instance=instance_name,
        bare_token_target=bool(bare_token_target) and instance_name is not None,
    )


class StorageBackendRegistry:
    """按 (存储标识, 实例名) 登记存储后端。"""

    def __init__(self) -> None:
        """创建登记表。"""
        self._lock = threading.RLock()
        self._entries: dict[Tuple[str, Optional[str]], StorageBackendEntry] = {}
        # 内建后端最近一次登记的快照，覆盖登记被撤销时据此还原内建取值
        self._builtin_entries: dict[Tuple[str, Optional[str]], StorageBackendEntry] = {}

    def register(self, backend: Any,
                 distribution: ExtensionDistribution = ExtensionDistribution.BUILTIN,
                 owner: Optional[str] = None,
                 storage_id: Optional[str] = None,
                 config_form: Optional[Tuple[List[Dict[str, Any]], Dict[str, Any]]] = None,
                 config_component: Optional[Dict[str, Any]] = None,
                 instance: Optional[str] = None,
                 bare_token_target: bool = False
                 ) -> Optional[str]:
        """
        登记一个存储后端，同一 (存储标识, 实例名) 重复登记以最新一次为准

        不给实例名即登记为该标识的裸令牌位，与只有单一实现时的行为一致；给出实例名
        则与同标识的其它实例并存，互不覆盖。

        :param backend: 存储后端类
        :param distribution: 后端的发行方式
        :param owner: 提供该后端的扩展标识
        :param storage_id: 显式指定的存储标识，为空时从后端的 schema 属性推导
        :param config_form: 登记方为该标识声明的专属配置界面（vuetify 模式），
            不给出时沿用既有调用点不传该参数时的行为
        :param config_component: 登记方为该标识声明的已解析 vue 模式配置组件，
            不给出时沿用既有调用点不传该参数时的行为
        :param instance: 实例名，为空表示登记为该标识的裸令牌位
        :param bare_token_target: 该具名实例是否承接该标识的裸令牌，未具名时不生效
        :return: 登记成功的存储令牌；登记失败时为 None
        """
        entry = build_storage_entry(
            backend, distribution, owner, storage_id, config_form, config_component,
            instance, bare_token_target
        )
        if not entry:
            return None
        with self._lock:
            self._entries[entry.key] = entry
            if distribution == ExtensionDistribution.BUILTIN:
                self._builtin_entries[entry.key] = entry
        return entry.storage

    def unregister(self, storage: str, owner: Optional[str] = None,
                   instance: Optional[str] = None) -> bool:
        """
        注销指定存储令牌的后端

        内建后端注销即真正腾空该实例位；覆盖了内建后端的登记被注销后，该实例位按其
        最近一次内建登记的快照还原，不会因扩展停用而让内建后端整体消失。

        给出 ``owner`` 时只注销当前仍归属该登记方的条目。实例位被更晚的登记接管后，
        原登记方停自己那一份不应连带把接管方踢掉——内建模块重启即属此列。

        :param storage: 存储令牌，如 u115 或 u115@work
        :param owner: 注销方标识，为空时不校验归属
        :param instance: 实例名，给出时覆盖令牌中携带的实例名
        :return: 该实例位原本已登记且归属校验通过时为 True
        :raises ValueError: 存储令牌带实例分隔符但不合法
        """
        if not storage:
            return False
        key = self._entry_key(storage, instance)
        with self._lock:
            if owner is not None:
                current = self._entries.get(key)
                if current is None or current.owner != owner:
                    return False
            return self._unregister_locked(key)

    @staticmethod
    def _entry_key(storage: str, instance: Optional[str] = None) -> Tuple[str, Optional[str]]:
        """
        把存储令牌与显式实例名合成登记键

        :param storage: 存储令牌
        :param instance: 实例名，给出时覆盖令牌中携带的实例名
        :return: (存储标识, 实例名) 二元组
        :raises ValueError: 存储令牌带实例分隔符但不合法
        """
        identity, token_instance = FileURI.split_storage(storage)
        return identity, instance if instance is not None else token_instance

    def _unregister_locked(self, key: Tuple[str, Optional[str]]) -> bool:
        """
        在已持有锁的前提下注销一条登记，供内部批量操作复用

        :param key: (存储标识, 实例名) 二元组
        :return: 该实例位原本已登记时为 True
        """
        removed = self._entries.pop(key, None)
        if removed is None:
            return False
        if removed.distribution == ExtensionDistribution.BUILTIN:
            self._builtin_entries.pop(key, None)
        else:
            builtin_entry = self._builtin_entries.get(key)
            if builtin_entry is not None:
                self._entries[key] = builtin_entry
        return True

    def unregister_owner(self, owner: str) -> tuple[str, ...]:
        """
        注销指定登记方当前仍生效的全部存储登记

        条目一旦被更晚的登记覆盖，owner 随之更新为新的登记方，因此本方法只回收
        当前仍归属该登记方的条目，不会波及后来居上、已接管同一实例位的登记方。

        :param owner: 登记方标识
        :return: 被注销的存储令牌元组
        """
        with self._lock:
            owned = tuple(
                entry for entry in self._entries.values() if entry.owner == owner
            )
            for entry in owned:
                self._unregister_locked(entry.key)
            return tuple(entry.storage for entry in owned)

    def entries(self) -> tuple[StorageBackendEntry, ...]:
        """
        列出当前可用的全部登记项

        :return: 登记项元组，按登记顺序排列
        """
        with self._lock:
            return tuple(self._entries.values())

    def storage_ids(self) -> tuple[str, ...]:
        """
        列出当前可用的全部存储标识，同标识的多个实例只出现一次

        :return: 存储标识元组
        """
        return tuple(dict.fromkeys(entry.storage_id for entry in self.entries()))

    def storage_tokens(self) -> tuple[str, ...]:
        """
        列出当前可用的全部存储令牌，具名实例各占一项

        :return: 存储令牌元组
        """
        return tuple(entry.storage for entry in self.entries())

    def instances(self, storage_id: str) -> tuple[StorageBackendEntry, ...]:
        """
        列出指定存储标识下的全部实例登记

        :param storage_id: 存储标识
        :return: 登记项元组，按实例名升序排列，未具名实例排在最前
        """
        return tuple(sorted(
            (entry for entry in self.entries() if entry.storage_id == storage_id),
            key=lambda item: (item.instance is not None, item.instance or ""),
        ))

    def bare_token_entry(self, storage_id: str) -> Optional[StorageBackendEntry]:
        """
        裁决指定存储标识的裸令牌落在哪条登记上

        未具名的登记就是该标识的裸令牌位，优先命中；全部为具名实例时只认唯一一个
        自称承接裸令牌的实例。无人自称、或多个实例同时自称，一律报错而不按登记顺序
        取任意一个——已停用的实例在本表中即已注销，与从未自称过同属无人承接。

        :param storage_id: 存储标识
        :return: 承接裸令牌的登记项；该标识一条登记都没有时为 None
        :raises LookupError: 该标识有登记但无法裁决出承接裸令牌的实例
        """
        with self._lock:
            unnamed = self._entries.get((storage_id, None))
            if unnamed is not None:
                return unnamed
            named = [
                entry for entry in self._entries.values()
                if entry.storage_id == storage_id
            ]
        if not named:
            return None
        marked = [entry for entry in named if entry.bare_token_target]
        if len(marked) == 1:
            return marked[0]
        candidates = describe_instance_candidates(
            (entry.instance, True)
            for entry in sorted(named, key=lambda item: item.instance or "")
        )
        if marked:
            raise LookupError(
                f"存储 {storage_id} 有多个实例自称承接裸令牌，调用必须显式指定实例；"
                f"可选实例：{candidates}"
            )
        raise LookupError(
            f"存储 {storage_id} 没有承接裸令牌的实例，调用必须显式指定实例；"
            f"可选实例：{candidates}"
        )

    def find(self, storage: str, method: Optional[str] = None,
             instance: Optional[str] = None) -> Optional[StorageBackendEntry]:
        """
        查找指定存储令牌的登记项

        令牌未带实例名且未显式给出实例名时走裸令牌兼容指针裁决。

        :param storage: 存储令牌，如 u115 或 u115@work
        :param method: 需要后端提供的操作方法名，为空表示不限定操作
        :param instance: 实例名，给出时覆盖令牌中携带的实例名
        :return: 登记项；未登记或不提供该操作时为 None
        :raises ValueError: 存储令牌带实例分隔符但不合法
        :raises LookupError: 未指定实例，且该标识有登记但无人承接裸令牌
        """
        if not storage:
            return None
        storage_id, selected = self._entry_key(storage, instance)
        if not storage_id:
            return None
        if selected is not None:
            with self._lock:
                entry = self._entries.get((storage_id, selected))
        else:
            entry = self.bare_token_entry(storage_id)
        if not entry or not entry.supports(method):
            return None
        return entry

    def resolve(self, storage: str, method: Optional[str] = None,
                instance: Optional[str] = None) -> Optional[Any]:
        """
        取得指定存储令牌的操作对象

        :param storage: 存储令牌，如 u115 或 u115@work
        :param method: 需要后端提供的操作方法名，为空表示不限定操作
        :param instance: 实例名，给出时覆盖令牌中携带的实例名
        :return: 存储操作对象；未登记或不提供该操作时为 None
        :raises ValueError: 存储令牌带实例分隔符但不合法
        :raises LookupError: 未指定实例，且该标识有登记但无人承接裸令牌
        """
        entry = self.find(storage, method, instance)
        return entry.create() if entry else None

    def diagnose(self) -> list[dict[str, Any]]:
        """
        输出只读的登记诊断信息

        :return: 每条登记的存储令牌、存储标识、实例名、是否承接裸令牌、发行方式与提供方
        """
        return [
            {
                "storage": entry.storage,
                "storage_id": entry.storage_id,
                "instance": entry.instance,
                "bare_token_target": entry.bare_token_target,
                "distribution": entry.distribution.value,
                "owner": entry.owner,
            }
            for entry in self.entries()
        ]


storage_backend_registry = StorageBackendRegistry()
