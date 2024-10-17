import asyncio

from app.core.plugin import PluginManager
from app.log import logger
from app.scheduler import Scheduler


async def init_plugins_async():
    """
    初始化安装插件，并动态注册后台任务及API
    """
    try:
        loop = asyncio.get_event_loop()
        plugin_manager = PluginManager()
        scheduler = Scheduler()
        sync_plugins = await loop.run_in_executor(None, plugin_manager.sync)
        if not sync_plugins:
            return
        # 为避免初始化插件异常，这里所有插件都进行初始化
        logger.info(f"已同步安装 {len(sync_plugins)} 个在线插件，正在初始化所有插件")
        # 安装完成后重新初始化插件
        plugin_manager.init_config()
        # 插件启动后注册后台任务
        scheduler.init_plugin_jobs()
        # 插件启动后注册插件API
        register_plugin_api()
        logger.info("所有插件初始化完成")
    except Exception as e:
        logger.error(f"插件初始化过程中出现异常: {e}")


def register_plugin_api():
    """
    插件启动后注册插件API
    """
    from app.api.endpoints import plugin
    plugin.register_plugin_api()
