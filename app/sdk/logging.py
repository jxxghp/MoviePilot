"""插件可使用的稳定日志入口。"""

from app.platform.log import (
    CustomFormatter,
    LogConfigModel,
    LogEntry,
    LogSettings,
    LoggerManager,
    NonBlockingFileHandler,
    configure_log_settings,
    configure_log_writer,
    logger,
    log_settings,
)


__all__ = ["logger"]
