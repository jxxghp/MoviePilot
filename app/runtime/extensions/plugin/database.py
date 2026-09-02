"""插件运行时自有数据库端口。"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

PluginDatabaseEnsure = Callable[[str, Sequence[type], Path | None], None]
PluginDatabaseRelease = Callable[[str], None]
PluginDatabaseDestroy = Callable[[str], None]


def _ignore_ensure(
        _plugin_id: str,
        _models: Sequence[type],
        _migrations: Path | None,
) -> None:
    """组合根尚未装配时忽略插件建库。"""


def _ignore_release(_plugin_id: str) -> None:
    """组合根尚未装配时忽略插件数据库释放。"""


def _ignore_destroy(_plugin_id: str) -> None:
    """组合根尚未装配时忽略插件数据库销毁。"""


class PluginDatabase:
    """封装插件自有数据库的建立、释放与销毁能力。"""

    def __init__(
            self,
            *,
            ensure: PluginDatabaseEnsure = _ignore_ensure,
            release: PluginDatabaseRelease = _ignore_release,
            destroy: PluginDatabaseDestroy = _ignore_destroy,
    ) -> None:
        """保存由启动组合根提供的建库、释放与销毁函数。"""
        self._ensure = ensure
        self._release = release
        self._destroy = destroy

    def ensure(
            self,
            plugin_id: str,
            models: Sequence[type],
            migrations: Path | None,
    ) -> None:
        """按插件声明建立自有数据库，两项声明都为空时不建库。"""
        self._ensure(plugin_id, models, migrations)

    def release(self, plugin_id: str) -> None:
        """释放插件自有数据库的连接，保留数据。"""
        self._release(plugin_id)

    def destroy(self, plugin_id: str) -> None:
        """销毁插件自有数据库，仅限删除插件数据的路径调用。"""
        self._destroy(plugin_id)


_plugin_database = PluginDatabase()


def configure_plugin_database(database: PluginDatabase) -> None:
    """由启动组合根替换插件自有数据库实现。"""
    global _plugin_database
    _plugin_database = database


def get_plugin_database() -> PluginDatabase:
    """返回当前插件自有数据库端口。"""
    return _plugin_database
