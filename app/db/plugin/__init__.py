"""插件自管理数据库框架的公开入口。

runtime/extensions 层不得反向依赖 db 层（见 tests/test_architecture_dependencies.py
的包级依赖矩阵），因此建库、释放与销毁钩子由本包在首次被 import 时自注册进
``app.runtime.extensions.plugin_manager`` 预留的可注入端口，而不是由 plugin_manager
直接 import 本包。
"""

from app.db.plugin.base import plugin_declarative_base
from app.db.plugin.container import PluginDatabaseHandle
from app.db.plugin.registry import (
    declare_migrations,
    declare_models,
    destroy_database,
    ensure_database,
    get_database,
    release_all,
    release_instance,
    release_plugin,
)
from app.runtime.extensions.plugin_manager import _configure_plugin_database_lifecycle

_configure_plugin_database_lifecycle(
    ensure=ensure_database,
    release=release_plugin,
    destroy=destroy_database,
)

__all__ = [
    "PluginDatabaseHandle",
    "declare_migrations",
    "declare_models",
    "destroy_database",
    "ensure_database",
    "get_database",
    "plugin_declarative_base",
    "release_all",
    "release_instance",
    "release_plugin",
]
