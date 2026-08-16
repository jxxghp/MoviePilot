import inspect

from app.runtime.events import eventmanager, Event
from app.runtime.log import logger
from app.schemas.types import EventType


class ConfigReloadMixin:
    """配置重载混入类

    继承此 Mixin 类的类，会在配置变更时自动调用 on_config_changed 方法。
    在类中定义 CONFIG_WATCH 集合，指定需要监听的配置项
    重写 on_config_changed 方法实现具体的重载逻辑
    可选地重写 get_reload_name 方法提供模块名称（用于日志显示）
    """

    # 统一生命周期管理器可以继承此 Mixin 的重载方法，但由外部唯一负责事件绑定。
    CONFIG_RELOAD_MANAGED_EXTERNALLY: bool = False

    def __init_subclass__(cls, **kwargs):
        """为声明了 CONFIG_WATCH 的子类生成配置变更处理器。"""
        super().__init_subclass__(**kwargs)

        if getattr(cls, "CONFIG_RELOAD_MANAGED_EXTERNALLY", False):
            return

        config_watch = getattr(cls, "CONFIG_WATCH", None)
        if not config_watch:
            return

        # 检查 on_config_changed 方法是否为异步
        is_async = inspect.iscoroutinefunction(cls.on_config_changed)

        method_name = "handle_config_changed"

        # 创建事件处理函数
        def create_handler(is_async):
            """按子类重载方法类型创建同步或异步事件包装器。"""
            if is_async:

                async def wrapper(self: ConfigReloadMixin, event: Event):
                    """筛选配置键后异步执行子类重载逻辑。"""
                    if not event:
                        return
                    changed_keys = (
                        getattr(event.event_data, "key", set()) & config_watch
                    )
                    if not changed_keys:
                        return
                    logger.info(
                        f"配置 {', '.join(changed_keys)} 变更，重载 {self.get_reload_name()}..."
                    )
                    await self.on_config_changed()
            else:

                def wrapper(self: ConfigReloadMixin, event: Event):
                    """筛选配置键后同步执行子类重载逻辑。"""
                    if not event:
                        return
                    changed_keys = (
                        getattr(event.event_data, "key", set()) & config_watch
                    )
                    if not changed_keys:
                        return
                    logger.info(
                        f"配置 {', '.join(changed_keys)} 变更，重载 {self.get_reload_name()}..."
                    )
                    self.on_config_changed()

            return wrapper

        # 创建并设置处理函数
        handler = create_handler(is_async)
        handler.__module__ = cls.__module__
        # 事件总线按 __name__ 在实例上解析方法，必须与类上的方法名保持一致，
        # 否则闭包默认名 wrapper 会导致处理器被静默跳过
        handler.__name__ = method_name
        handler.__qualname__ = f"{cls.__name__}.{method_name}"
        setattr(cls, method_name, handler)
        # 添加为事件处理器
        eventmanager.add_event_listener(EventType.ConfigChanged, handler)

    def on_config_changed(self):
        """子类重写此方法实现具体重载逻辑"""
        pass

    def get_reload_name(self):
        """功能/模块名称"""
        return self.__class__.__name__
