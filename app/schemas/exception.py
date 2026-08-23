__all__ = (
    "ImmediateException",
    "LimitException",
    "APIRateLimitException",
    "RateLimitExceededException",
    "OperationInterrupted",
    "PluginMutationRejectedError",
    "StorageQueryError",
    "TMDbException",
)


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


class OperationInterrupted(KeyboardInterrupt):
    """
    用于表示操作被中断
    """
    pass


class PluginMutationRejectedError(RuntimeError):
    """表示插件运行时已封口，新的可变事务未获准执行。"""

    def __init__(self, operation: str) -> None:
        """保存被拒绝的操作名称并生成稳定诊断消息。"""
        self.operation = operation
        super().__init__(f"插件运行时已进入停机阶段，拒绝{operation}")


class StorageQueryError(Exception):
    """
    用于表示存储查询无法确认结果的异常类。
    当文件信息查询因网络、限流或接口错误失败（区别于「确认不存在」）时抛出，
    调用方不应把该状态当作文件不存在处理。
    """
    pass


class PersistenceUnavailableError(RuntimeError):
    """持久化执行能力暂时拒绝新操作，调用方可在稍后重试。"""


class DatabaseWorkerClosedError(PersistenceUnavailableError):
    """数据库执行器尚未启动或已经停止。"""


class DatabaseWorkerOverloadedError(PersistenceUnavailableError):
    """数据库执行器的运行与排队容量已经用尽。"""


class AgentChatPersistenceUnavailableError(PersistenceUnavailableError):
    """AgentChat 持久化服务因关闭或自身容量限制拒绝新写入。"""


class TMDbException(Exception):
    """
    用于表示TheMovieDB数据源请求失败的跨层异常契约。
    具体实现由TMDB模块内的异常子类继承本类，链层与API层只捕获本基类，
    不依赖模块内部实现路径。
    """
    pass


