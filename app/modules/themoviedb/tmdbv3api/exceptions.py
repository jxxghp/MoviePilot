class TMDbException(Exception):
    pass


class TMDbConnectionError(TMDbException):
    """
    TMDB连接失败异常。

    仅在确认为传输层/响应格式问题（如底层HTTP请求失败、响应无法解析为JSON）时抛出，
    与TMDB业务层明确返回的错误（如404条目不存在、参数错误等，仍抛出普通TMDbException）
    区分开，便于上层区分"网络故障，请重试"与"条目确实不存在"两类完全不同的处理与文案。

    继承自TMDbException，因此现有 `except TMDbException` 代码路径无需修改即可
    继续捕获本异常，保持向后兼容。
    """
    pass
