import time
from typing import Any
from functools import lru_cache


def retry(ExceptionToCheck: Any,
          tries: int = 3, delay: int = 3, backoff: int = 2, logger: Any = None):
    """
    :param ExceptionToCheck: 需要捕获的异常
    :param tries: 重试次数
    :param delay: 延迟时间
    :param backoff: 延迟倍数
    :param logger: 日志对象
    """

    def deco_retry(f):
        def f_retry(*args, **kwargs):
            mtries, mdelay = tries, delay
            while mtries > 1:
                try:
                    return f(*args, **kwargs)
                except ExceptionToCheck as e:
                    msg = f"{str(e)}, {mdelay} 秒后重试 ..."
                    if logger:
                        logger.warn(msg)
                    else:
                        print(msg)
                    time.sleep(mdelay)
                    mtries -= 1
                    mdelay *= backoff
            return f(*args, **kwargs)

        return f_retry

    return deco_retry


def lru_cache_without_none(maxsize=None, typed=False):
    """
    不缓存None的lru_cache
    :param maxsize: 缓存大小
    :param typed: 是否区分参数类型
    """
    def decorator(func):
        cache = lru_cache(maxsize=maxsize, typed=typed)(func)

        def wrapper(*args, **kwargs):
            result = cache(*args, **kwargs)
            if result is not None:
                return result

        def cache_clear():
            cache.cache_clear()

        wrapper.cache_clear = cache_clear
        return wrapper

    return decorator
