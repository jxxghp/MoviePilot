from app.adapters.cache.backends import configure_platform_cache


def configure_cache_dependencies() -> None:
    """在导入使用缓存装饰器的业务模块前注册具体缓存适配器。"""
    configure_platform_cache()
