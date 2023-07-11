import traceback
from multiprocessing.dummy import Pool as ThreadPool
from multiprocessing.pool import ThreadPool
from threading import Event
from typing import Any, List, Dict
from urllib.parse import urljoin

from apscheduler.schedulers.background import BackgroundScheduler
from ruamel.yaml import CommentedMap

from app import schemas
from app.core.event import EventManager, eventmanager
from app.core.config import settings
from app.helper.browser import PlaywrightHelper
from app.helper.cloudflare import under_challenge
from app.helper.module import ModuleHelper
from app.helper.sites import SitesHelper
from app.log import logger
from app.plugins import _PluginBase
from app.utils.http import RequestUtils
from app.utils.site import SiteUtils
from app.utils.string import StringUtils
from app.utils.timer import TimerUtils
from app.schemas.types import EventType


class AutoSignIn(_PluginBase):

    # 插件名称
    plugin_name = "站点自动签到"
    # 插件描述
    plugin_desc = "站点每日自动模拟登录或签到。"
    # 插件图标
    plugin_icon = ""
    # 主题色
    plugin_color = ""
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "autosignin_"
    # 加载顺序
    plugin_order = 0
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    sites: SitesHelper = None
    # 事件管理器
    event: EventManager = None
    # 定时器
    _scheduler = None
    # 加载的模块
    _site_schema: list = []

    def init_plugin(self, config: dict = None):
        self.sites = SitesHelper()
        self.event = EventManager()

        # 停止现有任务
        self.stop_service()

        # 加载模块
        self._site_schema = ModuleHelper.load('app.plugins.autosignin.sites',
                                              filter_func=lambda _, obj: hasattr(obj, 'match'))

        # 定时服务
        self._scheduler = BackgroundScheduler(timezone=settings.TZ)
        triggers = TimerUtils.random_scheduler(num_executions=2,
                                               begin_hour=9,
                                               end_hour=23,
                                               max_interval=12 * 60,
                                               min_interval=6 * 60)
        for trigger in triggers:
            self._scheduler.add_job(self.sign_in, "cron", hour=trigger.hour, minute=trigger.minute)

        # 启动任务
        if self._scheduler.get_jobs():
            self._scheduler.print_jobs()
            self._scheduler.start()

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """
        定义远程控制命令
        :return: 命令关键字、事件、描述、附带数据
        """
        return [{
            "cmd": "/site_signin",
            "event": EventType.SiteSignin,
            "desc": "站点签到",
            "data": {}
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        """
        获取插件API
        [{
            "path": "/xx",
            "endpoint": self.xxx,
            "methods": ["GET", "POST"],
            "summary": "API说明"
        }]
        """
        return [{
            "path": "/signin_by_domain",
            "endpoint": self.signin_by_domain,
            "methods": ["GET"],
            "summary": "站点签到",
            "description": "使用站点域名签到站点",
        }]

    @eventmanager.register(EventType.SiteSignin)
    def sign_in(self, event: Event = None):
        """
        自动签到
        """
        if event:
            logger.info("收到远程签到命令，开始执行签到任务 ...")
        # 查询签到站点
        sign_sites = [site for site in self.sites.get_indexers() if not site.get("public")]
        if not sign_sites:
            logger.info("没有需要签到的站点")
            return

        # 执行签到
        logger.info("开始执行签到任务 ...")
        with ThreadPool(min(len(sign_sites), 5)) as p:
            status = p.map(self.signin_site, sign_sites)

        if status:
            logger.info("站点签到任务完成！")
            # 发送通知
            self.chain.post_message(title="站点自动签到", text="\n".join([s for s in status if s]))
        else:
            logger.error("站点签到任务失败！")

    def __build_class(self, url) -> Any:
        for site_schema in self._site_schema:
            try:
                if site_schema.match(url):
                    return site_schema
            except Exception as e:
                logger.error("站点模块加载失败：%s" % str(e))
        return None

    def signin_by_domain(self, url: str) -> schemas.Response:
        """
        签到一个站点，可由API调用
        """
        domain = StringUtils.get_url_domain(url)
        site_info = self.sites.get_indexer(domain)
        if not site_info:
            return schemas.Response(
                success=True,
                message=f"站点【{url}】不存在"
            )
        else:
            return schemas.Response(
                success=True,
                message=self.signin_site(site_info)
            )

    def signin_site(self, site_info: CommentedMap) -> str:
        """
        签到一个站点
        """
        site_module = self.__build_class(site_info.get("url"))
        if site_module and hasattr(site_module, "signin"):
            try:
                status, msg = site_module().signin(site_info)
                # 特殊站点直接返回签到信息，防止仿真签到、模拟登陆有歧义
                return msg or ""
            except Exception as e:
                traceback.print_exc()
                return f"【{site_info.get('name')}】签到失败：{str(e)}"
        else:
            return self.__signin_base(site_info)

    @staticmethod
    def __signin_base(site_info: CommentedMap) -> str:
        """
        通用签到处理
        :param site_info: 站点信息
        :return: 签到结果信息
        """
        if not site_info:
            return ""
        site = site_info.get("name")
        site_url = site_info.get("url")
        site_cookie = site_info.get("cookie")
        ua = site_info.get("ua")
        render = site_info.get("render")
        proxies = settings.PROXY if site_info.get("proxy") else None
        proxy_server = settings.PROXY_SERVER if site_info.get("proxy") else None
        if not site_url or not site_cookie:
            logger.warn(f"未配置 {site} 的站点地址或Cookie，无法签到")
            return ""
        # 模拟登录
        try:
            # 访问链接
            checkin_url = site_url
            if site_url.find("attendance.php") == -1:
                # 拼登签到地址
                checkin_url = urljoin(site_url, "attendance.php")
            logger.info(f"开始站点签到：{site}，地址：{checkin_url}...")
            if render:
                page_source = PlaywrightHelper().get_page_source(url=checkin_url,
                                                                 cookies=site_cookie,
                                                                 ua=ua,
                                                                 proxies=proxy_server)
                if not SiteUtils.is_logged_in(page_source):
                    if under_challenge(page_source):
                        return f"【{site}】无法通过Cloudflare！"
                    return f"【{site}】仿真登录失败，Cookie已失效！"
            else:
                res = RequestUtils(cookies=site_cookie,
                                   ua=ua,
                                   proxies=proxies
                                   ).get_res(url=checkin_url)
                if not res and site_url != checkin_url:
                    logger.info(f"开始站点模拟登录：{site}，地址：{site_url}...")
                    res = RequestUtils(cookies=site_cookie,
                                       ua=ua,
                                       proxies=proxies
                                       ).get_res(url=site_url)
                # 判断登录状态
                if res and res.status_code in [200, 500, 403]:
                    if not SiteUtils.is_logged_in(res.text):
                        if under_challenge(res.text):
                            msg = "站点被Cloudflare防护，请打开站点浏览器仿真"
                        elif res.status_code == 200:
                            msg = "Cookie已失效"
                        else:
                            msg = f"状态码：{res.status_code}"
                        logger.warn(f"{site} 签到失败，{msg}")
                        return f"【{site}】签到失败，{msg}！"
                    else:
                        logger.info(f"{site} 签到成功")
                        return f"【{site}】签到成功"
                elif res is not None:
                    logger.warn(f"{site} 签到失败，状态码：{res.status_code}")
                    return f"【{site}】签到失败，状态码：{res.status_code}！"
                else:
                    logger.warn(f"{site} 签到失败，无法打开网站")
                    return f"【{site}】签到失败，无法打开网站！"
        except Exception as e:
            logger.warn("%s 签到失败：%s" % (site, str(e)))
            traceback.print_exc()
            return f"【{site}】签到失败：{str(e)}！"

    def stop_service(self):
        """
        退出插件
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error("退出插件失败：%s" % str(e))
