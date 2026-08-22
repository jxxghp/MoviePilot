import base64
import re
from datetime import datetime
from typing import Any, Callable, Optional, Tuple, Union, Dict
from urllib.parse import urljoin

from app.application.site.sites import SitesHelper  # pylint: disable=import-error,no-name-in-module
from lxml import etree

from app.application.orchestration import ChainBase
from app.application.orchestration._interaction import InteractionChainMixin
from app.runtime.config import global_vars
from app.runtime.events import Event, eventmanager
from app.application.orchestration.data import SitePortProxy as SiteOper
from app.application.configuration import get_configured_system_config
from app.adapters.network.browser import PlaywrightHelper
from app.adapters.network.cloudflare import under_challenge
from app.application.security.cookie import CookieHelper
from app.adapters.external.cookiecloud import CookieCloudHelper
from app.application.messaging.site import SiteInteractionHandler
from app.application.rss import RssHelper
from app.runtime.log import logger
from app.schemas.notification import NotificationChannel
from app.schemas.message import Message
from app.schemas.site import SiteUserData
from app.schemas.types import EventType, MessageType
from app.adapters.network.http import RequestUtils
from app.domain.site import SiteUtils
from app.domain import site as site_rules
from app.foundation import size as size_tools
from app.foundation import url as url_tools
from app.foundation.dom import DomUtils

Site = Any


class SiteChain(InteractionChainMixin, ChainBase):
    """
    站点管理处理链
    """

    # 交互处理器类注入，供 InteractionChainMixin 的 parse_callback 委托
    _interaction_handler_type = SiteInteractionHandler

    def __init__(self):
        """初始化站点管理处理链及特殊站点测试器"""
        super().__init__()

        # 特殊站点登录验证
        self.special_site_test = {
            "zhuque.in": self.__zhuque_test,
            "m-team.io": self.__mteam_test,
            "m-team.cc": self.__mteam_test,
            "ptlsp.com": self.__indexphp_test,
            "1ptba.com": self.__indexphp_test,
            "star-space.net": self.__indexphp_test,
            "yemapt.org": self.__yema_test,
            "hddolby.com": self.__hddolby_test,
            "rousi.pro": self.__rousi_test,
            "sunnypt.top": self.__sunnypt_test,
        }

    def refresh_userdata(self, site: dict = None) -> Optional[SiteUserData]:
        """
        刷新站点的用户数据
        :param site:  站点
        :return: 用户数据
        """
        userdata: SiteUserData = self.unicast("refresh_userdata", site=site)
        if userdata:
            SiteOper().update_userdata(domain=site_rules.extract_domain(site.get("domain")),
                                       name=site.get("name"),
                                       payload=userdata.model_dump())
            # 发送事件
            eventmanager.send_event(EventType.SiteRefreshed, {
                "site_id": site.get("id")
            })
            self._post_site_messages(site=site, userdata=userdata)
            # 低分享率警告
            if userdata.ratio and float(userdata.ratio) < 1 and not bool(
                    re.search(r"(贵宾|VIP?)", userdata.user_level or "", re.IGNORECASE)):
                self.post_message(Message(
                    mtype=MessageType.SiteMessage,
                    title=f"【站点分享率低预警】",
                    text=f"站点 {site.get('name')} 分享率 {userdata.ratio}，请注意！"
                ))
        return userdata

    def _post_site_messages(self, site: dict, userdata: SiteUserData) -> None:
        """
        发送站点未读消息，并按解析器提供的来源标识做持久化去重。

        :param site: 站点索引配置
        :param userdata: 本次刷新的站点用户数据
        """
        if not userdata.message_unread:
            return
        if not userdata.message_unread_contents:
            self.post_message(Message(
                mtype=MessageType.SiteMessage,
                title=f"站点 {site.get('name')} 收到 "
                      f"{userdata.message_unread} 条新消息，请登陆查看",
                link=site.get("url")
            ))
            return
        for message in userdata.message_unread_contents:
            head, date, content, *metadata = message
            message_source = metadata[0] if metadata else None
            if message_source and self.messageoper.exists_by_source(message_source):
                continue
            msg_title = f"【站点 {site.get('name')} 消息】"
            msg_text = f"时间：{date}\n标题：{head}\n内容：\n{content}"
            self.post_message(Message(
                source=message_source,
                mtype=MessageType.SiteMessage,
                title=msg_title,
                text=msg_text,
                link=site.get("url")
            ))

    def refresh_userdatas(
            self,
            progress_callback: Optional[Callable[..., None]] = None,
    ) -> Optional[Dict[str, SiteUserData]]:
        """
        刷新所有站点的用户数据

        :param progress_callback: 定时服务进度更新回调
        """
        any_site_updated = False
        result = {}
        sites = [site for site in SitesHelper().get_indexers() if site.get("is_active")]
        total_num = len(sites)
        if progress_callback:
            progress_callback(
                value=0,
                text=f"开始刷新站点数据，共 {total_num} 个站点 ...",
                data={"total": total_num, "finished": 0},
            )
        for index, site in enumerate(sites, start=1):
            if global_vars.is_system_stopped:
                return None
            if progress_callback:
                progress_callback(
                    value=(index - 1) / total_num * 100 if total_num else 100,
                    text=f"正在刷新站点数据（{index}/{total_num}）{site.get('name')} ...",
                    data={
                        "total": total_num,
                        "finished": index - 1,
                        "current": site.get("id"),
                    },
                )
            userdata = self.refresh_userdata(site)
            if userdata:
                any_site_updated = True
                result[site.get("name")] = userdata
            if progress_callback:
                progress_callback(
                    value=index / total_num * 100 if total_num else 100,
                    text=f"站点数据（{index}/{total_num}）刷新完成",
                    data={"total": total_num, "finished": index},
                )
        if any_site_updated:
            eventmanager.send_event(EventType.SiteRefreshed, {
                "site_id": "*"
            })
        if progress_callback:
            progress_callback(value=100, text="站点数据刷新完成")

        return result

    def is_special_site(self, domain: str) -> bool:
        """
        判断是否特殊站点
        """
        return domain in self.special_site_test

    def __zhuque_test(self, site: Site) -> Tuple[bool, str]:
        """
        判断站点是否已经登陆：zhuique
        """
        # 获取token
        token = None
        user_agent = site.ua or self.runtime_config.user_agent
        res = RequestUtils(
            ua=user_agent,
            cookies=site.cookie,
            proxies=self.runtime_config.proxy if site.proxy else None,
            timeout=site.timeout or 15
        ).get_res(url=site.url)
        if res is None:
            return False, "无法打开网站！"
        if res.status_code == 200:
            csrf_token = re.search(r'<meta name="x-csrf-token" content="(.+?)">', res.text)
            if csrf_token:
                token = csrf_token.group(1)
        else:
            return False, f"错误：{res.status_code} {res.reason}"
        if not token:
            return False, "无法获取Token"
        # 调用查询用户信息接口
        user_res = RequestUtils(
            headers={
                'X-CSRF-TOKEN': token,
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": f"{user_agent}"
            },
            cookies=site.cookie,
            proxies=self.runtime_config.proxy if site.proxy else None,
            timeout=site.timeout or 15
        ).get_res(url=f"{site.url}api/user/getInfo")
        if user_res is None:
            return False, "无法打开网站！"
        if user_res.status_code == 200:
            user_info = user_res.json()
            if user_info and user_info.get("data"):
                return True, "连接成功"
            return False, "Cookie已失效"
        else:
            return False, f"错误：{user_res.status_code} {user_res.reason}"

    def __mteam_test(self, site: Site) -> Tuple[bool, str]:
        """
        判断站点是否已经登陆：m-team
        """
        user_agent = site.ua or self.runtime_config.user_agent
        domain = site_rules.extract_domain(site.url)
        url = f"https://api.{domain}/api/member/profile"
        headers = {
            "User-Agent": user_agent,
            "Accept": "application/json, text/plain, */*",
            "x-api-key": site.apikey,
        }
        res = RequestUtils(
            headers=headers,
            proxies=self.runtime_config.proxy if site.proxy else None,
            timeout=site.timeout or 15
        ).post_res(url=url)
        if res is None:
            return False, "无法打开网站！"
        if res.status_code == 200:
            user_info = res.json() or {}
            if user_info.get("data"):
                return True, "连接成功"
            return False, user_info.get("message", "鉴权已过期或无效")
        else:
            return False, f"错误：{res.status_code} {res.reason}"

    def __sunnypt_test(self, site: Site) -> Tuple[bool, str]:
        """
        通过 profile 接口测试 SunnyPT API Key 和下载权限

        :param site: SunnyPT 站点配置
        :return: 是否可用及状态信息
        """
        indexer = SitesHelper().get_indexer(site.domain) or {}
        api_url = str(
            indexer.get("api_url") or "https://api.sunnypt.top/api/v1/mp"
        ).rstrip("/")
        res = RequestUtils(
            headers={
                "Accept": "application/json",
                "User-Agent": site.ua or self.runtime_config.user_agent,
                "X-API-Key": site.apikey,
            },
            proxies=self.runtime_config.proxy if site.proxy else None,
            timeout=site.timeout or 15,
        ).get_res(url=f"{api_url}/profile")
        if res is None:
            return False, "无法连接 SunnyPT API 服务"
        if res.status_code != 200:
            return False, f"错误：{res.status_code} {res.reason}"
        try:
            payload = res.json() or {}
        except (TypeError, ValueError):
            return False, "SunnyPT API 响应不是有效 JSON"
        if str(payload.get("code")) != "0" or not isinstance(payload.get("data"), dict):
            return False, payload.get("msg") or "API Key 已过期或无效"
        if payload["data"].get("download_allowed") is False:
            return False, "当前账号没有下载权限"
        return True, "连接成功"

    def __yema_test(self, site: Site) -> Tuple[bool, str]:
        """
        判断站点是否已经登陆：yemapt
        """
        user_agent = site.ua or self.runtime_config.user_agent
        url = f"{site.url}api/consumer/fetchSelfDetail"
        headers = {
            "User-Agent": user_agent,
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
        }
        res = RequestUtils(
            headers=headers,
            cookies=site.cookie,
            proxies=self.runtime_config.proxy if site.proxy else None,
            timeout=site.timeout or 15
        ).get_res(url=url)
        if res is None:
            return False, "无法打开网站！"
        if res.status_code == 200:
            user_info = res.json()
            if user_info and user_info.get("success"):
                return True, "连接成功"
            return False, "Cookie已过期"
        else:
            return False, f"错误：{res.status_code} {res.reason}"

    def __indexphp_test(self, site: Site) -> Tuple[bool, str]:
        """
        判断站点是否已经登陆：ptlsp/1ptba
        """
        site.url = f"{site.url}index.php"
        return self.__test(site)

    def __hddolby_test(self, site: Site) -> Tuple[bool, str]:
        """
        判断站点是否已经登陆：hddolby
        """
        url = f"{site.url}api/v1/user/data"
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "x-api-key": site.apikey,
        }
        res = RequestUtils(
            headers=headers,
            proxies=self.runtime_config.proxy if site.proxy else None,
            timeout=site.timeout or 15
        ).get_res(url=url)
        if res is None:
            return False, "无法打开网站！"
        if res.status_code == 200:
            user_info = res.json()
            if user_info and user_info.get("status") == 0:
                return True, "连接成功"
            return False, "APIKEY已过期"
        else:
            return False, f"错误：{res.status_code} {res.reason}"

    def __rousi_test(self, site: Site) -> Tuple[bool, str]:
        """
        判断站点是否已经登陆：rousi
        """
        url = f"https://{site_rules.extract_domain(site.url)}/api/v1/profile"
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {site.apikey}",
        }
        res = RequestUtils(
            headers=headers,
            proxies=self.runtime_config.proxy if site.proxy else None,
            timeout=site.timeout or 15
        ).get_res(url=url)
        if res is None:
            return False, "无法打开网站！"
        if res.status_code == 200:
            user_info = res.json()
            if user_info and user_info.get("code") == 0:
                return True, "连接成功"
            return False, "APIKEY已过期"
        else:
            return False, f"错误：{res.status_code} {res.reason}"

    @staticmethod
    def __parse_favicon(url: str, cookie: str, ua: str) -> Tuple[str, Optional[str]]:
        """
        解析站点favicon,返回base64 fav图标
        :param url: 站点地址
        :param cookie: Cookie
        :param ua: User-Agent
        :return:
        """
        favicon_url = urljoin(url, "favicon.ico")
        res = RequestUtils(cookies=cookie, timeout=30, ua=ua).get_res(url=url)
        if res:
            html_text = res.text
        else:
            logger.error(f"获取站点页面失败：{url}")
            return favicon_url, None
        html = etree.HTML(html_text)
        try:
            if DomUtils.has_child_elements(html):
                fav_link = html.xpath('//head/link[contains(@rel, "icon")]/@href')
                if fav_link:
                    favicon_url = urljoin(url, fav_link[0])

            res = RequestUtils(cookies=cookie, timeout=15, ua=ua).get_res(url=favicon_url)
            if res:
                return favicon_url, base64.b64encode(res.content).decode()
            else:
                logger.error(f"获取站点图标失败：{favicon_url}")
        finally:
            if html is not None:
                del html
        return favicon_url, None

    def sync_cookies(
            self,
            manual: bool = False,
            progress_callback: Optional[Callable[..., None]] = None,
    ) -> Tuple[bool, str]:
        """
        通过CookieCloud同步站点Cookie

        :param manual: 是否手动同步
        :param progress_callback: 定时服务进度更新回调
        """

        logger.info("开始同步CookieCloud站点 ...")
        if progress_callback:
            progress_callback(value=0, text="开始下载 CookieCloud 数据 ...")
        cookies, msg = CookieCloudHelper().download()
        if not cookies:
            logger.error(f"CookieCloud同步失败：{msg}")
            if progress_callback:
                progress_callback(value=100, text=f"CookieCloud同步失败：{msg}")
            if manual:
                self.messagehelper.put(msg, title="CookieCloud同步失败", role="system")
            return False, msg
        siteshelper = SitesHelper()
        siteoper = SiteOper()
        rsshelper = RssHelper()
        total_num = len(cookies)
        update_count = add_count = fail_count = 0
        for index, (domain, cookie) in enumerate(cookies.items(), start=1):
            # 检查系统是否停止
            if global_vars.is_system_stopped:
                logger.info("系统正在停止，中断CookieCloud同步")
                return False, "系统正在停止，同步被中断"
            if progress_callback:
                progress_callback(
                    value=(index - 1) / total_num * 100 if total_num else 100,
                    text=f"正在同步 CookieCloud 站点（{index}/{total_num}）{domain} ...",
                    data={
                        "total": total_num,
                        "finished": index - 1,
                        "current": domain,
                    },
                )

            indexer = siteshelper.get_indexer(domain)
            site_info = siteoper.get_by_domain(domain)
            updated, added, failed, should_finalize = self._sync_cookiecloud_domain(
                domain=domain,
                cookie=cookie,
                indexer=indexer,
                site_info=site_info,
                siteoper=siteoper,
                rsshelper=rsshelper,
            )
            update_count += updated
            add_count += added
            fail_count += failed
            if not should_finalize:
                continue

            # 通知站点更新
            if indexer:
                eventmanager.send_event(EventType.SiteUpdated, {
                    "domain": domain,
                })
            if progress_callback:
                progress_callback(
                    value=index / total_num * 100 if total_num else 100,
                    text=f"CookieCloud 站点（{index}/{total_num}）同步完成",
                    data={"total": total_num, "finished": index},
                )
        # 处理完成
        ret_msg = f"更新了{update_count}个站点，新增了{add_count}个站点"
        if fail_count > 0:
            ret_msg += f"，{fail_count}个站点添加失败，下次同步时将重试，也可以手动添加"
        if manual:
            self.messagehelper.put(ret_msg, title="CookieCloud同步成功", role="system")
        logger.info(f"CookieCloud同步成功：{ret_msg}")
        if progress_callback:
            progress_callback(value=100, text=f"CookieCloud同步成功：{ret_msg}")
        return True, ret_msg

    def _sync_cookiecloud_domain(
            self,
            domain: str,
            cookie: str,
            indexer: Optional[dict],
            site_info: Any,
            siteoper: SiteOper,
            rsshelper: RssHelper,
    ) -> Tuple[int, int, int, bool]:
        """处理单个域名，并返回计数与是否继续发送更新事件。"""
        if site_info and site_info.is_active:
            status, _ = self.test(domain)
            if status:
                logger.info(f"站点【{site_info.name}】连通性正常，不同步CookieCloud数据")
                if not site_info.public and not site_info.rss:
                    rss_url, errmsg = rsshelper.get_rss_link(
                        url=site_info.url,
                        cookie=cookie,
                        ua=site_info.ua or self.runtime_config.user_agent,
                        proxy=bool(site_info.proxy),
                        timeout=site_info.timeout or 15,
                    )
                    if rss_url:
                        siteoper.update_rss(domain=domain, rss=rss_url)
                    else:
                        logger.warning(errmsg)
                return 0, 0, 0, False
            logger.info(f"更新站点 {domain} Cookie ...")
            siteoper.update_cookie(domain=domain, cookies=cookie)
            return 1, 0, 0, True
        if not indexer:
            return 0, 0, 0, True
        if self._cookiecloud_blacklisted(domain):
            logger.warning(f"站点 {domain} 已在黑名单中，不添加站点")
            return 0, 0, 0, False
        domain_url = self._cookiecloud_indexer_domain(indexer, domain)
        proxy, response = self._cookiecloud_connect(domain_url, cookie, indexer)
        if response is None:
            return 0, 0, 1, False
        if response.status_code not in [200, 500, 403]:
            logger.warning(f"站点 {indexer.get('name')} 连接状态码：{response.status_code}，无法添加站点")
            return 0, 0, 1, False
        if not indexer.get("public") and not SiteUtils.is_logged_in(response.text):
            logger.warning(f"站点 {indexer.get('name')} 登录失败，无法添加站点")
            return 0, 0, 1, False
        rss_url = None
        if not indexer.get("public") and domain_url:
            rss_url, errmsg = rsshelper.get_rss_link(
                url=domain_url, cookie=cookie, ua=self.runtime_config.user_agent, proxy=proxy
            )
            if errmsg:
                logger.warning(errmsg)
        siteoper.add(
            name=indexer.get("name"), url=domain_url, domain=domain, cookie=cookie,
            rss=rss_url, proxy=1 if proxy else 0, public=1 if indexer.get("public") else 0,
        )
        return 0, 1, 0, True

    def _cookiecloud_blacklisted(self, domain: str) -> bool:
        """判断域名是否命中 CookieCloud 黑名单。"""
        blacklist = self.runtime_config.cookiecloud_blacklist
        return bool(blacklist) and any(
            site_rules.extract_domain(domain) == site_rules.extract_domain(item)
            for item in str(blacklist).split(",")
        )

    @staticmethod
    def _cookiecloud_indexer_domain(indexer: dict, sub_domain: str) -> str:
        """根据索引器主域名和扩展域名解析实际访问地址。"""
        if site_rules.extract_domain(indexer.get("domain")) == sub_domain:
            return indexer.get("domain")
        for ext_domain in indexer.get("ext_domains", []):
            if site_rules.extract_domain(ext_domain) == sub_domain:
                return ext_domain
        return sub_domain

    def _cookiecloud_connect(self, domain_url: str, cookie: str, indexer: dict) -> Tuple[bool, Any]:
        """连接新站点，必要时通过已配置代理重试。"""
        response = RequestUtils(cookies=cookie, ua=self.runtime_config.user_agent).get_res(url=domain_url)
        if response is not None or not self.runtime_config.proxy_host:
            return False, response
        logger.info(f"站点 {indexer.get('name')} 初次连接失败，尝试通过代理重试...")
        response = RequestUtils(
            cookies=cookie, ua=self.runtime_config.user_agent, proxies=self.runtime_config.proxy
        ).get_res(url=domain_url)
        return True, response

    @eventmanager.register(EventType.SiteUpdated)
    def cache_site_icon(self, event: Event):
        """
        缓存站点图标
        """
        if not event:
            return
        event_data = event.event_data or {}
        # 主域名
        domain = event_data.get("domain")
        if not domain:
            return
        if str(domain).startswith("http"):
            domain = site_rules.extract_domain(domain)
        # 站点信息
        siteoper = SiteOper()
        siteshelper = SitesHelper()
        siteinfo = siteoper.get_by_domain(domain)
        if not siteinfo:
            logger.warn(f"未维护站点 {domain} 信息！")
            return
        # Cookie
        cookie = siteinfo.cookie
        # 索引器
        indexer = siteshelper.get_indexer(domain)
        if not indexer:
            logger.warn(f"站点 {domain} 索引器不存在！")
            return
        # 查询站点图标
        logger.info(f"开始缓存站点 {indexer.get('name')} 图标 ...")
        icon_url, icon_base64 = self.__parse_favicon(url=indexer.get("domain"),
                                                     cookie=cookie,
                                                     ua=self.runtime_config.user_agent)
        if icon_url:
            siteoper.update_icon(name=indexer.get("name"),
                                 domain=domain,
                                 icon_url=icon_url,
                                 icon_base64=icon_base64)
            logger.info(f"缓存站点 {indexer.get('name')} 图标成功")
        else:
            logger.warn(f"缓存站点 {indexer.get('name')} 图标失败")

    @eventmanager.register(EventType.SiteUpdated)
    def clear_site_data(self, event: Event):
        """
        清理站点数据
        """
        if not event:
            return
        event_data = event.event_data or {}
        # 主域名
        domain = event_data.get("domain")
        if not domain:
            return
        # 获取主域名中间那段
        domain_host = url_tools.host_label(domain)
        # 查询以"site.domain_host"开头的配置项，并清除
        # SystemConfigService 未提供 all()，无参 get() 经仓库返回全部配置字典
        systemconfig = get_configured_system_config()
        site_keys = systemconfig.get().keys()
        for key in site_keys:
            if key.startswith(f"site.{domain_host}"):
                logger.info(f"清理站点配置：{key}")
                systemconfig.delete(key)

    @eventmanager.register(EventType.SiteUpdated)
    def cache_site_userdata(self, event: Event):
        """
        缓存站点用户数据
        """
        if not event:
            return
        event_data = event.event_data or {}
        # 主域名
        domain = event_data.get("domain")
        if not domain:
            return
        if str(domain).startswith("http"):
            domain = site_rules.extract_domain(domain)
        indexer = SitesHelper().get_indexer(domain)
        if not indexer:
            return
        # 刷新站点用户数据
        self.refresh_userdata(site=indexer) or {}

    def test(self, url: str) -> Tuple[bool, str]:
        """
        测试站点是否可用
        :param url: 站点域名
        :return: (是否可用, 错误信息)
        """
        # 检查域名是否可用
        domain = site_rules.extract_domain(url)
        siteoper = SiteOper()
        site_info = siteoper.get_by_domain(domain)
        if not site_info:
            return False, f"站点【{url}】不存在"

        # 模拟登录
        try:
            # 开始记时
            start_time = datetime.now()
            # 特殊站点测试
            if self.special_site_test.get(domain):
                state, message = self.special_site_test[domain](site_info)
            else:
                # 通用站点测试
                state, message = self.__test(site_info)
            # 统计
            seconds = (datetime.now() - start_time).seconds
            if state:
                siteoper.success(domain=domain, seconds=seconds)
            else:
                siteoper.fail(domain)
            return state, message
        except Exception as e:
            return False, f"{str(e)}！"

    def __test(self, site_info: Site) -> Tuple[bool, str]:
        """
        通用站点测试
        """
        site_url = site_info.url
        site_cookie = site_info.cookie
        ua = site_info.ua or self.runtime_config.user_agent
        render = site_info.render
        public = site_info.public
        proxies = self.runtime_config.proxy if site_info.proxy else None
        proxy_server = self.runtime_config.proxy_server if site_info.proxy else None
        timeout = site_info.timeout or 60

        # 访问链接
        if render:
            page_source = PlaywrightHelper().get_page_source(url=site_url,
                                                             cookies=site_cookie,
                                                             ua=ua,
                                                             proxies=proxy_server,
                                                             timeout=timeout)
            if not public and not SiteUtils.is_logged_in(page_source):
                if under_challenge(page_source):
                    return False, f"无法通过Cloudflare！"
                return False, f"仿真登录失败，Cookie已失效！"
        else:
            res = RequestUtils(cookies=site_cookie,
                               ua=ua,
                               proxies=proxies
                               ).get_res(url=site_url)
            # 判断登录状态
            if res and res.status_code in [200, 500, 403]:
                content = res.text
                if not public and not SiteUtils.is_logged_in(content):
                    if under_challenge(content):
                        msg = "站点被Cloudflare防护，请打开站点浏览器仿真"
                    elif res.status_code == 200:
                        msg = "Cookie已失效"
                    else:
                        msg = f"错误：{res.status_code} {res.reason}"
                    return False, f"{msg}！"
                elif public and res.status_code != 200:
                    return False, f"错误：{res.status_code} {res.reason}！"
            elif res is not None:
                return False, f"错误：{res.status_code} {res.reason}！"
            else:
                return False, f"无法打开网站！"
        return True, "连接成功"

    def _interaction_handler(self) -> "SiteInteractionHandler":
        """构造 /sites 交互处理器，Cookie 更新动作由本链提供。"""
        return SiteInteractionHandler(
            messenger=self,
            cookie_updater=self.update_cookie,
            repository=SiteOper(),
        )

    def remote_disable(self, arg_str: str, channel: NotificationChannel,
                       userid: Union[str, int] = None, source: Optional[str] = None):
        """
        禁用站点
        """
        if not arg_str:
            return
        arg_str = str(arg_str).strip()
        if not arg_str.isdigit():
            return
        site_id = int(arg_str)
        siteoper = SiteOper()
        site = siteoper.get(site_id)
        if not site:
            self.post_message(Message(
                channel=channel,
                title=f"站点编号 {site_id} 不存在！",
                userid=userid,
                save_history=False))
            return
        # 禁用站点
        siteoper.update(site_id, {
            "is_active": False
        })
        # 重新发送消息
        self.remote_list(channel=channel, userid=userid, source=source)

    def remote_enable(self, arg_str: str, channel: NotificationChannel,
                      userid: Union[str, int] = None, source: Optional[str] = None):
        """
        启用站点
        """
        if not arg_str:
            return
        arg_strs = str(arg_str).split()
        siteoper = SiteOper()
        for arg_str in arg_strs:
            arg_str = arg_str.strip()
            if not arg_str.isdigit():
                continue
            site_id = int(arg_str)
            site = siteoper.get(site_id)
            if not site:
                self.post_message(Message(
                    channel=channel,
                    title=f"站点编号 {site_id} 不存在！",
                    userid=userid,
                    save_history=False))
                return
            # 禁用站点
            siteoper.update(site_id, {
                "is_active": True
            })
        # 重新发送消息
        self.remote_list(channel=channel, userid=userid, source=source)

    def update_cookie(self, site_info: Site,
                      username: str, password: str, two_step_code: Optional[str] = None) -> Tuple[bool, str]:
        """
        根据用户名密码更新站点Cookie
        :param site_info: 站点信息
        :param username: 用户名
        :param password: 密码
        :param two_step_code: 二步验证码或密钥
        :return: (是否成功, 错误信息)
        """
        # 更新站点Cookie
        result = CookieHelper().get_site_cookie_ua(
            url=site_info.url,
            username=username,
            password=password,
            two_step_code=two_step_code,
            proxies=self.runtime_config.proxy_server if site_info.proxy else None,
            timeout=site_info.timeout or 60
        )
        if result:
            cookie, ua, msg = result
            if not cookie:
                return False, msg
            SiteOper().update(site_info.id, {
                "cookie": cookie,
                "ua": ua
            })
            return True, msg
        return False, "未知错误"

    def remote_cookie(self, arg_str: str, channel: NotificationChannel,
                      userid: Union[str, int] = None, source: Optional[str] = None):
        """
        使用用户名密码更新站点Cookie
        """
        err_title = "请输入正确的命令格式：/site_cookie [id] [username] [password] [2fa_code/secret]，" \
                    "[id]为站点编号，[uername]为站点用户名，[password]为站点密码，[2fa_code/secret]为站点二步验证码或密钥"
        if not arg_str:
            self.post_message(Message(
                channel=channel,
                source=source,
                title=err_title,
                userid=userid,
                save_history=False))
            return
        arg_str = str(arg_str).strip()
        args = arg_str.split()
        # 二步验证码
        two_step_code = None
        if len(args) == 4:
            two_step_code = args[3]
        elif len(args) != 3:
            self.post_message(Message(
                channel=channel,
                source=source,
                title=err_title,
                userid=userid,
                save_history=False))
            return
        site_id = args[0]
        if not site_id.isdigit():
            self.post_message(Message(
                channel=channel,
                source=source,
                title=err_title,
                userid=userid,
                save_history=False))
            return
        # 站点ID
        site_id = int(site_id)
        # 站点信息
        site_info = SiteOper().get(site_id)
        if not site_info:
            self.post_message(Message(
                channel=channel,
                source=source,
                title=f"站点编号 {site_id} 不存在！",
                userid=userid,
                save_history=False))
            return
        self.post_message(Message(
            channel=channel,
            source=source,
            title=f"开始更新【{site_info.name}】Cookie&UA ...",
            userid=userid,
            save_history=False))
        # 用户名
        username = args[1]
        # 密码
        password = args[2]
        # 更新Cookie
        status, msg = self.update_cookie(site_info=site_info,
                                         username=username,
                                         password=password,
                                         two_step_code=two_step_code)
        if not status:
            logger.error(msg)
            self.post_message(Message(
                channel=channel,
                source=source,
                title=f"【{site_info.name}】 Cookie&UA更新失败！",
                text=f"错误原因：{msg}",
                userid=userid,
                save_history=False))
        else:
            self.post_message(Message(
                channel=channel,
                source=source,
                title=f"【{site_info.name}】 Cookie&UA更新成功",
                userid=userid,
                save_history=False))

    def remote_refresh_userdatas(self, channel: NotificationChannel,
                                 userid: Union[str, int] = None, source: Optional[str] = None):
        """
        刷新所有站点用户数据
        """
        logger.info("收到命令，开始刷新站点数据 ...")
        self.post_message(Message(
            channel=channel,
            source=source,
            title="开始刷新站点数据 ...",
            userid=userid,
            save_history=False,
        ))
        # 刷新站点数据
        site_datas = self.refresh_userdatas()
        if site_datas:
            # 发送消息
            messages = {}
            # 总上传
            incUploads = 0
            # 总下载
            incDownloads = 0
            # 今天日期
            today_date = datetime.now().strftime("%Y-%m-%d")

            for rand, site in enumerate(site_datas.keys()):
                upload = int(site_datas[site].upload or 0)
                download = int(site_datas[site].download or 0)
                updated_date = site_datas[site].updated_day
                if updated_date and updated_date != today_date:
                    updated_date = f"（{updated_date}）"
                else:
                    updated_date = ""

                if upload > 0 or download > 0:
                    incUploads += upload
                    incDownloads += download
                    messages[upload + (rand / 1000)] = (
                            f"【{site}】{updated_date}\n"
                            + f"上传量：{size_tools.format_compact_size(upload)}\n"
                            + f"下载量：{size_tools.format_compact_size(download)}\n"
                            + "————————————"
                    )
            if incDownloads or incUploads:
                sorted_messages = [messages[key] for key in sorted(messages.keys(), reverse=True)]
                sorted_messages.insert(0, f"【汇总】\n"
                                          f"总上传：{size_tools.format_compact_size(incUploads)}\n"
                                          f"总下载：{size_tools.format_compact_size(incDownloads)}\n"
                                          f"————————————")
                self.post_message(Message(
                    channel=channel,
                    source=source,
                    title="【站点数据统计】",
                    text="\n".join(sorted_messages),
                    userid=userid,
                    save_history=False
                ))
        else:
            self.post_message(Message(
                channel=channel,
                source=source,
                title="没有刷新到任何站点数据！",
                userid=userid,
                save_history=False,
            ))
