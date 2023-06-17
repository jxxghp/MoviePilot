from datetime import datetime
from multiprocessing.dummy import Pool as ThreadPool
from threading import Lock
from typing import Optional, Any, List

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from ruamel.yaml import CommentedMap

from app.core.config import settings
from app.core.event import eventmanager
from app.core.event import Event
from app.helper.browser import PlaywrightHelper
from app.helper.module import ModuleHelper
from app.helper.sites import SitesHelper
from app.log import logger
from app.plugins import _PluginBase
from app.plugins.sitestatistic.siteuserinfo import ISiteUserInfo
from app.utils.http import RequestUtils
from app.utils.string import StringUtils
from app.utils.timer import TimerUtils

import warnings

from app.schemas.types import EventType

warnings.filterwarnings("ignore", category=FutureWarning)

lock = Lock()


class SiteStatistic(_PluginBase):
    sites = None
    _scheduler: BackgroundScheduler = None
    _MAX_CONCURRENCY: int = 10
    _last_update_time: Optional[datetime] = None
    _sites_data: dict = {}
    _site_schema: List[ISiteUserInfo] = None

    def init_plugin(self, config: dict = None):
        # 加载模块
        self._site_schema = ModuleHelper.load('app.plugins.sitestatistic.siteuserinfo',
                                              filter_func=lambda _, obj: hasattr(obj, 'schema'))
        self._site_schema.sort(key=lambda x: x.order)
        # 站点管理
        self.sites = SitesHelper()
        # 站点上一次更新时间
        self._last_update_time = None
        # 站点数据
        self._sites_data = {}
        # 定时服务
        self._scheduler = BackgroundScheduler(timezone=settings.TZ)
        triggers = TimerUtils.random_scheduler(num_executions=1,
                                               begin_hour=0,
                                               end_hour=1,
                                               min_interval=1,
                                               max_interval=60)
        for trigger in triggers:
            self._scheduler.add_job(self.refresh_all_site_data, "cron", hour=trigger.hour, minute=trigger.minute)

        # 启动任务
        if self._scheduler.get_jobs():
            self._scheduler.print_jobs()
            self._scheduler.start()

    @staticmethod
    def get_command() -> dict:
        """
        定义远程控制命令
        :return: 命令关键字、事件、描述、附带数据
        """
        return {
            "cmd": "/site_statistic",
            "event": EventType.SiteStatistic,
            "desc": "站点数据统计",
            "data": {}
        }

    def stop_service(self):
        pass

    def __build_class(self, html_text: str) -> Any:
        for site_schema in self._site_schema:
            try:
                if site_schema.match(html_text):
                    return site_schema
            except Exception as e:
                logger.error(f"站点匹配失败 {e}")
        return None

    def build(self, site_info: CommentedMap) -> Optional[ISiteUserInfo]:
        """
        构建站点信息
        """
        site_cookie = site_info.get("cookie")
        if not site_cookie:
            return None
        site_name = site_info.get("name")
        url = site_info.get("url")
        proxy = site_info.get("proxy")
        ua = site_info.get("ua")
        session = requests.Session()
        proxies = settings.PROXY if proxy else None
        proxy_server = settings.PROXY_SERVER if proxy else None
        render = site_info.get("render")

        logger.debug(f"站点 {site_name} url={url} site_cookie={site_cookie} ua={ua}")
        if render:
            # 演染模式
            html_text = PlaywrightHelper().get_page_source(url=url,
                                                           cookies=site_cookie,
                                                           ua=ua,
                                                           proxies=proxy_server)
        else:
            # 普通模式
            res = RequestUtils(cookies=site_cookie,
                               session=session,
                               ua=ua,
                               proxies=proxies
                               ).get_res(url=url)
            if res and res.status_code == 200:
                if "charset=utf-8" in res.text or "charset=UTF-8" in res.text:
                    res.encoding = "UTF-8"
                else:
                    res.encoding = res.apparent_encoding
                html_text = res.text
                # 第一次登录反爬
                if html_text.find("title") == -1:
                    i = html_text.find("window.location")
                    if i == -1:
                        return None
                    tmp_url = url + html_text[i:html_text.find(";")] \
                        .replace("\"", "") \
                        .replace("+", "") \
                        .replace(" ", "") \
                        .replace("window.location=", "")
                    res = RequestUtils(cookies=site_cookie,
                                       session=session,
                                       ua=ua,
                                       proxies=proxies
                                       ).get_res(url=tmp_url)
                    if res and res.status_code == 200:
                        if "charset=utf-8" in res.text or "charset=UTF-8" in res.text:
                            res.encoding = "UTF-8"
                        else:
                            res.encoding = res.apparent_encoding
                        html_text = res.text
                        if not html_text:
                            return None
                    else:
                        logger.error("站点 %s 被反爬限制：%s, 状态码：%s" % (site_name, url, res.status_code))
                        return None

                # 兼容假首页情况，假首页通常没有 <link rel="search" 属性
                if '"search"' not in html_text and '"csrf-token"' not in html_text:
                    res = RequestUtils(cookies=site_cookie,
                                       session=session,
                                       ua=ua,
                                       proxies=proxies
                                       ).get_res(url=url + "/index.php")
                    if res and res.status_code == 200:
                        if "charset=utf-8" in res.text or "charset=UTF-8" in res.text:
                            res.encoding = "UTF-8"
                        else:
                            res.encoding = res.apparent_encoding
                        html_text = res.text
                        if not html_text:
                            return None
            elif res is not None:
                logger.error(f"站点 {site_name} 连接失败，状态码：{res.status_code}")
                return None
            else:
                logger.error(f"站点 {site_name} 无法访问：{url}")
                return None
        # 解析站点类型
        if html_text:
            site_schema = self.__build_class(html_text)
            if not site_schema:
                logger.error("站点 %s 无法识别站点类型" % site_name)
                return None
            return site_schema(site_name, url, site_cookie, html_text, session=session, ua=ua, proxy=proxy)
        return None

    def __refresh_site_data(self, site_info: CommentedMap) -> Optional[ISiteUserInfo]:
        """
        更新单个site 数据信息
        :param site_info:
        :return:
        """
        site_name = site_info.get('name')
        site_url = site_info.get('url')
        if not site_url:
            return None
        unread_msg_notify = True
        try:
            site_user_info: ISiteUserInfo = self.build(site_info=site_info)
            if site_user_info:
                logger.debug(f"站点 {site_name} 开始以 {site_user_info.site_schema()} 模型解析")
                # 开始解析
                site_user_info.parse()
                logger.debug(f"站点 {site_name} 解析完成")

                # 获取不到数据时，仅返回错误信息，不做历史数据更新
                if site_user_info.err_msg:
                    self._sites_data.update({site_name: {"err_msg": site_user_info.err_msg}})
                    return None

                # 发送通知，存在未读消息
                self.__notify_unread_msg(site_name, site_user_info, unread_msg_notify)

                self._sites_data.update(
                    {
                        site_name: {
                            "upload": site_user_info.upload,
                            "username": site_user_info.username,
                            "user_level": site_user_info.user_level,
                            "join_at": site_user_info.join_at,
                            "download": site_user_info.download,
                            "ratio": site_user_info.ratio,
                            "seeding": site_user_info.seeding,
                            "seeding_size": site_user_info.seeding_size,
                            "leeching": site_user_info.leeching,
                            "bonus": site_user_info.bonus,
                            "url": site_url,
                            "err_msg": site_user_info.err_msg,
                            "message_unread": site_user_info.message_unread
                        }
                    })
                return site_user_info

        except Exception as e:
            logger.error(f"站点 {site_name} 获取流量数据失败：{str(e)}")
        return None

    def __notify_unread_msg(self, site_name: str, site_user_info: ISiteUserInfo, unread_msg_notify: bool):
        if site_user_info.message_unread <= 0:
            return
        if self._sites_data.get(site_name, {}).get('message_unread') == site_user_info.message_unread:
            return
        if not unread_msg_notify:
            return

        # 解析出内容，则发送内容
        if len(site_user_info.message_unread_contents) > 0:
            for head, date, content in site_user_info.message_unread_contents:
                msg_title = f"【站点 {site_user_info.site_name} 消息】"
                msg_text = f"时间：{date}\n标题：{head}\n内容：\n{content}"
                self.chain.post_message(title=msg_title, text=msg_text)
        else:
            self.chain.post_message(title=f"站点 {site_user_info.site_name} 收到 "
                                          f"{site_user_info.message_unread} 条新消息，请登陆查看")

    @eventmanager.register(EventType.SiteStatistic)
    def refresh(self, event: Event):
        """
        刷新站点数据
        """
        if event:
            logger.info("收到命令，开始执行站点数据刷新 ...")
        self.refresh_all_site_data(force=True)

    def refresh_all_site_data(self, force: bool = False, specify_sites: list = None):
        """
        多线程刷新站点下载上传量，默认间隔6小时
        """
        if not self.sites.get_indexers():
            return

        logger.info("开始刷新站点数据 ...")

        with lock:

            if not force \
                    and not specify_sites \
                    and self._last_update_time:
                return

            if specify_sites \
                    and not isinstance(specify_sites, list):
                specify_sites = [specify_sites]

            # 没有指定站点，默认使用全部站点
            if not specify_sites:
                refresh_sites = [site for site in self.sites.get_indexers() if not site.get("public")]
            else:
                refresh_sites = [site for site in self.sites.get_indexers() if
                                 site.get("name") in specify_sites]

            if not refresh_sites:
                return

            # 并发刷新
            with ThreadPool(min(len(refresh_sites), self._MAX_CONCURRENCY)) as p:
                p.map(self.__refresh_site_data, refresh_sites)

            # 获取今天的日期
            key = datetime.now().strftime('%Y-%m-%d')
            # 保存数据
            self.save_data(key, self._sites_data)
            # 更新时间
            self._last_update_time = datetime.now()

            # 通知刷新完成
            messages = []
            # 按照上传降序排序
            sites = self._sites_data.keys()
            uploads = [self._sites_data[site].get("upload") or 0 for site in sites]
            downloads = [self._sites_data[site].get("download") or 0 for site in sites]
            data_list = sorted(list(zip(sites, uploads, downloads)),
                               key=lambda x: x[1],
                               reverse=True)
            # 总上传
            incUploads = 0
            # 总下载
            incDownloads = 0
            for data in data_list:
                site = data[0]
                upload = int(data[1])
                download = int(data[2])
                if upload > 0 or download > 0:
                    incUploads += int(upload)
                    incDownloads += int(download)
                    messages.append(f"【{site}】\n"
                                    f"上传量：{StringUtils.str_filesize(upload)}\n"
                                    f"下载量：{StringUtils.str_filesize(download)}\n"
                                    f"————————————")

            if incDownloads or incUploads:
                messages.insert(0, f"【汇总】\n"
                                   f"总上传：{StringUtils.str_filesize(incUploads)}\n"
                                   f"总下载：{StringUtils.str_filesize(incDownloads)}\n"
                                   f"————————————")
            self.chain.post_message(title="站点数据统计", text="\n".join(messages))

        logger.info("站点数据刷新完成")
