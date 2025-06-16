import sys

from app.core.cache import close_cache
from app.core.config import settings
from app.core.module import ModuleManager
from app.log import logger
from app.utils.system import SystemUtils
from app.command import CommandChain
from app.db.site_oper import SiteOper

# SitesHelper涉及资源包拉取，提前引入并容错提示
try:
    from app.helper.sites import SitesHelper
except ImportError as e:
    SitesHelper = None
    error_message = f"错误: {str(e)}\n站点认证及索引相关资源导入失败，请尝试重建容器或手动拉取资源"
    print(error_message, file=sys.stderr)
    sys.exit(1)

from app.core.event import EventManager
from app.helper.thread import ThreadHelper
from app.helper.display import DisplayHelper
from app.helper.doh import DohHelper
from app.helper.resource import ResourceHelper
from app.helper.message import MessageHelper
from app.schemas import Notification, NotificationType
from app.schemas.types import SystemConfigKey
from app.db import close_database
from app.db.systemconfig_oper import SystemConfigOper


def start_frontend():
    """
    启动前端服务
    """
    # 仅Windows可执行文件支持内嵌nginx
    if not SystemUtils.is_frozen() \
            or not SystemUtils.is_windows():
        return
    # 临时Nginx目录
    nginx_path = settings.ROOT_PATH / 'nginx'
    if not nginx_path.exists():
        return
    # 配置目录下的Nginx目录
    run_nginx_dir = settings.CONFIG_PATH.with_name('nginx')
    if not run_nginx_dir.exists():
        # 移动到配置目录
        SystemUtils.move(nginx_path, run_nginx_dir)
    # 启动Nginx
    import subprocess
    subprocess.Popen("start nginx.exe",
                     cwd=run_nginx_dir,
                     shell=True)


def stop_frontend():
    """
    停止前端服务
    """
    if not SystemUtils.is_frozen() \
            or not SystemUtils.is_windows():
        return
    import subprocess
    subprocess.Popen(f"taskkill /f /im nginx.exe", shell=True)


def clear_temp():
    """
    清理临时文件和图片缓存
    """
    # 清理临时目录中3天前的文件
    SystemUtils.clear(settings.TEMP_PATH, days=3)
    # 清理图片缓存目录中7天前的文件
    SystemUtils.clear(settings.CACHE_PATH / "images", days=7)


def user_auth():
    """
    用户认证检查
    """
    sites_helper = SitesHelper()
    if sites_helper.auth_level >= 2:
        return
    auth_conf = SystemConfigOper().get(SystemConfigKey.UserSiteAuthParams)
    status, msg = sites_helper.check_user(**auth_conf) if auth_conf else sites_helper.check_user()
    if status:
        logger.info(f"{msg} 用户认证成功")
    else:
        logger.info(f"用户认证失败，{msg}")


def check_auth():
    """
    检查认证状态
    """
    if SitesHelper().auth_level < 2:
        err_msg = "用户认证失败，站点相关功能将无法使用！"
        MessageHelper().put(f"注意：{err_msg}", title="用户认证", role="system")
        CommandChain().post_message(
            Notification(
                mtype=NotificationType.Manual,
                title="MoviePilot用户认证",
                text=err_msg,
                link=settings.MP_DOMAIN('#/site')
            )
        )


def auto_merge_duplicate_sites():
    """
    自动合并重复站点
    """
    try:
        logger.info("正在检测并自动合并重复站点...")
        site_oper = SiteOper()
        duplicates = site_oper.find_duplicate_sites()

        if duplicates:
            logger.info(f"发现 {len(duplicates)} 组重复站点，开始自动合并...")
            success, message = site_oper.merge_duplicate_sites()
            if success:
                logger.info(f"自动合并重复站点成功: {message}")
            else:
                logger.error(f"自动合并重复站点失败: {message}")
        else:
            logger.info("未发现重复站点，无需合并")
    except Exception as e:
        logger.error(f"自动合并重复站点时发生错误: {str(e)}")


def stop_modules():
    """
    服务关闭
    """
    # 停止模块
    ModuleManager().stop()
    # 停止事件消费
    EventManager().stop()
    # 停止虚拟显示
    DisplayHelper().stop()
    # 停止线程池
    ThreadHelper().shutdown()
    # 停止缓存连接
    close_cache()
    # 停止数据库连接
    close_database()
    # 停止前端服务
    stop_frontend()
    # 清理临时文件
    clear_temp()


def init_modules():
    """
    启动模块
    """
    # 虚拟显示
    DisplayHelper()
    # DoH
    DohHelper()
    # 站点管理
    SitesHelper()
    # 资源包检测
    ResourceHelper()
    # 用户认证
    user_auth()
    # 加载模块
    ModuleManager()
    # 启动事件消费
    EventManager().start()
    # 启动前端服务
    start_frontend()
    # 检查认证状态
    check_auth()
    # 自动合并重复站点
    auto_merge_duplicate_sites()
