"""
TMDB 业务异常的兼容导出与连接失败异常类型。
"""

# TMDbException 是跨层异常契约，定义在 schemas 层，供链层与API层捕获；
# 模块内部沿用原导入路径，保持类身份一致
from app.schemas.exception import TMDbException as TMDbException


class TMDbConnectionError(TMDbException):
    """
    表示请求未取得响应的 TMDB 连接异常。

    继承 TMDbException，允许调用方专门区分传输失败并决定是否重试。
    JSON 解码和业务响应失败仍由普通 TMDbException 表示。
    """
    pass
