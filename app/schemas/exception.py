class ImmediateException(Exception):
    """
    用于立即抛出异常而不重试的特殊异常类。
    当不希望使用重试机制时，可以抛出此异常。
    """
    pass


class LimitException(ImmediateException):
    """
    用于表示本地限流器或外部触发的限流异常的基类。
    该异常类可用于本地限流逻辑或外部限流处理。
    """
    pass


class APIRateLimitException(LimitException):
    """
    用于表示API速率限制的异常类。
    当API调用触发速率限制时，可以抛出此异常以立即终止操作并报告错误。
    """
    pass


class RateLimitExceededException(LimitException):
    """
    用于表示本地限流器触发的异常类。
    当函数调用频率超过限流器的限制时，可以抛出此异常以停止当前操作并告知调用者限流情况。
    这个异常通常用于本地限流逻辑（例如 RateLimiter），当系统检测到函数调用频率过高时，触发限流并抛出该异常。
    """
    pass
