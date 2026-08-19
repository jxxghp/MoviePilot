"""app.db.plugin 包导入即自注册进 plugin_manager 的建库/释放/销毁端口。

plugin_manager.py 所在的 runtime/extensions 包不得反向依赖 db 层，因此接线方向是
app.db.plugin 主动注册，而不是 plugin_manager 主动 import。本测试钉住这条接线，
防止今后重构悄悄改回反向依赖或漏掉某一个钩子。
"""

import app.db.plugin as plugin_database_framework
from app.runtime.extensions import plugin_manager as plugin_manager_module


def test_importing_plugin_database_framework_wires_all_three_ports():
    """导入 app.db.plugin 后，三个端口都应指向注册表的真实实现。"""
    assert (
        plugin_manager_module._plugin_database_ensure
        is plugin_database_framework.ensure_database
    )
    assert (
        plugin_manager_module._plugin_database_release
        is plugin_database_framework.release_plugin
    )
    assert (
        plugin_manager_module._plugin_database_destroy
        is plugin_database_framework.destroy_database
    )
