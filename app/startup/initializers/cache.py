"""缓存依赖的启动入口。"""

from app.startup.composition.cache import configure_cache_composition


def configure_cache_dependencies() -> None:
    """委托组合根登记平台缓存具体实现。"""
    configure_cache_composition()
