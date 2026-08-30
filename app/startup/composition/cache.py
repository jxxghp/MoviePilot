"""缓存 Adapter 的宿主组合根。"""

from app.adapters.cache.backends import configure_platform_cache


def configure_cache_composition() -> None:
    """在业务模块导入前登记平台缓存具体实现。"""
    configure_platform_cache()
