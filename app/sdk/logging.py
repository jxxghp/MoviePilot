"""插件可使用的稳定日志入口。

``LogWriter`` 是 ``logger.configure_writer`` 接受的写入端口形状，替换写入实现时按它给对象。
"""

from app.runtime.log import LogWriter, logger


__all__ = ["LogWriter", "logger"]
