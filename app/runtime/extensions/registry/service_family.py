"""服务族登记表：宿主按「一份配置扇出一个具名实例」消费的服务族目录。

服务实例声明的 ``capability`` 取值须是本表登记过的族。族是登记出来的而不是写死在
声明面上的枚举——族回答「这类服务长什么样」，宿主没有理由预先穷举它。

本表登记的是族本身的元数据：能力标签、展示名称与归属。族里有哪些类型、每个类型
按配置扇出几个实例，由 `app.runtime.extensions.registry.service_instance` 承载，
两张表回答的不是同一个问题。「这一族配置存放在哪个配置键下、默认标记的作用域是族
还是类型」同样不在本表，它们是宿主内部实现，收在
`app.runtime.extensions.service_config`。

内建族在本模块导入时登记，因此任何取用本表的路径看见的族集合都相同，不取决于组合
根有没有跑过——契约校验与配置界面端点都按本表回答，它们在组合根之外也会被调用。

列举顺序按能力标签升序，与登记先后无关：候选列表会出现在拒绝声明的提示里，而宿主
内部的登记先后用户既看不见也无法预期。判据见 docs/plugin-extension-architecture.md §7.2。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from app.runtime.extensions.contract.extension import ExtensionDistribution
from app.runtime.log import logger
from app.schemas.types import ModuleType

# 宿主自带的服务族能力标签与展示名称。族由宿主登记、类型由谁提供是另一回事：登录
# 认证族没有任何内建类型，全部登录入口类型都来自扩展声明，族本身仍归宿主登记，否则
# 第一个提供登录入口的扩展会连族一起带进来，卸载它即让存量配置整族失去归属。
_BUILTIN_SERVICE_FAMILIES: Tuple[Tuple[str, str], ...] = (
    (ModuleType.Downloader.value, "下载器"),
    (ModuleType.MediaServer.value, "媒体服务器"),
    (ModuleType.Notification.value, "消息通知"),
    (ModuleType.Storage.value, "存储"),
    (ModuleType.Auth.value, "登录认证"),
)


@dataclass(frozen=True, slots=True)
class ServiceFamilyEntry:
    """服务族在注册表中的一条登记。

    :param capability: 能力标签，服务实例声明的 ``capability`` 即取此值
    :param name: 族的展示名称
    :param distribution: 登记方的发行方式
    :param owner: 登记方实例键，宿主内建族为 None
    """

    capability: str
    name: str
    distribution: ExtensionDistribution
    owner: Optional[str] = None


class ServiceFamilyRegistry:
    """按能力标签登记可声明服务实例的服务族。"""

    def __init__(self) -> None:
        """创建登记表。"""
        self._lock = threading.RLock()
        self._entries: Dict[str, ServiceFamilyEntry] = {}

    def register(self,
                 capability: str,
                 name: Optional[str] = None,
                 owner: Optional[str] = None,
                 distribution: ExtensionDistribution = ExtensionDistribution.BUILTIN
                 ) -> Optional[str]:
        """登记一族服务，同一能力标签重复登记以最新一次为准。

        :param capability: 能力标签
        :param name: 展示名称，为空时取能力标签
        :param owner: 登记方实例键，为空表示宿主内建族
        :param distribution: 登记方的发行方式
        :return: 登记成功的能力标签；标签不是非空字符串时为 None
        """
        identity = capability.strip() if isinstance(capability, str) else ""
        if not identity:
            logger.error(f"【服务】{owner or '宿主'} 的服务族登记缺少能力标签，无法登记")
            return None
        entry = ServiceFamilyEntry(
            capability=identity,
            name=(name or "").strip() or identity,
            distribution=distribution,
            owner=owner,
        )
        with self._lock:
            self._entries[identity] = entry
        return identity

    def unregister_owner(self, owner: str) -> Tuple[str, ...]:
        """注销指定登记方当前仍生效的全部服务族。

        族一旦被更晚的登记覆盖，owner 随之更新为新的登记方，因此本方法只回收当前
        仍归属该登记方的条目，不会波及后来居上、已接管同一标签的登记方。

        :param owner: 登记方实例键
        :return: 被注销的能力标签元组，按标签升序排列
        """
        with self._lock:
            owned = tuple(sorted(
                capability for capability, entry in self._entries.items()
                if entry.owner == owner
            ))
            for capability in owned:
                self._entries.pop(capability, None)
            return owned

    def find(self, capability: str) -> Optional[ServiceFamilyEntry]:
        """查找指定能力标签的登记项。

        :param capability: 能力标签
        :return: 登记项；未登记时为 None
        """
        if not capability:
            return None
        with self._lock:
            return self._entries.get(capability)

    def is_registered(self, capability: str) -> bool:
        """判断指定能力标签是否为已登记的服务族。

        :param capability: 能力标签
        :return: 该标签已登记时为 True
        """
        return self.find(capability) is not None

    def entries(self) -> Tuple[ServiceFamilyEntry, ...]:
        """列出当前登记的全部服务族。

        :return: 登记项元组，按能力标签升序排列
        """
        with self._lock:
            return tuple(sorted(
                self._entries.values(), key=lambda entry: entry.capability
            ))

    def capabilities(self) -> Tuple[str, ...]:
        """列出当前登记的全部能力标签。

        :return: 能力标签元组，按标签升序排列
        """
        return tuple(entry.capability for entry in self.entries())


def register_builtin_service_families(registry: ServiceFamilyRegistry) -> None:
    """把内建服务族登记进指定登记表。

    :param registry: 目标登记表
    :return: 无返回值
    """
    for capability, name in _BUILTIN_SERVICE_FAMILIES:
        registry.register(capability, name)


service_family_registry = ServiceFamilyRegistry()
register_builtin_service_families(service_family_registry)
