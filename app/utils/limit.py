import functools
import threading
import time
from collections import deque
from typing import Any, Tuple, List, Callable, Optional

from app.log import logger
from app.schemas import RateLimitExceededException, LimitException


# 抽象基类
class BaseRateLimiter:
    """
    限流器基类，定义了限流器的通用接口，用于子类实现不同的限流策略
    所有限流器都必须实现 can_call、reset 方法
    """

    def __init__(self, source: str = "", enable_logging: bool = True):
        """
        初始化 BaseRateLimiter 实例
        :param source: 业务来源或上下文信息，默认为空字符串
        :param enable_logging: 是否启用日志记录，默认为 True
        """
        self.source = source
        self.enable_logging = enable_logging
        self.lock = threading.Lock()

    @property
    def reset_on_success(self) -> bool:
        """
        是否在成功调用后自动重置限流器状态，默认为 False
        """
        return False

    def can_call(self) -> Tuple[bool, str]:
        """
        检查是否可以进行调用
        :return: 如果允许调用，返回 True 和空消息，否则返回 False 和限流消息
        """
        raise NotImplementedError

    def reset(self):
        """
        重置限流状态
        """
        raise NotImplementedError

    def trigger_limit(self):
        """
        触发限流
        """
        pass

    def record_call(self):
        """
        记录一次调用
        """
        pass

    def format_log(self, message: str) -> str:
        """
        格式化日志消息
        :param message: 日志内容
        :return: 格式化后的日志消息
        """
        return f"[{self.source}] {message}" if self.source else message

    def log(self, level: str, message: str):
        """
        根据日志级别记录日志
        :param level: 日志级别
        :param message: 日志内容
        """
        if self.enable_logging:
            log_method = getattr(logger, level, None)
            if not callable(log_method):
                log_method = logger.info
            log_method(self.format_log(message))

    def log_info(self, message: str):
        """
        记录信息日志
        """
        self.log("info", message)

    def log_warning(self, message: str):
        """
        记录警告日志
        """
        self.log("warning", message)


# 指数退避限流器
class ExponentialBackoffRateLimiter(BaseRateLimiter):
    """
    基于指数退避的限流器，用于处理单次调用频率的控制
    每次触发限流时，等待时间会成倍增加，直到达到最大等待时间
    """

    def __init__(self, base_wait: float = 60.0, max_wait: float = 600.0, backoff_factor: float = 2.0,
                 source: str = "", enable_logging: bool = True):
        """
        初始化 ExponentialBackoffRateLimiter 实例
        :param base_wait: 基础等待时间（秒），默认值为 60 秒（1 分钟）
        :param max_wait: 最大等待时间（秒），默认值为 600 秒（10 分钟）
        :param backoff_factor: 等待时间的递增倍数，默认值为 2.0，表示指数退避
        :param source: 业务来源或上下文信息，默认值为 ""
        :param enable_logging: 是否启用日志记录，默认为 True
        """
        super().__init__(source, enable_logging)
        self.next_allowed_time = 0.0
        self.current_wait = base_wait
        self.base_wait = base_wait
        self.max_wait = max_wait
        self.backoff_factor = backoff_factor
        self.source = source

    @property
    def reset_on_success(self) -> bool:
        """
        指数退避限流器在调用成功后应重置等待时间
        """
        return True

    def can_call(self) -> Tuple[bool, str]:
        """
        检查是否可以进行调用，如果当前时间超过下一次允许调用的时间，则允许调用
        :return: 如果允许调用，返回 True 和空消息，否则返回 False 和限流消息
        """
        current_time = time.time()
        with self.lock:
            if current_time >= self.next_allowed_time:
                return True, ""
            wait_time = self.next_allowed_time - current_time
            message = f"限流期间，跳过调用，将在 {wait_time:.2f} 秒后允许继续调用"
            self.log_info(message)
            return False, self.format_log(message)

    def reset(self):
        """
        重置等待时间
        当调用成功时调用此方法，重置当前等待时间为基础等待时间
        """
        with self.lock:
            if self.next_allowed_time != 0 or self.current_wait > self.base_wait:
                self.log_info(f"调用成功，重置限流等待时间为 {self.base_wait} 秒")
            self.next_allowed_time = 0.0
            self.current_wait = self.base_wait

    def trigger_limit(self):
        """
        触发限流
        当触发限流异常时调用此方法，增加下一次允许调用的时间并更新当前等待时间
        """
        current_time = time.time()
        with self.lock:
            self.next_allowed_time = current_time + self.current_wait
            self.current_wait = min(self.current_wait * self.backoff_factor, self.max_wait)
            wait_time = self.next_allowed_time - current_time
            self.log_warning(f"触发限流，将在 {wait_time:.2f} 秒后允许继续调用")


# 时间窗口限流器
class WindowRateLimiter(BaseRateLimiter):
    """
    基于时间窗口的限流器，用于限制在特定时间窗口内的调用次数
    如果超过允许的最大调用次数，则限流直到窗口期结束
    """

    def __init__(self, max_calls: int, window_seconds: float,
                 source: str = "", enable_logging: bool = True):
        """
        初始化 WindowRateLimiter 实例
        :param max_calls: 在时间窗口内允许的最大调用次数
        :param window_seconds: 时间窗口的持续时间（秒）
        :param source: 业务来源或上下文信息，默认值为 ""
        :param enable_logging: 是否启用日志记录，默认为 True
        """
        super().__init__(source, enable_logging)
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self.call_times = deque()

    def can_call(self) -> Tuple[bool, str]:
        """
        检查是否可以进行调用，如果在时间窗口内的调用次数少于最大允许次数，则允许调用。
        :return: 如果允许调用，返回 True 和空消息，否则返回 False 和限流消息
        """
        current_time = time.time()
        with self.lock:
            # 清理超出时间窗口的调用记录
            while self.call_times and current_time - self.call_times[0] > self.window_seconds:
                self.call_times.popleft()

            if len(self.call_times) < self.max_calls:
                return True, ""
            else:
                wait_time = self.window_seconds - (current_time - self.call_times[0])
                message = f"限流期间，跳过调用，将在 {wait_time:.2f} 秒后允许继续调用"
                self.log_info(message)
                return False, self.format_log(message)

    def reset(self):
        """
        重置时间窗口内的调用记录
        当调用成功时调用此方法，清空时间窗口内的调用记录
        """
        with self.lock:
            self.call_times.clear()

    def record_call(self):
        """
        记录当前时间戳，用于限流检查
        """
        current_time = time.time()
        with self.lock:
            self.call_times.append(current_time)


# 组合限流器
class CompositeRateLimiter(BaseRateLimiter):
    """
    组合限流器，可以组合多个限流策略
    当任意一个限流策略触发限流时，都会阻止调用
    """

    def __init__(self, limiters: List[BaseRateLimiter], source: str = "", enable_logging: bool = True):

        """
        初始化 CompositeRateLimiter 实例
        :param limiters: 要组合的限流器列表
        :param source: 业务来源或上下文信息，默认值为 ""
        :param enable_logging: 是否启用日志记录，默认为 True
        """
        super().__init__(source, enable_logging)
        self.limiters = limiters

    def can_call(self) -> Tuple[bool, str]:
        """
        检查是否可以进行调用，当组合的任意限流器触发限流时，阻止调用。
        :return: 如果所有限流器都允许调用，返回 True 和空消息，否则返回 False 和限流信息。
        """
        for limiter in self.limiters:
            can_call, message = limiter.can_call()
            if not can_call:
                return False, message
        return True, ""

    def reset(self):
        """
        重置所有组合的限流器状态
        """
        for limiter in self.limiters:
            limiter.reset()

    def record_call(self):
        """
        记录所有组合的限流器的调用时间
        """
        for limiter in self.limiters:
            limiter.record_call()


# 通用装饰器：自定义限流器实例
def rate_limit_handler(limiter: BaseRateLimiter, raise_on_limit: bool = False) -> Callable:
    """
    通用装饰器，允许用户传递自定义的限流器实例，用于处理限流逻辑
    该装饰器可灵活支持任意继承自 BaseRateLimiter 的限流器

    :param limiter: 限流器实例，必须继承自 BaseRateLimiter
    :param raise_on_limit: 控制在限流时是否抛出异常，默认为 False
    :return: 装饰器函数
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> Optional[Any]:
            # 检查是否传入了 "raise_exception" 参数，优先使用该参数，否则使用默认的 raise_on_limit 值
            raise_exception = kwargs.get("raise_exception", raise_on_limit)

            # 检查是否可以进行调用，调用 limiter.can_call() 方法
            can_call, message = limiter.can_call()
            if not can_call:
                # 如果调用受限，并且 raise_exception 为 True，则抛出限流异常
                if raise_exception:
                    raise RateLimitExceededException(message)
                # 如果不抛出异常，则返回 None 表示跳过调用
                return None

            # 如果调用允许，执行目标函数，并记录一次调用
            try:
                result = func(*args, **kwargs)
                limiter.record_call()
                if limiter.reset_on_success:
                    limiter.reset()
                return result
            except LimitException as e:
                # 如果目标函数触发了限流相关的异常，执行限流器的触发逻辑（如递增等待时间）
                limiter.trigger_limit()
                logger.error(limiter.format_log(f"触发限流：{str(e)}"))
                # 如果 raise_exception 为 True，则抛出异常，否则返回 None
                if raise_exception:
                    raise e
                return None

        return wrapper

    return decorator


# 装饰器：指数退避限流
def rate_limit_exponential(base_wait: float = 60.0, max_wait: float = 600.0, backoff_factor: float = 2.0,
                           raise_on_limit: bool = False, source: str = "", enable_logging: bool = True) -> Callable:
    """
    装饰器，用于应用指数退避限流策略
    通过逐渐增加调用等待时间控制调用频率。每次触发限流时，等待时间会成倍增加，直到达到最大等待时间

    :param base_wait: 基础等待时间（秒），默认值为 60 秒（1 分钟）
    :param max_wait: 最大等待时间（秒），默认值为 600 秒（10 分钟）
    :param backoff_factor: 等待时间递增的倍数，默认值为 2.0，表示指数退避
    :param raise_on_limit: 控制在限流时是否抛出异常，默认为 False
    :param source: 业务来源或上下文信息，默认为空字符串
    :param enable_logging: 是否启用日志记录，默认为 True
    :return: 装饰器函数
    """
    # 实例化 ExponentialBackoffRateLimiter，并传入相关参数
    limiter = ExponentialBackoffRateLimiter(base_wait, max_wait, backoff_factor, source, enable_logging)
    # 使用通用装饰器逻辑包装该限流器
    return rate_limit_handler(limiter, raise_on_limit)


# 装饰器：时间窗口限流
def rate_limit_window(max_calls: int, window_seconds: float,
                      raise_on_limit: bool = False, source: str = "", enable_logging: bool = True) -> Callable:
    """
    装饰器，用于应用时间窗口限流策略
    在固定的时间窗口内限制调用次数，当调用次数超过最大值时，触发限流，直到时间窗口结束

    :param max_calls: 时间窗口内允许的最大调用次数
    :param window_seconds: 时间窗口的持续时间（秒）
    :param raise_on_limit: 控制在限流时是否抛出异常，默认为 False
    :param source: 业务来源或上下文信息，默认为空字符串
    :param enable_logging: 是否启用日志记录，默认为 True
    :return: 装饰器函数
    """
    # 实例化 WindowRateLimiter，并传入相关参数
    limiter = WindowRateLimiter(max_calls, window_seconds, source, enable_logging)
    # 使用通用装饰器逻辑包装该限流器
    return rate_limit_handler(limiter, raise_on_limit)
