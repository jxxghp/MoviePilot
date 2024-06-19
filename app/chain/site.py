import base64
import re
from datetime import datetime
from typing import Tuple, Optional
from typing import Union
from urllib.parse import urljoin

from lxml import etree

from app.chain import ChainBase
from app.core.config import settings
from app.core.event import eventmanager, Event, EventManager
from app.db.models.site import Site
from app.db.site_oper import SiteOper
from app.db.siteicon_oper import SiteIconOper
from app.db.systemconfig_oper import SystemConfigOper
from app.db.sitestatistic_oper import SiteStatisticOper
from app.helper.browser import PlaywrightHelper
from app.helper.cloudflare import under_challenge
from app.helper.cookie import CookieHelper
from app.helper.cookiecloud import CookieCloudHelper
from app.helper.message import MessageHelper
from app.helper.rss import RssHelper
from app.helper.sites import SitesHelper
from app.log import logger
from app.schemas import MessageChannel, Notification
from app.schemas.types import EventType
from app.utils.http import RequestUtils
from app.utils.site import SiteUtils
from app.utils.string import StringUtils


class SiteChain(ChainBase):
    """
    站点管理处理链
    """

    def __init__(self):
        super().__init__()
        self.siteoper = SiteOper()
        self.siteiconoper = SiteIconOper()
        self.siteshelper = SitesHelper()
        self.rsshelper = RssHelper()
        self.cookiehelper = CookieHelper()
        self.message = MessageHelper()
        self.cookiecloud = CookieCloudHelper()
        self.systemconfig = SystemConfigOper()
        self.sitestatistic = SiteStatisticOper()

        # 特殊站点登录验证
        self.special_site_test = {
            "zhuque.in": self.__zhuque_test,
            "m-team.io": self.__mteam_test,
            "m-team.cc": self.__mteam_test,
            "ptlsp.com": self.__indexphp_test,
            "1ptba.com": self.__indexphp_test,
            "star-space.net": self.__indexphp_test,
            "yemapt.org": self.__yema_test,
        }

    def is_special_site(self, domain: str) -> bool:
        """
        判断是否特殊站点
        """
        return domain in self.special_site_test

    @staticmethod
    def __zhuque_test(site: Site) -> Tuple[bool, str]:
        """
        判断站点是否已经登陆：zhuique
        """
        # 获取token
        token = None
        user_agent = site.ua or settings.USER_AGENT
        res = RequestUtils(
            ua=user_agent,
            cookies=site.cookie,
            proxies=settings.PROXY if site.proxy else None,
            timeout=site.timeout or 15
        ).get_res(url=site.url)
        if res and res.status_code == 200:
            csrf_token = re.search(r'<meta name="x-csrf-token" content="(.+?)">', res.text)
            if csrf_token:
                token = csrf_token.group(1)
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
            proxies=settings.PROXY if site.proxy else None,
            timeout=site.timeout or 15
        ).get_res(url=f"{site.url}api/user/getInfo")
        if user_res and user_res.status_code == 200:
            user_info = user_res.json()
            if user_info and user_info.get("data"):
                return True, "连接成功"
        return False, "Cookie已失效"

    @staticmethod
    def __mteam_test(site: Site) -> Tuple[bool, str]:
        """
        判断站点是否已经登陆：m-team
        """
        user_agent = site.ua or settings.USER_AGENT
        domain = StringUtils.get_url_domain(site.url)
        url = f"https://api.{domain}/api/member/profile"
        headers = {
            "Content-Type": "application/json",
            "User-Agent": user_agent,
            "Accept": "application/json, text/plain, */*",
            "Authorization": site.token
        }
        res = RequestUtils(
            headers=headers,
            proxies=settings.PROXY if site.proxy else None,
            timeout=site.timeout or 15
        ).post_res(url=url)
        if res and res.status_code == 200:
            user_info = res.json()
            if user_info and user_info.get("data"):
                # 更新最后访问时间
                res = RequestUtils(headers=headers,
                                   timeout=site.timeout or 15,
                                   proxies=settings.PROXY if site.proxy else None,
                                   referer=f"{site.url}index"
                                   ).post_res(url=f"https://api.{domain}/api/member/updateLastBrowse")
                if res:
                    return True, "连接成功"
                else:
                    return True, f"连接成功，但更新状态失败"
        return False, "鉴权已过期或无效"

    @staticmethod
    def __yema_test(site: Site) -> Tuple[bool, str]:
        """
        判断站点是否已经登陆：yemapt
        """
        user_agent = site.ua or settings.USER_AGENT
        url = f"{site.url}api/consumer/fetchSelfDetail"
        headers = {
            "User-Agent": user_agent,
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
        }
        res = RequestUtils(
            headers=headers,
            cookies=site.cookie,
            proxies=settings.PROXY if site.proxy else None,
            timeout=site.timeout or 15
        ).get_res(url=url)
        if res and res.status_code == 200:
            user_info = res.json()
            if user_info and user_info.get("success"):
                return True, "连接成功"
        return False, "Cookie已过期"

    def __indexphp_test(self, site: Site) -> Tuple[bool, str]:
        """
        判断站点是否已经登陆：ptlsp/1ptba
        """
        site.url = f"{site.url}index.php"
        return self.__test(site)

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
        if html:
            fav_link = html.xpath('//head/link[contains(@rel, "icon")]/@href')
            if fav_link:
                favicon_url = urljoin(url, fav_link[0])

        res = RequestUtils(cookies=cookie, timeout=15, ua=ua).get_res(url=favicon_url)
        if res:
            return favicon_url, base64.b64encode(res.content).decode()
        else:
            logger.error(f"获取站点图标失败：{favicon_url}")
        return favicon_url, None

    def sync_cookies(self, manual=False) -> Tuple[bool, str]:
        """
        通过CookieCloud同步站点Cookie
        """

        def __indexer_domain(inx: dict, sub_domain: str) -> str:
            """
            根据主域名获取索引器地址
            """
            if StringUtils.get_url_domain(inx.get("domain")) == sub_domain:
                return inx.get("domain")
            for ext_d in inx.get("ext_domains"):
                if StringUtils.get_url_domain(ext_d) == sub_domain:
                    return ext_d
            return sub_domain

        logger.info("开始同步CookieCloud站点 ...")
        cookies, msg = self.cookiecloud.download()
        if not cookies:
            logger.error(f"CookieCloud同步失败：{msg}")
            if manual:
                self.message.put(msg, title="CookieCloud同步失败", role="system")
            return False, msg
        # 保存Cookie或新增站点
        _update_count = 0
        _add_count = 0
        _fail_count = 0
        for domain, cookie in cookies.items():
            # 索引器信息
            indexer = self.siteshelper.get_indexer(domain)
            # 数据库的站点信息
            site_info = self.siteoper.get_by_domain(domain)
            if site_info and site_info.is_active == 1:
                # 站点已存在，检查站点连通性
                status, msg = self.test(domain)
                # 更新站点Cookie
                if status:
                    logger.info(f"站点【{site_info.name}】连通性正常，不同步CookieCloud数据")
                    # 更新站点rss地址
                    if not site_info.public and not site_info.rss:
                        # 自动生成rss地址
                        rss_url, errmsg = self.rsshelper.get_rss_link(
                            url=site_info.url,
                            cookie=cookie,
                            ua=site_info.ua or settings.USER_AGENT,
                            proxy=True if site_info.proxy else False
                        )
                        if rss_url:
                            logger.info(f"更新站点 {domain} RSS地址 ...")
                            self.siteoper.update_rss(domain=domain, rss=rss_url)
                        else:
                            logger.warn(errmsg)
                    continue
                # 更新站点Cookie
                logger.info(f"更新站点 {domain} Cookie ...")
                self.siteoper.update_cookie(domain=domain, cookies=cookie)
                _update_count += 1
            elif indexer:
                if settings.COOKIECLOUD_BLACKLIST and any(
                        StringUtils.get_url_domain(domain) == StringUtils.get_url_domain(black_domain) for black_domain
                        in str(settings.COOKIECLOUD_BLACKLIST).split(",")):
                    logger.warn(f"站点 {domain} 已在黑名单中，不添加站点")
                    continue
                # 新增站点
                domain_url = __indexer_domain(inx=indexer, sub_domain=domain)
                res = RequestUtils(cookies=cookie,
                                   ua=settings.USER_AGENT
                                   ).get_res(url=domain_url)
                if res and res.status_code in [200, 500, 403]:
                    if not indexer.get("public") and not SiteUtils.is_logged_in(res.text):
                        _fail_count += 1
                        if under_challenge(res.text):
                            logger.warn(f"站点 {indexer.get('name')} 被Cloudflare防护，无法登录，无法添加站点")
                            continue
                        logger.warn(
                            f"站点 {indexer.get('name')} 登录失败，没有该站点账号或Cookie已失效，无法添加站点")
                        continue
                elif res is not None:
                    _fail_count += 1
                    logger.warn(f"站点 {indexer.get('name')} 连接状态码：{res.status_code}，无法添加站点")
                    continue
                else:
                    _fail_count += 1
                    logger.warn(f"站点 {indexer.get('name')} 连接失败，无法添加站点")
                    continue
                # 获取rss地址
                rss_url = None
                if not indexer.get("public") and domain_url:
                    # 自动生成rss地址
                    rss_url, errmsg = self.rsshelper.get_rss_link(url=domain_url,
                                                                  cookie=cookie,
                                                                  ua=settings.USER_AGENT)
                    if errmsg:
                        logger.warn(errmsg)
                # 插入数据库
                logger.info(f"新增站点 {indexer.get('name')} ...")
                self.siteoper.add(name=indexer.get("name"),
                                  url=domain_url,
                                  domain=domain,
                                  cookie=cookie,
                                  rss=rss_url,
                                  public=1 if indexer.get("public") else 0)
                _add_count += 1

            # 通知站点更新
            if indexer:
                EventManager().send_event(EventType.SiteUpdated, {
                    "domain": domain,
                })
        # 处理完成
        ret_msg = f"更新了{_update_count}个站点，新增了{_add_count}个站点"
        if _fail_count > 0:
            ret_msg += f"，{_fail_count}个站点添加失败，下次同步时将重试，也可以手动添加"
        if manual:
            self.message.put(ret_msg, title="CookieCloud同步成功", role="system")
        logger.info(f"CookieCloud同步成功：{ret_msg}")
        return True, ret_msg

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
            domain = StringUtils.get_url_domain(domain)
        # 站点信息
        siteinfo = self.siteoper.get_by_domain(domain)
        if not siteinfo:
            logger.warn(f"未维护站点 {domain} 信息！")
            return
        # Cookie
        cookie = siteinfo.cookie
        # 索引器
        indexer = self.siteshelper.get_indexer(domain)
        if not indexer:
            logger.warn(f"站点 {domain} 索引器不存在！")
            return
        # 查询站点图标
        site_icon = self.siteiconoper.get_by_domain(domain)
        if not site_icon or not site_icon.base64:
            logger.info(f"开始缓存站点 {indexer.get('name')} 图标 ...")
            icon_url, icon_base64 = self.__parse_favicon(url=indexer.get("domain"),
                                                         cookie=cookie,
                                                         ua=settings.USER_AGENT)
            if icon_url:
                self.siteiconoper.update_icon(name=indexer.get("name"),
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
        domain_host = StringUtils.get_url_host(domain)
        # 查询以"site.domain_host"开头的配置项，并清除
        site_keys = self.systemconfig.all().keys()
        for key in site_keys:
            if key.startswith(f"site.{domain_host}"):
                logger.info(f"清理站点配置：{key}")
                self.systemconfig.delete(key)

    def test(self, url: str) -> Tuple[bool, str]:
        """
        测试站点是否可用
        :param url: 站点域名
        :return: (是否可用, 错误信息)
        """
        # 检查域名是否可用
        domain = StringUtils.get_url_domain(url)
        site_info = self.siteoper.get_by_domain(domain)
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
                self.sitestatistic.success(domain=domain, seconds=seconds)
            else:
                self.sitestatistic.fail(domain)
            return state, message
        except Exception as e:
            return False, f"{str(e)}！"

    @staticmethod
    def __test(site_info: Site) -> Tuple[bool, str]:
        """
        通用站点测试
        """
        site_url = site_info.url
        site_cookie = site_info.cookie
        ua = site_info.ua or settings.USER_AGENT
        render = site_info.render
        public = site_info.public
        proxies = settings.PROXY if site_info.proxy else None
        proxy_server = settings.PROXY_SERVER if site_info.proxy else None

        # 访问链接
        if render:
            page_source = PlaywrightHelper().get_page_source(url=site_url,
                                                             cookies=site_cookie,
                                                             ua=ua,
                                                             proxies=proxy_server)
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
                if not public and not SiteUtils.is_logged_in(res.text):
                    if under_challenge(res.text):
                        msg = "站点被Cloudflare防护，请打开站点浏览器仿真"
                    elif res.status_code == 200:
                        msg = "Cookie已失效"
                    else:
                        msg = f"状态码：{res.status_code}"
                    return False, f"{msg}！"
                elif public and res.status_code != 200:
                    return False, f"状态码：{res.status_code}！"
            elif res is not None:
                return False, f"状态码：{res.status_code}！"
            else:
                return False, f"无法打开网站！"
        return True, "连接成功"

    def remote_list(self, channel: MessageChannel, userid: Union[str, int] = None):
        """
        查询所有站点，发送消息
        """
        site_list = self.siteoper.list()
        if not site_list:
            self.post_message(Notification(
                channel=channel,
                title="没有维护任何站点信息！",
                userid=userid,
                link=settings.MP_DOMAIN('#/site')))
        title = f"共有 {len(site_list)} 个站点，回复对应指令操作：" \
                f"\n- 禁用站点：/site_disable [id]" \
                f"\n- 启用站点：/site_enable [id]" \
                f"\n- 更新站点Cookie：/site_cookie [id] [username] [password] [2fa_code/secret]"
        messages = []
        for site in site_list:
            if site.render:
                render_str = "🧭"
            else:
                render_str = ""
            if site.is_active:
                messages.append(f"{site.id}. {site.name} {render_str}")
            else:
                messages.append(f"{site.id}. {site.name} ⚠️")
        # 发送列表
        self.post_message(Notification(
            channel=channel,
            title=title, text="\n".join(messages), userid=userid,
            link=settings.MP_DOMAIN('#/site')))

    def remote_disable(self, arg_str, channel: MessageChannel, userid: Union[str, int] = None):
        """
        禁用站点
        """
        if not arg_str:
            return
        arg_str = str(arg_str).strip()
        if not arg_str.isdigit():
            return
        site_id = int(arg_str)
        site = self.siteoper.get(site_id)
        if not site:
            self.post_message(Notification(
                channel=channel,
                title=f"站点编号 {site_id} 不存在！",
                userid=userid))
            return
        # 禁用站点
        self.siteoper.update(site_id, {
            "is_active": False
        })
        # 重新发送消息
        self.remote_list(channel, userid)

    def remote_enable(self, arg_str, channel: MessageChannel, userid: Union[str, int] = None):
        """
        启用站点
        """
        if not arg_str:
            return
        arg_strs = str(arg_str).split()
        for arg_str in arg_strs:
            arg_str = arg_str.strip()
            if not arg_str.isdigit():
                continue
            site_id = int(arg_str)
            site = self.siteoper.get(site_id)
            if not site:
                self.post_message(Notification(
                    channel=channel,
                    title=f"站点编号 {site_id} 不存在！", userid=userid))
                return
            # 禁用站点
            self.siteoper.update(site_id, {
                "is_active": True
            })
        # 重新发送消息
        self.remote_list(channel, userid)

    def update_cookie(self, site_info: Site,
                      username: str, password: str, two_step_code: str = None) -> Tuple[bool, str]:
        """
        根据用户名密码更新站点Cookie
        :param site_info: 站点信息
        :param username: 用户名
        :param password: 密码
        :param two_step_code: 二步验证码或密钥
        :return: (是否成功, 错误信息)
        """
        # 更新站点Cookie
        result = self.cookiehelper.get_site_cookie_ua(
            url=site_info.url,
            username=username,
            password=password,
            two_step_code=two_step_code,
            proxies=settings.PROXY_HOST if site_info.proxy else None
        )
        if result:
            cookie, ua, msg = result
            if not cookie:
                return False, msg
            self.siteoper.update(site_info.id, {
                "cookie": cookie,
                "ua": ua
            })
            return True, msg
        return False, "未知错误"

    def remote_cookie(self, arg_str: str, channel: MessageChannel, userid: Union[str, int] = None):
        """
        使用用户名密码更新站点Cookie
        """
        err_title = "请输入正确的命令格式：/site_cookie [id] [username] [password] [2fa_code/secret]，" \
                    "[id]为站点编号，[uername]为站点用户名，[password]为站点密码，[2fa_code/secret]为站点二步验证码或密钥"
        if not arg_str:
            self.post_message(Notification(
                channel=channel,
                title=err_title, userid=userid))
            return
        arg_str = str(arg_str).strip()
        args = arg_str.split()
        # 二步验证码
        two_step_code = None
        if len(args) == 4:
            two_step_code = args[3]
        elif len(args) != 3:
            self.post_message(Notification(
                channel=channel,
                title=err_title, userid=userid))
            return
        site_id = args[0]
        if not site_id.isdigit():
            self.post_message(Notification(
                channel=channel,
                title=err_title, userid=userid))
            return
        # 站点ID
        site_id = int(site_id)
        # 站点信息
        site_info = self.siteoper.get(site_id)
        if not site_info:
            self.post_message(Notification(
                channel=channel,
                title=f"站点编号 {site_id} 不存在！", userid=userid))
            return
        self.post_message(Notification(
            channel=channel,
            title=f"开始更新【{site_info.name}】Cookie&UA ...", userid=userid))
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
            self.post_message(Notification(
                channel=channel,
                title=f"【{site_info.name}】 Cookie&UA更新失败！",
                text=f"错误原因：{msg}",
                userid=userid))
        else:
            self.post_message(Notification(
                channel=channel,
                title=f"【{site_info.name}】 Cookie&UA更新成功",
                userid=userid))
