"""服务实例族扩展实现具名实例时要满足的形状，以及本次运行认哪些族。

下载器、媒体服务器与消息通知三族按**方法名**判定实例形状，不按基类继承：宿主的族级
取用链上直调这几个方法，实现类在不在某个基类的 MRO 上无关紧要；走 ``factory`` 路径的
声明宿主拿不到类型，更无从要求继承。因此本模块给的是协议而不是基类——继承它可选，只要
方法在场同样通过登记，不继承的实现不会因此被拒。

协议只写取用链上直调的那几个方法，与宿主的必填集一一对应，绑定由
``tests/test_plugin_sdk.py`` 判定。``service_instance_required_methods()`` 直读同一份
必填集，回答的是「这一族现在必填什么」：没有协议的族由它答出空元组，宿主将来新增的可选
契约方法不进必填集，因而也不会出现在它的返回值里。

存储与智能体工具两族不在本模块：它们按继承判定，基类分别在 `app.sdk.storage` 与
`app.sdk.agent`。

能力标签不以常量形式出现在 SDK 里。族是登记出来的，SDK 抄一份固定清单会在新族登记后
失真；``service_capabilities()`` 直读宿主的服务族登记表，因此它是**运行期取值**——同一
份代码在装了不同扩展的宿主上返回不同的族集合，取值也随扩展装卸变化。标签本身是稳定
字符串，写进声明时照 ``type`` 字段的写法直接给字面量即可。
"""

from __future__ import annotations

from typing import Dict, Protocol, Tuple, runtime_checkable

from app.runtime.extensions.contract.service_instance import (
    SERVICE_INSTANCE_REQUIRED_METHODS,
)
from app.runtime.extensions.registry.service_family import service_family_registry


def service_capabilities() -> Dict[str, str]:
    """
    列出本次运行的宿主已登记的服务族

    返回值是调用时刻的快照而不是常量：内建族在登记表模块导入时即登记，因而任何时候都在，
    扩展登记的族则随该扩展装卸出现与消失。``ServiceInstanceDeclaration.capability`` 只
    接受其中的标签，不在表里的标签整条声明被拒。

    :return: 能力标签到族展示名称的映射，按能力标签升序
    """
    return {
        entry.capability: entry.name
        for entry in service_family_registry.entries()
    }


def service_instance_required_methods(capability: str) -> Tuple[str, ...]:
    """
    返回一族服务实例在宿主取用链上必须实现的方法名

    缺席这些方法的实现类会被拒绝登记。判定只看方法在不在、可不可调用，不追问实现是不是
    空桩，也不校验签名。

    :param capability: 能力标签
    :return: 必须实现的方法名元组；该族没有必填方法或未登记时为空元组
    """
    return SERVICE_INSTANCE_REQUIRED_METHODS.get(capability, ())


@runtime_checkable
class DownloaderInstance(Protocol):
    """下载器族具名实例的形状。

    宿主的十分钟重连回路直调这两个方法且没有保护，缺席即在用户看不见的背景回路里抛异常。
    ``add_torrent`` 这类下载业务方法不在协议里：宿主按模块清单的方法名分发下载请求，
    扩展声明的下载器类型不进模块清单，写进来拒的是宿主根本不会发起的调用。
    """

    def is_inactive(self) -> bool:
        """
        判断实例是否已失联、需要重连

        :return: 需要重连时为 True
        """
        ...

    def reconnect(self) -> None:
        """
        重建与下载器的连接

        :return: 无返回值
        """
        ...


@runtime_checkable
class MediaServerInstance(Protocol):
    """媒体服务器族具名实例的形状。

    与下载器族要求同样两个方法，出于同样的原因：媒体服务器的十分钟重连回路也直调它们。
    两族各写一份而不共用一个协议，是因为必填集按族判定，将来某一族增删方法时另一族不该
    跟着变。
    """

    def is_inactive(self) -> bool:
        """
        判断实例是否已失联、需要重连

        :return: 需要重连时为 True
        """
        ...

    def reconnect(self) -> None:
        """
        重建与媒体服务器的连接

        :return: 无返回值
        """
        ...


@runtime_checkable
class NotificationInstance(Protocol):
    """消息通知族具名实例的形状。

    宿主的连通性测试直调 ``get_state``。发消息的方法不在协议里：消息按模块清单的方法名
    分发，与下载器族同理。
    """

    def get_state(self) -> bool:
        """
        判断实例当前是否就绪

        :return: 可以收发消息时为 True
        """
        ...


__all__ = [
    "DownloaderInstance",
    "MediaServerInstance",
    "NotificationInstance",
    "service_capabilities",
    "service_instance_required_methods",
]
