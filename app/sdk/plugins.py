"""插件和模块运行时管理接口，以及两个管理器交回的结果类型。

``CapabilitySpec`` 及其字段类型 ``ActivationPolicy``、``SelectorSpec`` 是
``ModuleManager.list_specs()`` 的返回内容；``PluginDependencyClassification`` 与
``PluginDependencyInstallResult`` 分别是 ``PluginManager`` 划分已装插件与安装缺失依赖的
返回内容。读取这些返回值不需要额外出口，标注它们则需要，因此一并在此给出。
"""

from app.runtime.capabilities.model import (
    ActivationPolicy,
    CapabilitySpec,
    SelectorSpec,
)
from app.runtime.extensions.contract.dependency import (
    PluginDependencyClassification,
    PluginDependencyInstallResult,
)
from app.runtime.extensions.module_manager import ModuleManager
from app.runtime.extensions.plugin_manager import PluginManager


__all__ = [
    "ActivationPolicy",
    "CapabilitySpec",
    "ModuleManager",
    "PluginDependencyClassification",
    "PluginDependencyInstallResult",
    "PluginManager",
    "SelectorSpec",
]
