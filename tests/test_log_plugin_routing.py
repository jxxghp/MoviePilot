"""
插件日志文件路由测试。
"""

from pathlib import Path
from types import SimpleNamespace

from app.runtime.log import LoggerManager, logger


class CapturingLogWriter:
    """
    记录日志写入目标，避免测试访问真实文件系统。
    """

    def __init__(self) -> None:
        self.entries: list[tuple[str, str, Path]] = []

    def write_log(
        self,
        level: str,
        message: str,
        file_path: Path,
        exc_info: tuple | None = None,
    ) -> None:
        """
        保存单条日志的级别、内容和目标路径。

        :param level: 日志等级名称
        :param message: 已经格式化的日志正文
        :param file_path: 目标日志文件路径
        :param exc_info: 已规范化的异常三元组，本测试写入器忽略该信息
        """
        self.entries.append((level, message, file_path))

    @staticmethod
    def shutdown() -> bool:
        """
        测试写入器没有待释放资源。
        """
        return True


def test_virtual_plugin_routes_log_by_runtime_module_identity(monkeypatch, tmp_path):
    """
    共享物理源码的虚拟实例应按运行实例 ID 写入独立日志文件。
    """
    writer = CapturingLogWriter()
    monkeypatch.setattr(LoggerManager, "_writer", writer)
    monkeypatch.setattr(LoggerManager, "_log_path", tmp_path)
    monkeypatch.setattr(
        LoggerManager,
        "_get_console_logger",
        classmethod(
            lambda _cls, _logfile: SimpleNamespace(info=lambda *_args, **_kwargs: None)
        ),
    )
    namespace = {
        "__name__": "app.plugins.mediawarp1",
        "logger": logger,
    }
    source = compile(
        "def emit_log():\n    logger.info('virtual instance started')\n",
        "/config/app/plugins/mediawarp/__init__.py",
        "exec",
    )
    exec(source, namespace)

    namespace["emit_log"]()

    assert writer.entries == [
        (
            "INFO",
            "mediawarp - virtual instance started",
            tmp_path / "plugins" / "mediawarp1.log",
        )
    ]
