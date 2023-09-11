import base64
from typing import Tuple, Optional, Union
from urllib.parse import urljoin

from lxml import etree
from sqlalchemy.orm import Session

from app.chain import ChainBase
from app.chain.site import SiteChain
from app.core.config import settings
from app.db.site_oper import SiteOper
from app.db.siteicon_oper import SiteIconOper
from app.helper.browser import PlaywrightHelper
from app.helper.cloudflare import under_challenge
from app.helper.cookiecloud import CookieCloudHelper
from app.helper.message import MessageHelper
from app.helper.sites import SitesHelper
from app.log import logger
from app.schemas import Notification, NotificationType, MessageChannel
from app.utils.http import RequestUtils
from app.utils.site import SiteUtils


class CookieCloudChain(ChainBase):
    """
    CookieCloud处理链
    """

    def __init__(self, db: Session = None):
        super().__init__(db)
        self.siteoper = SiteOper(self._db)
        self.siteiconoper = SiteIconOper(self._db)
        self.siteshelper = SitesHelper()
        self.sitechain = SiteChain(self._db)
        self.message = MessageHelper()
        self.cookiecloud = CookieCloudHelper(
            server=settings.COOKIECLOUD_HOST,
            key=settings.COOKIECLOUD_KEY,
            password=settings.COOKIECLOUD_PASSWORD
        )

    def remote_sync(self, channel: MessageChannel, userid: Union[int, str]):
        """
        远程触发同步站点，发送消息
        """
        self.post_message(Notification(channel=channel, mtype=NotificationType.SiteMessage,
                                       title="开始同步CookieCloud站点 ...", userid=userid))
        # 开始同步
        success, msg = self.process()
        if success:
            self.post_message(Notification(channel=channel, mtype=NotificationType.SiteMessage,
                                           title=f"同步站点成功，{msg}", userid=userid))
        else:
            self.post_message(Notification(channel=channel, mtype=NotificationType.SiteMessage,
                                           title=f"同步站点失败：{msg}", userid=userid))

    def process(self, manual=False) -> Tuple[bool, str]:
        """
        通过CookieCloud同步站点Cookie
        """
        logger.info("开始同步CookieCloud站点 ...")
        cookies, msg = self.cookiecloud.download()
        if not cookies:
            logger.error(f"CookieCloud同步失败：{msg}")
            if manual:
                self.message.put(f"CookieCloud同步失败： {msg}")
            return False, msg
        # 保存Cookie或新增站点
        _update_count = 0
        _add_count = 0
        _fail_count = 0
        for domain, cookie in cookies.items():
            # 获取站点信息
            indexer = self.siteshelper.get_indexer(domain)
            site_info = self.siteoper.get_by_domain(domain)
            if site_info:
                # 检查站点连通性
                status, msg = self.sitechain.test(domain)

                # 更新站点Cookie
                if status:
                    logger.info(f"站点【{site_info.name}】连通性正常，不同步CookieCloud数据")
                    if not site_info.public and not site_info.rss:
                        # 自动生成rss地址
                        rss_url = self.__get_rss(url=site_info.url, cookie=cookie, ua=settings.USER_AGENT,
                                                 proxy=site_info.proxy)
                        # 更新站点rss地址
                        self.siteoper.update_rss(domain=domain, rss=rss_url)
                    continue
                # 更新站点Cookie
                self.siteoper.update_cookie(domain=domain, cookies=cookie)
                _update_count += 1
            elif indexer:
                # 新增站点
                res = RequestUtils(cookies=cookie,
                                   ua=settings.USER_AGENT
                                   ).get_res(url=indexer.get("domain"))
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
                rss_url = None
                if not indexer.get("public") and indexer.get("domain"):
                    # 自动生成rss地址
                    rss_url = self.__get_rss(url=indexer.get("domain"), cookie=cookie, ua=settings.USER_AGENT)
                self.siteoper.add(name=indexer.get("name"),
                                  url=indexer.get("domain"),
                                  domain=domain,
                                  cookie=cookie,
                                  rss=rss_url,
                                  public=1 if indexer.get("public") else 0)
                _add_count += 1
            # 保存站点图标
            if indexer:
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
        # 处理完成
        ret_msg = f"更新了{_update_count}个站点，新增了{_add_count}个站点"
        if _fail_count > 0:
            ret_msg += f"，{_fail_count}个站点添加失败，下次同步时将重试，也可以手动添加"
        if manual:
            self.message.put(f"CookieCloud同步成功, {ret_msg}")
        logger.info(f"CookieCloud同步成功：{ret_msg}")
        return True, ret_msg

    def __get_rss(self, url: str, cookie: str, ua: str, proxy: int) -> str:
        """
        获取站点rss地址
        """
        if "ourbits.club" in url:
            return self.__get_rss_ourbits(url=url, cookie=cookie, ua=ua, proxy=proxy)
        if "totheglory.im" in url:
            return self.__get_rss_ttg(url=url, cookie=cookie, ua=ua, proxy=proxy)
        if "monikadesign.uk" in url:
            return self.__get_rss_monika(url=url, cookie=cookie, ua=ua, proxy=proxy)
        if "zhuque.in" in url:
            return self.__get_rss_zhuque(url=url, cookie=cookie, ua=ua, proxy=proxy)

        xpath = "//a[@class='faqlink']/@href"
        if "club.hares.top" in url:
            xpath = "//*[@id='layui-layer100001']/div[2]/div/p[4]/a/@href"
        if "et8.org" in url:
            xpath = "//*[@id='outer']/table/tbody/tr/td/table/tbody/tr/td/a[2]/@href"
        if "pttime.org" in url:
            xpath = "//*[@id='outer']/table/tbody/tr/td/table/tbody/tr/td/text()[5]"

        return self.__get_rss_base(url=url, cookie=cookie, ua=ua, xpath=xpath, proxy=proxy)

    def __get_rss_base(self, url: str, cookie: str, ua: str, xpath: str, proxy: int) -> str:
        """
        默认获取站点rss地址
        """
        try:
            get_rss_url = urljoin(url, "getrss.php")
            rss_data = self.__get_rss_data(url)
            res = RequestUtils(cookies=cookie,
                               timeout=60,
                               ua=ua,
                               proxies=settings.PROXY if proxy else None).post_res(
                url=get_rss_url, data=rss_data)
            if res:
                html_text = res.text
            else:
                logger.error(f"获取rss失败：{url}")
                return ""
            html = etree.HTML(html_text)
            if html:
                rss_link = html.xpath(xpath)
                if rss_link:
                    return str(rss_link[-1])
            return ""
        except Exception as e:
            print(str(e))
            return ""

    def __get_rss_ttg(self, url: str, cookie: str, ua: str, proxy: int) -> str:
        """
        获取ttg rss地址
        """
        try:
            get_rss_url = urljoin(url,
                                  "rsstools.php?c51=51&c52=52&c53=53&c54=54&c108=108&c109=109&c62=62&c63=63&c67=67&c69=69&c70=70&c73=73&c76=76&c75=75&c74=74&c87=87&c88=88&c99=99&c90=90&c58=58&c103=103&c101=101&c60=60")
            res = RequestUtils(cookies=cookie,
                               timeout=60,
                               ua=ua,
                               proxies=settings.PROXY if proxy else None).get_res(url=get_rss_url)
            if res:
                html_text = res.text
            else:
                logger.error(f"获取rss失败：{url}")
                return ""
            html = etree.HTML(html_text)
            if html:
                rss_link = html.xpath("//textarea/text()")
                if rss_link:
                    return str(rss_link[-1])
            return ""
        except Exception as e:
            print(str(e))
            return ""

    def __get_rss_monika(self, url: str, cookie: str, ua: str, proxy: int) -> str:
        """
        获取monikadesign rss地址
        """
        try:
            get_rss_url = urljoin(url, "rss")
            res = RequestUtils(cookies=cookie,
                               timeout=60,
                               ua=ua,
                               proxies=settings.PROXY if proxy else None).get_res(url=get_rss_url)
            if res:
                html_text = res.text
            else:
                logger.error(f"获取rss失败：{url}")
                return ""
            html = etree.HTML(html_text)
            if html:
                rss_link = html.xpath("//a/@href")
                if rss_link:
                    return str(rss_link[0])
            return ""
        except Exception as e:
            print(str(e))
            return ""

    def __get_rss_ourbits(self, url: str, cookie: str, ua: str, proxy: int) -> str:
        """
        获取我堡rss地址
        """
        try:
            get_rss_url = urljoin(url, "getrss.php")
            html_text = PlaywrightHelper().get_page_source(url=get_rss_url,
                                                           cookies=cookie,
                                                           ua=ua,
                                                           proxies=settings.PROXY if proxy else None)
            if html_text:
                html = etree.HTML(html_text)
                if html:
                    rss_link = html.xpath("//a[@class='gen_rsslink']/@href")
                    if rss_link:
                        return str(rss_link[-1])
            return ""
        except Exception as e:
            print(str(e))
            return ""

    def __get_rss_zhuque(self, url: str, cookie: str, ua: str, proxy: int) -> str:
        """
        获取zhuque rss地址
        """
        try:
            get_rss_url = urljoin(url, "user/rss")
            html_text = PlaywrightHelper().get_page_source(url=get_rss_url,
                                                           cookies=cookie,
                                                           ua=ua,
                                                           proxies=settings.PROXY if proxy else None)
            if html_text:
                html = etree.HTML(html_text)
                if html:
                    rss_link = html.xpath("//a/@href")
                    if rss_link:
                        return str(rss_link[-1])
            return ""
        except Exception as e:
            print(str(e))
            return ""

    @staticmethod
    def __get_rss_data(url: str) -> dict:
        """
        获取请求rss的参数，有的站不太一样，后续不断维护
        """
        _rss_data = {
            "inclbookmarked": 0,
            "itemsmalldescr": 1,
            "showrows": 50,
            "search_mode": 1,
        }

        if 'hdchina.org' in url:
            # 显示下载框	0全部 1仅下载框
            _rss_data['rsscart'] = 0

        if 'audiences.me' in url:
            # 种子类型 1新种与重置顶旧种 0只包含新种
            _rss_data['torrent_type'] = 1
            # RSS链接有效期： 180天
            _rss_data['exp'] = 180

        if 'shadowflow.org' in url:
            # 下载需扣除魔力 0不需要 1需要 2全部
            _rss_data['paid'] = 0
            _rss_data['search_mode'] = 0
            _rss_data['showrows'] = 30

        if 'hddolby.com' in url:
            # RSS链接有效期： 180天
            _rss_data['exp'] = 180

        if 'hdhome.org' in url:
            # RSS链接有效期： 180天
            _rss_data['exp'] = 180

        if 'pthome.net' in url:
            # RSS链接有效期： 180天
            _rss_data['exp'] = 180

        if 'ptsbao.club' in url:
            _rss_data['size'] = 0

        if 'leaves.red' in url:
            # 下载需扣除魔力 0不需要 1需要 2全部
            _rss_data['paid'] = 2
            _rss_data['search_mode'] = 0

        if 'hdtime.org' in url:
            _rss_data['search_mode'] = 0

        if 'kp.m-team.cc' in url:
            _rss_data = {
                "showrows": 50,
                "inclbookmarked": 0,
                "itemsmalldescr": 1,
                "https": 1
            }

        if 'u2.dmhy.org' in url:
            # 显示自动通过的种子 0不显示自动通过的种子 1全部
            _rss_data['inclautochecked'] = 1
            # Tracker SSL 0不使用SSL 1使用SSL
            _rss_data['trackerssl'] = 1

        if 'www.pttime.org' in url:
            _rss_data = {
                "showrows": 10,
                "inclbookmarked": 0,
                "itemsmalldescr": 1
            }

        return _rss_data

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
        res = RequestUtils(cookies=cookie, timeout=60, ua=ua).get_res(url=url)
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

        res = RequestUtils(cookies=cookie, timeout=20, ua=ua).get_res(url=favicon_url)
        if res:
            return favicon_url, base64.b64encode(res.content).decode()
        else:
            logger.error(f"获取站点图标失败：{favicon_url}")
        return favicon_url, None
