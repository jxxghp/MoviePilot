class ImmediateException(Exception):
    """
    用于立即抛出异常而不重试的特殊异常类。
    当不希望使用重试机制时，可以抛出此异常。
    """
    pass
