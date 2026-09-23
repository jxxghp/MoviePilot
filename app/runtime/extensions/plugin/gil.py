"""free-threaded 运行时下插件触发 GIL 回退的归因。"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager

from app.foundation.environment import is_free_threaded_runtime, is_gil_enabled

# 按插件 ID 记录"该插件的加载使进程回退到 GIL"的端口，由注册表实现
GilFallbackRecorder = Callable[[str], None]


def ignore_gil_fallback(_plugin_id: str) -> None:
    """默认归因端口：未装配注册表的加载器／生命周期不记录归因。"""


@contextmanager
def attribute_gil_fallback(plugin_id: str, recorder: GilFallbackRecorder) -> Iterator[None]:
    """
    观察包裹区间内 GIL 是否从关闭变为开启，发生时把回退归因到该插件。

    GIL 一旦被原生扩展开启就会保持到进程结束，因此只有第一个触发转换的插件能被观察到；
    之后加载的插件即使同样缺少 free-threaded 声明，也无法再被区分。
    插件在加载之后才惰性导入的原生扩展不在任何包裹区间内，同样无法归因。
    区间内抛出的异常照常向外传播，归因在 finally 中完成，加载失败的插件同样会被记录。
    """
    gil_enabled_before = is_gil_enabled()
    try:
        yield
    finally:
        if is_free_threaded_runtime() and not gil_enabled_before and is_gil_enabled():
            recorder(plugin_id)
