"""废弃阶段的运行期行为。

调用点只需回答两个问题：这条废弃路径现在还该不该生效（``is_active``），以及走到这里
时要不要留痕或拦截（``enforce``）。阶段的推进只改 ``notices.NOTICES`` 里的 ``stage``，
调用点不动。
"""
import functools
import inspect
import threading
from typing import Any, Callable, FrozenSet, Optional, Set, Tuple

from app.runtime.deprecation import notices
from app.runtime.deprecation.notices import DeprecationNotice, DeprecationStage
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting

# 已告警过的 (标识, 触发来源)，保证每个来源只留一次痕迹
_warned: Set[Tuple[str, Optional[str]]] = set()
_warned_lock = threading.Lock()


class DeprecatedFeatureError(RuntimeError):
    """触达了已停用或已移除的废弃能力。"""


def find_notice(key: str) -> Optional[DeprecationNotice]:
    """
    查找废弃登记

    :param key: 废弃标识
    :return: 对应登记，未登记时为 None
    """
    return notices.NOTICES.get(key)


def get_notice(key: str) -> DeprecationNotice:
    """
    取出废弃登记

    :param key: 废弃标识
    :return: 对应登记
    :raises KeyError: 标识未登记
    """
    notice = find_notice(key)
    if notice is None:
        raise KeyError(f"未登记的废弃标识：{key}")
    return notice


def all_notices() -> Tuple[DeprecationNotice, ...]:
    """
    列出全部废弃登记

    :return: 按标识升序排列的登记
    """
    return tuple(notices.NOTICES[key] for key in sorted(notices.NOTICES))


def _enabled_keys() -> FrozenSet[str]:
    """
    读取被显式恢复的废弃标识集合

    :return: 标识集合
    """
    configured = get_runtime_setting('DEPRECATION_ENABLED') or ""
    return frozenset(item.strip() for item in str(configured).split(",") if item.strip())


def _notice_active(notice: DeprecationNotice) -> bool:
    """
    判断一条登记当前是否仍应生效

    :param notice: 废弃登记
    :return: 生效为 True
    """
    if notice.stage <= DeprecationStage.WARN:
        return True
    if notice.stage is DeprecationStage.DISABLED:
        return notice.key in _enabled_keys()
    return False


def is_active(key: str) -> bool:
    """
    判断废弃路径当前是否仍应生效

    登记与预警阶段照常生效；停用阶段默认不生效，仅当标识出现在 DEPRECATION_ENABLED 中
    才恢复；移除阶段无论如何都不生效。

    :param key: 废弃标识
    :return: 生效为 True
    :raises KeyError: 标识未登记
    """
    return _notice_active(get_notice(key))


def warn(key: str, *, context: Optional[str] = None) -> None:
    """
    就废弃路径留下一次告警

    仅登记阶段不打扰用户；其余阶段下同一 (标识, 触发来源) 在单个进程内只告警一次，
    避免热路径刷屏。

    :param key: 废弃标识
    :param context: 触发来源，例如具体方法名或插件标识
    :raises KeyError: 标识未登记
    """
    notice = get_notice(key)
    if notice.stage is DeprecationStage.SILENT:
        return
    dedup_key = (key, context)
    with _warned_lock:
        if dedup_key in _warned:
            return
        _warned.add(dedup_key)
    logger.warning(notice.message(context))


def guard(key: str, *, context: Optional[str] = None) -> None:
    """
    拦截已停用或已移除的废弃能力

    :param key: 废弃标识
    :param context: 触发来源
    :raises KeyError: 标识未登记
    :raises DeprecatedFeatureError: 该能力当前不应再生效
    """
    notice = get_notice(key)
    if not _notice_active(notice):
        raise DeprecatedFeatureError(notice.message(context))


def enforce(key: str, *, context: Optional[str] = None) -> None:
    """
    对一次废弃路径的触达执行当前阶段的处置

    未登记的标识视为尚未纳入废弃流程，直接放行；已登记的先拦截再留痕。

    :param key: 废弃标识
    :param context: 触发来源
    :raises DeprecatedFeatureError: 该能力当前不应再生效
    """
    if find_notice(key) is None:
        return
    guard(key, context=context)
    warn(key, context=context)


def enforce_facade(facade: str, operation: str) -> None:
    """
    对一次旧 Facade 命中执行当前阶段的处置

    先按 ``facade.operation`` 精确匹配，再退回整个 Facade 的登记，两者都没有登记则放行，
    因此未纳入废弃流程的 Facade 不受任何影响。

    :param facade: Facade 标识，与 compat.facade.hit 指标的 facade 标签一致
    :param operation: 被调用的方法名
    :raises DeprecatedFeatureError: 该 Facade 或方法当前不应再生效
    """
    registry = notices.NOTICES
    if not registry:
        return
    context = f"{facade}.{operation}"
    for key in (context, facade):
        if key in registry:
            enforce(key, context=context)
            return


def deprecated(key: str) -> Callable:
    """
    把废弃语义施加到一个可调用对象上

    停用与移除阶段直接抛错，其余阶段留痕后照常执行。

    :param key: 废弃标识
    :return: 装饰器
    """

    def decorator(func: Callable) -> Callable:
        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                context = getattr(func, "__qualname__", None)
                guard(key, context=context)
                warn(key, context=context)
                return await func(*args, **kwargs)

            return async_wrapper

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            context = getattr(func, "__qualname__", None)
            guard(key, context=context)
            warn(key, context=context)
            return func(*args, **kwargs)

        return wrapper

    return decorator


def reset_warned() -> None:
    """清空告警去重记录。"""
    with _warned_lock:
        _warned.clear()
