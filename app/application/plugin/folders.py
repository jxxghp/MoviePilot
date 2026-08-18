"""插件文件夹应用用例。"""

from app.application.configuration import get_configured_system_config
from app.runtime.log import logger
from app.schemas.types import SystemConfigKey


def remove_plugin_from_folders(plugin_id: str) -> None:
    """
    从所有配置文件夹中移除指定插件。

    同时兼容当前的字典格式和迁移前的插件列表格式，避免卸载旧版本插件时留下
    不可见的文件夹引用。
    :param plugin_id: 要移除的插件 ID
    """
    try:
        config_oper = get_configured_system_config()
        folders = config_oper.get(SystemConfigKey.PluginFolders) or {}
        modified = False

        for folder_name, folder_data in folders.items():
            if isinstance(folder_data, dict) and "plugins" in folder_data:
                if plugin_id in folder_data["plugins"]:
                    folder_data["plugins"].remove(plugin_id)
                    logger.info(f"已从文件夹 '{folder_name}' 中移除插件 {plugin_id}")
                    modified = True
            elif isinstance(folder_data, list) and plugin_id in folder_data:
                folder_data.remove(plugin_id)
                logger.info(f"已从文件夹 '{folder_name}' 中移除插件 {plugin_id}")
                modified = True

        if modified:
            config_oper.set(SystemConfigKey.PluginFolders, folders)
        else:
            logger.debug(f"插件 {plugin_id} 不在任何文件夹中，无需移除")
    except Exception as error:
        # 文件夹配置损坏不应阻断插件代码、数据和定时任务的卸载流程。
        logger.error(f"从文件夹中移除插件时出错：{error}")
