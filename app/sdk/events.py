"""插件事件订阅与发布接口。

``EventHandlerBinding`` 是 ``EventManager.register_handler_instance_resolver`` 要求解析器
交回的绑定形状：解析器按处理器所属的类答出「这次调用用哪个实例、算在谁名下、要不要放进
线程池」，形状对不上宿主就派发不到实例上。事件类型的枚举在 `app.sdk.types`。
"""

from app.runtime.events import Event, EventHandlerBinding, EventManager, eventmanager


__all__ = ["Event", "EventHandlerBinding", "EventManager", "eventmanager"]
