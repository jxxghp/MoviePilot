"""
现有宿主模块能力注册表的平台扩展投影；复用清单发现，不维护第二套激活或权限状态。
"""

from functools import cache
from importlib import import_module
from typing import Any

from app.runtime.capabilities.model import CapabilitySpec
from app.runtime.capabilities.registry import CapabilityRegistry
from app.runtime.extensions.module.adapter import (
    HOST_MODULE_KIND,
    build_host_module_registry,
    channel_extension_api_version,
)
from app.schemas.types import NotificationChannel


class ModuleChannelPlatform:
    """
    在调用平台协议时才加载模块实现，枚举元数据不会初始化 SDK 或连接。

    :ivar api_version: 已通过宿主验证的渠道扩展类型契约版本
    :ivar module_id: 清单中的唯一宿主解析 owner，不接受插件自行冒充
    :ivar provider: 原有服务 selector 声明的平台标识
    :ivar channel: 原有通知枚举声明的兼容名称
    :ivar display_name: 模块展示名称
    :ivar interaction: 模块是否声明机器人入口；不代表调用者已获授权
    """

    def __init__(self, spec: CapabilitySpec) -> None:
        """
        核验宿主 kind 和类型版本，冻结解析 owner 后才允许读取平台实现。

        :param spec: 经宿主清单验证的模块能力声明
        """
        if spec.kind != HOST_MODULE_KIND:
            raise ValueError("可信渠道扩展必须属于宿主模块清单，插件不能冒充宿主 owner")
        assert spec.selector is not None
        self.api_version = channel_extension_api_version(spec)
        self._spec = spec
        self.module_id = spec.id
        self.provider = str(spec.selector.config["match_value"])
        subtype = str(spec.metadata["subtype"])
        self.channel = (NotificationChannel.__members__.get(subtype) or NotificationChannel(subtype)).value
        self.display_name = str(spec.metadata["name"])
        self.interaction = spec.metadata.get("channel_interaction") is True

    def __getattr__(self, name: str) -> Any:
        """
        将扩展契约调用交给清单指定的模块，不解释平台协议。

        :param name: 平台扩展契约的成员名
        :return: 模块声明的值或方法
        """
        entrypoint = str(self._spec.metadata["channel_extension"])
        module_name, symbol = entrypoint.split(":", 1)
        implementation = getattr(import_module(module_name), symbol)()
        if implementation.provider != self.provider or implementation.channel != self.channel:
            raise ValueError("渠道扩展与宿主模块清单的身份不一致")
        return getattr(implementation, name)


class ModuleChannelCatalog:
    """
    以同一 CapabilityRegistry 产生不可变的平台视图，不启动或重载服务。
    """

    def __init__(self, registry: CapabilityRegistry) -> None:
        """
        :param registry: 现有宿主模块注册表或隔离测试提供的等价注册表
        """
        self._entries = tuple(
            ModuleChannelPlatform(spec) for spec in registry.list_specs() if spec.metadata.get("channel_extension")
        )
        if any(entry.interaction and entry.channel in {NotificationChannel.Web.value, NotificationChannel.WebAgent.value} for entry in self._entries):
            raise ValueError("宿主网页身份不能注册为外部平台交互入口")
        providers = [entry.provider for entry in self._entries]
        if len(set(providers)) != len(providers):
            raise ValueError("同一渠道类型存在重复的平台扩展")

    def providers(self) -> tuple[ModuleChannelPlatform, ...]:
        """
        返回清单声明的平台快照，调用者不能更改目录内容。
        """
        return self._entries

    def get(self, provider: str) -> ModuleChannelPlatform | None:
        """
        按模块服务 selector 查找协议扩展。

        :param provider: 连接类型
        :return: 已声明的平台扩展，未知类型为空
        """
        return next((entry for entry in self._entries if entry.provider == provider), None)


@cache
def build_channel_platform_catalog() -> ModuleChannelCatalog:
    """
    从现有宿主模块发现机制构建平台视图，交由运行配置组合根持有。
    """
    return ModuleChannelCatalog(build_host_module_registry())
