import asyncio
import shutil

from app.core.config import settings
from app.core.plugin import PluginManager
from app.log import logger
from app.utils.system import SystemUtils


async def sync_plugins() -> bool:
    """
    初始化安装插件，并动态注册后台任务及API
    """
    try:
        loop = asyncio.get_event_loop()
        plugin_manager = PluginManager()

        sync_result = await execute_task(loop, plugin_manager.sync, "插件同步到本地")
        resolved_dependencies = await execute_task(loop, plugin_manager.install_plugin_missing_dependencies,
                                                   "缺失依赖项安装")
        # 判断是否需要进行插件初始化
        if not sync_result and not resolved_dependencies:
            logger.debug("没有新的插件同步到本地或缺失依赖项需要安装")
            return False

        # 继续执行后续的插件初始化步骤
        logger.info("正在重新初始化插件")
        # 重新初始化插件
        plugin_manager.init_config()
        # 重新注册插件API
        register_plugin_api()
        logger.info("所有插件初始化完成")
        return True
    except Exception as e:
        logger.error(f"插件初始化过程中出现异常: {e}")
        return False


async def execute_task(loop, task_func, task_name):
    """
    执行后台任务
    """
    try:
        result = await loop.run_in_executor(None, task_func)
        if isinstance(result, list) and result:
            logger.debug(f"{task_name} 已完成，共处理 {len(result)} 个项目")
        else:
            logger.debug(f"没有新的 {task_name} 需要处理")
        return result
    except Exception as e:
        logger.error(f"{task_name} 时发生错误：{e}", exc_info=True)
        return []


def register_plugin_api():
    """
    插件启动后注册插件API
    """
    from app.api.endpoints import plugin
    plugin.register_plugin_api()


def init_plugins():
    """
    初始化插件
    """
    PluginManager().start()
    register_plugin_api()


def stop_plugins():
    """
    停止插件
    """
    try:
        plugin_manager = PluginManager()
        plugin_manager.stop()
        plugin_manager.stop_monitor()
    except Exception as e:
        logger.error(f"停止插件时发生错误：{e}", exc_info=True)


def backup_plugins():
    """
    备份插件到用户配置目录（仅docker环境）
    """

    # 非docker环境不处理
    if not SystemUtils.is_docker():
        return

    try:
        # 使用绝对路径确保准确性
        plugins_dir = settings.ROOT_PATH / "app" / "plugins"
        backup_dir = settings.CONFIG_PATH / "plugins_backup"
        
        if not plugins_dir.exists():
            logger.info("插件目录不存在，跳过备份")
            return
            
        # 确保备份目录存在
        backup_dir.mkdir(parents=True, exist_ok=True)
        
        # 需要排除的文件和目录
        exclude_items = {"__init__.py", "__pycache__", ".DS_Store"}
        
        # 遍历插件目录，备份除排除项外的所有内容
        for item in plugins_dir.iterdir():
            if item.name in exclude_items:
                continue
                
            target_path = backup_dir / item.name
            
            # 如果是目录
            if item.is_dir():
                if target_path.exists():
                    shutil.rmtree(target_path)
                shutil.copytree(item, target_path)
                logger.info(f"已备份插件目录: {item.name}")
            # 如果是文件
            elif item.is_file():
                shutil.copy2(item, target_path)
                logger.info(f"已备份插件文件: {item.name}")
                
        logger.info(f"插件备份完成，备份位置: {backup_dir}")
        
    except Exception as e:
        logger.error(f"插件备份失败: {str(e)}")


def restore_plugins():
    """
    从备份恢复插件到app/plugins目录，恢复完成后删除备份（仅docker环境）
    """

    # 非docker环境不处理
    if not SystemUtils.is_docker():
        return

    try:
        # 使用绝对路径确保准确性
        plugins_dir = settings.ROOT_PATH / "app" / "plugins"
        backup_dir = settings.CONFIG_PATH / "plugins_backup"
        
        if not backup_dir.exists():
            logger.info("插件备份目录不存在，跳过恢复")
            return

        # 系统被重置才恢复插件
        if SystemUtils.is_system_reset():
            
            # 确保插件目录存在
            plugins_dir.mkdir(parents=True, exist_ok=True)

            # 遍历备份目录，恢复所有内容
            restored_count = 0
            for item in backup_dir.iterdir():
                target_path = plugins_dir / item.name

                # 如果是目录，且目录内有内容
                if item.is_dir() and any(item.iterdir()):
                    if target_path.exists():
                        shutil.rmtree(target_path)
                    shutil.copytree(item, target_path)
                    logger.info(f"已恢复插件目录: {item.name}")
                    restored_count += 1
                # 如果是文件
                elif item.is_file():
                    shutil.copy2(item, target_path)
                    logger.info(f"已恢复插件文件: {item.name}")
                    restored_count += 1

            logger.info(f"插件恢复完成，共恢复 {restored_count} 个项目")
        
        # 删除备份目录
        try:
            shutil.rmtree(backup_dir)
            logger.info(f"已删除插件备份目录: {backup_dir}")
        except Exception as e:
            logger.warning(f"删除备份目录失败: {str(e)}")
        
    except Exception as e:
        logger.error(f"插件恢复失败: {str(e)}")
