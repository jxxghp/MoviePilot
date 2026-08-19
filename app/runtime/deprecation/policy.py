"""废弃阶段的运行期行为。

调用点只需回答两个问题：这条废弃路径现在还该不该生效（``is_active``），以及走到这里
时要不要留痕（``warn``）。阶段的推进只改 ``notices.NOTICES`` 里的 ``stage``，调用点不动。
"""
import functools
import threading
from typing import Any, Callable, Optional, Set, Tuple

from app.runtime.deprecation.notices import NOTICES, DeprecationNotice, DeprecationStage
from app.runtime.log import logger

# 已告警过的 (标识, 触发来源)，保证每个来源只留一次痕迹
_warned: Set[Tuple[str, Optional[str]]] = set()
_warned_lock = threading.Lock()


class DeprecatedFeatureError(RuntimeError):
    """触达已彻底移除的废弃能力。"""


def get_notice(key: str) -> DeprecationNotice:
    """
    取出废弃登记

    :param key: 废弃标识
    :return: 对应登记
    :raises KeyError: 标识未登记
    """
    try:
        return NOTICES[key]
    except KeyError:
        raise KeyError(f"未登记的废弃标识：{key}") from None


def all_notices() -> Tuple[DeprecationNotice, ...]:
    """
    列出全部废弃登记

    :return: 按标识升序排列的登记
    """
    return tuple(NOTICES[key] for key in sorted(NOTICES))


def _enabled_keys() -> frozenset:
    """读取被显式恢复的废弃标识集合。"""
    from app.runtime.config import settings

    configured = getattr(settings, "DEPRECATION_ENABLED", None) or ()
    return frozenset(str(item).strip() for item in configured if str(item).strip())


def is_active(key: str) -> bool:
    """
    判断废弃路径当前是否仍应生效

    阶段一照常生效；阶段二默认停用，仅当标识出现在 DEPRECATION_ENABLED 中才恢复；
    阶段三无论如何都不生效。

    :param key: 废弃标识
    :return: 生效为 True
    """
    notice = get_notice(key)
    if notice.stage is DeprecationStage.WARN:
        return True
    if notice.stage is DeprecationStage.DISABLED:
        return key in _enabled_keys()
    return False


def warn(key: str, *, context: Optional[str] = None) -> None:
    """
    就废弃路径留下一次告警

    同一 (标识, 触发来源) 在单个进程内只告警一次，避免热路径刷屏。

    :param key: 废弃标识
    :param context: 触发来源，例如插件标识
    """
    notice = get_notice(key)
    dedup_key = (key, context)
    with _warned_lock:
        if dedup_key in _warned:
            return
        _warned.add(dedup_key)
    logger.warning(notice.message(context))


def guard(key: str, *, context: Optional[str] = None) -> None:
    """
    拦截已彻底移除的废弃能力

    :param key: 废弃标识
    :param context: 触发来源
    :raises DeprecatedFeatureError: 该能力已处于彻底删除阶段
    """
    notice = get_notice(key)
    if notice.stage is DeprecationStage.REMOVED:
        raise DeprecatedFeatureError(notice.message(context))


def deprecated(key: str) -> Callable:
    """
    把废弃语义施加到一个可调用对象上

    阶段三直接抛错，阶段二告警后返回 None，阶段一告警后照常执行。

    :param key: 废弃标识
    :return: 装饰器
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            context = getattr(func, "__qualname__", None)
            guard(key, context=context)
            warn(key, context=context)
            if not is_active(key):
                return None
            return func(*args, **kwargs)

        return wrapper

    return decorator


def reset_warned() -> None:
    """清空告警去重记录。"""
    with _warned_lock:
        _warned.clear()
