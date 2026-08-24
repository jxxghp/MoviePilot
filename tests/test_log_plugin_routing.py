"""插件日志文件路由测试。"""

from pathlib import Path
from types import SimpleNamespace

from app.runtime.log import LoggerManager, logger


class CapturingLogWriter:
    """记录日志写入目标，避免测试访问真实文件系统。"""

    def __init__(self) -> None:
        self.entries: list[tuple[str, str, Path]] = []

    def write_log(self, level: str, message: str, file_path: Path) -> None:
        """保存单条日志的级别、内容和目标路径。"""
        self.entries.append((level, message, file_path))

    @staticmethod
    def shutdown() -> bool:
        """测试写入器没有待释放资源。"""
        return True


def test_virtual_plugin_routes_log_by_runtime_module_identity(monkeypatch, tmp_path):
    """共享物理源码的虚拟实例应按运行实例 ID 写入独立日志文件。"""
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
