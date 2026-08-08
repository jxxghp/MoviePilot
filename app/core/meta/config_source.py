from typing import Any, Callable, Optional

from app.schemas.types import SystemConfigKey

# core/meta 不应依赖 app.db。识别相关的用户自定义配置（自定义识别词、自定义占位符、
# 自定义制作组）统一通过此处的可注入 provider 读取，由应用启动层（app/startup）在启动时
# 将其接到持久化存储（SystemConfigOper）。未注册 provider 时返回 None（无自定义、无需数据库），
# 因此元数据解析算法可脱离数据库独立复用与测试。

_provider: Optional[Callable[[SystemConfigKey], Any]] = None


def set_meta_config_provider(provider: Optional[Callable[[SystemConfigKey], Any]]) -> None:
    """
    注册 core/meta 读取用户自定义识别配置的来源（由应用层在启动时接入 SystemConfigOper）。
    传入 None 可解除注册（用于测试隔离）。
    """
    global _provider
    _provider = provider


def get_meta_config(key: SystemConfigKey) -> Any:
    """
    读取指定 key 的用户自定义识别配置；未注册 provider 时返回 None（无自定义、无需数据库）。
    """
    if _provider is None:
        return None
    return _provider(key)
