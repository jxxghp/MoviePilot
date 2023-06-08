from datetime import datetime
from multiprocessing.dummy import Pool as ThreadPool
from threading import Lock
from typing import Optional, Any

import requests
from ruamel.yaml import CommentedMap

from app.core import settings
from app.helper import ModuleHelper
from app.helper.sites import SitesHelper
from app.log import logger
from app.plugins import _PluginBase
from app.plugins.sitestatistics.siteuserinfo import ISiteUserInfo
from app.utils.http import RequestUtils

lock = Lock()


class SiteStatistics(_PluginBase):
    sites = None

    _MAX_CONCURRENCY: int = 10
    _last_update_time: Optional[datetime] = None
    _sites_data: dict = {}
    _site_schema: list = None

    def init_plugin(self, config: dict = None):
        # 加载模块
        self._site_schema = ModuleHelper.load('app.plugins.sitestatistics.siteuserinfo',
                                              filter_func=lambda _, obj: hasattr(obj, 'schema'))
        self._site_schema.sort(key=lambda x: x.order)
        # 站点管理
        self.sites = SitesHelper()
        # 站点上一次更新时间
        self._last_update_time = None
        # 站点数据
        self._sites_data = {}

    def stop_service(self):
        pass

    def __build_class(self, html_text: str) -> Any:
        for site_schema in self._site_schema:
            try:
                if site_schema.match(html_text):
                    return site_schema
            except Exception as e:
                logger.error(f"站点 {site_schema.name} 匹配失败 {e}")
        return None

    def build(self, url: str, site_name: str,
              site_cookie: str = None,
              ua: str = None,
              proxy: bool = False) -> Any:
        if not site_cookie:
            return None
        session = requests.Session()
        logger.debug(f"站点 {site_name} url={url} site_cookie={site_cookie} ua={ua}")
        proxies = settings.PROXY if proxy else None
        res = RequestUtils(cookies=site_cookie,
                           session=session,
                           headers=ua,
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
                    .replace("\"", "").replace("+", "").replace(" ", "").replace("window.location=", "")
                res = RequestUtils(cookies=site_cookie,
                                   session=session,
                                   headers=ua,
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
                                   headers=ua,
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
        site_schema = self.__build_class(html_text)
        if not site_schema:
            logger.error("站点 %s 无法识别站点类型" % site_name)
            return None
        return site_schema(site_name, url, site_cookie, html_text, session=session, ua=ua, proxy=proxy)

    def __refresh_site_data(self, site_info: CommentedMap):
        """
        更新单个site 数据信息
        :param site_info:
        :return:
        """
        site_name = site_info.get("name")
        site_url = site_info.get("strict_url")
        if not site_url:
            return
        site_cookie = site_info.get("cookie")
        ua = site_info.get("ua")
        unread_msg_notify = True
        proxy = site_info.get("proxy")
        try:
            site_user_info = self.build(url=site_url,
                                        site_name=site_name,
                                        site_cookie=site_cookie,
                                        ua=ua,
                                        proxy=proxy)
            if site_user_info:
                logger.debug(f"站点 {site_name} 开始以 {site_user_info.site_schema()} 模型解析")
                # 开始解析
                site_user_info.parse()
                logger.debug(f"站点 {site_name} 解析完成")

                # 获取不到数据时，仅返回错误信息，不做历史数据更新
                if site_user_info.err_msg:
                    self._sites_data.update({site_name: {"err_msg": site_user_info.err_msg}})
                    return

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
                self.chain.run_module("post_message", title=msg_title, text=msg_text)
        else:
            self.chain.run_module("post_message",
                                  title=f"站点 {site_user_info.site_name} 收到 "
                                        f"{site_user_info.message_unread} 条新消息，请登陆查看")

    def refresh_all_site_data(self, force: bool = False, specify_sites: list = None):
        """
        多线程刷新站点下载上传量，默认间隔6小时
        """
        if not self.sites.get_indexers():
            return

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
                refresh_sites = self.sites.get_indexers()
            else:
                refresh_sites = [site for site in self.sites.get_indexers() if
                                 site.get("name") in specify_sites]

            if not refresh_sites:
                return

            # 并发刷新
            with ThreadPool(min(len(refresh_sites), self._MAX_CONCURRENCY)) as p:
                site_user_infos = p.map(self.__refresh_site_data, refresh_sites)
                site_user_infos = [info for info in site_user_infos if info]

            print(site_user_infos)
            # TODO 登记历史数据
            # TODO 实时用户数据
            # TODO 更新站点图标
            # TODO 实时做种信息

            # 更新时间
            self._last_update_time = datetime.now()

    @staticmethod
    def __todict(raw_statistics):
        statistics = []
        for site in raw_statistics:
            statistics.append({"site": site.SITE,
                               "username": site.USERNAME,
                               "user_level": site.USER_LEVEL,
                               "join_at": site.JOIN_AT,
                               "update_at": site.UPDATE_AT,
                               "upload": site.UPLOAD,
                               "download": site.DOWNLOAD,
                               "ratio": site.RATIO,
                               "seeding": site.SEEDING,
                               "leeching": site.LEECHING,
                               "seeding_size": site.SEEDING_SIZE,
                               "bonus": site.BONUS,
                               "url": site.URL,
                               "msg_unread": site.MSG_UNREAD
                               })
        return statistics
