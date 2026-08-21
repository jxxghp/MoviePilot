"""插件配置与运行态刷新依赖。"""

from app.application.commands import init_commands
from app.application.plugin.config import PluginConfigCommand
from app.application.plugin.routes import register_plugin_api
from app.application.plugin.runtime import get_plugin_manager
from app.application.scheduling import update_plugin_job
from app.runtime.events import eventmanager
from app.schemas.event import PluginDataResetEventData
from app.schemas.types import ChainEventType


def get_plugin_config_command() -> PluginConfigCommand:
    """组装插件配置更新与重置用例，隔离 API 对运行时写操作的编排。"""
    manager = get_plugin_manager()

    def publish_reset(plugin_id: str) -> None:
        """在清理持久化数据前通知目标插件执行补偿。"""
        eventmanager.send_event(
            ChainEventType.PluginDataReset,
            PluginDataResetEventData(
                plugin_id=plugin_id,
                reset_config=True,
                reset_data=True,
            ),
        )

    def refresh_registrations(plugin_id: str) -> None:
        """按服务、命令、动态路由顺序刷新插件宿主注册。"""
        update_plugin_job(plugin_id)
        init_commands(plugin_id)
        register_plugin_api(plugin_id)

    return PluginConfigCommand(
        save_config=manager.save_plugin_config,
        initialize=manager.init_plugin,
        stop=manager.stop,
        delete_config=manager.delete_plugin_config,
        delete_data=manager.delete_plugin_data,
        reload_runtime=manager.reload_plugin,
        publish_reset=publish_reset,
        refresh_registrations=refresh_registrations,
    )
