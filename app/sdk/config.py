"""插件可读取的宿主配置对象与配置模型。"""

from app.runtime.config import (
    ConfigModel,
    GlobalVar,
    Settings,
    SystemConfModel,
    global_vars,
    settings,
)


__all__ = [
    "ConfigModel",
    "GlobalVar",
    "Settings",
    "SystemConfModel",
    "global_vars",
    "settings",
]
