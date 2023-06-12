import base64
from typing import Tuple, Optional
from urllib.parse import urljoin

from lxml import etree

from app.chain import ChainBase
from app.core.config import settings
from app.db.sites import Sites
from app.helper.cookiecloud import CookieCloudHelper
from app.helper.sites import SitesHelper
from app.log import logger
from app.utils.http import RequestUtils


class CookieCloudChain(ChainBase):
    """
    同步站点Cookie
    """

    def __init__(self):
        super().__init__()
        self.sites = Sites()
        self.siteshelper = SitesHelper()
        self.cookiecloud = CookieCloudHelper(
            server=settings.COOKIECLOUD_HOST,
            key=settings.COOKIECLOUD_KEY,
            password=settings.COOKIECLOUD_PASSWORD
        )

    def process(self) -> Tuple[bool, str]:
        """
        通过CookieCloud同步站点Cookie
        """
        logger.info("开始同步CookieCloud站点 ...")
        cookies, msg = self.cookiecloud.download()
        if not cookies:
            logger.error(f"CookieCloud同步失败：{msg}")
            return False, msg
        # 保存Cookie或新增站点
        _update_count = 0
        _add_count = 0
        for domain, cookie in cookies.items():
            # 获取站点信息
            indexer = self.siteshelper.get_indexer(domain)
            if self.sites.exists(domain):
                # 更新站点Cookie
                self.sites.update_cookie(domain=domain, cookies=cookie)
                _update_count += 1
            elif indexer:
                # 新增站点
                self.sites.add(name=indexer.get("name"),
                               url=indexer.get("domain"),
                               domain=domain,
                               cookie=cookie)
                _add_count += 1
            # 保存站点图标
            if indexer:
                icon_url, icon_base64 = self.__parse_favicon(url=indexer.get("domain"),
                                                             cookie=cookie,
                                                             ua=settings.USER_AGENT)
                if icon_url:
                    self.sites.update_icon(name=indexer.get("name"),
                                           domain=domain,
                                           icon_url=icon_url,
                                           icon_base64=icon_base64)
        # 处理完成
        ret_msg = f"更新了{_update_count}个站点，新增了{_add_count}个站点"
        logger.info(f"CookieCloud同步成功：{ret_msg}")
        return True, ret_msg

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
