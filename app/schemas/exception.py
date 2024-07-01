class ImmediateException(Exception):
    """
    用于立即抛出异常而不重试的特殊异常类。
    当不希望使用重试机制时，可以抛出此异常。
    """
    pass


class APIRateLimitException(ImmediateException):
    """
    用于表示API速率限制的异常类。
    当API调用触发速率限制时，可以抛出此异常以立即终止操作并报告错误。
    """
    pass
