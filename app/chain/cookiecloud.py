import base64
from typing import Tuple, Optional, Union
from urllib.parse import urljoin

from lxml import etree
from sqlalchemy.orm import Session

from app.chain import ChainBase
from app.chain.site import SiteChain
from app.core.config import settings
from app.db.siteicon_oper import SiteIconOper
from app.db.site_oper import SiteOper
from app.helper.cookiecloud import CookieCloudHelper
from app.helper.message import MessageHelper
from app.helper.sites import SitesHelper
from app.log import logger
from app.schemas import Notification, NotificationType, MessageChannel
from app.utils.http import RequestUtils


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
        for domain, cookie in cookies.items():
            # 获取站点信息
            indexer = self.siteshelper.get_indexer(domain)
            # 检查站点连通性
            status, msg = self.sitechain.test(domain)
            if self.siteoper.exists(domain):
                # 更新站点Cookie
                if status:
                    logger.info(f"站点【{indexer.get('name')}】连通性正常，不同步CookieCloud数据")
                    continue
                # 更新站点Cookie
                self.siteoper.update_cookie(domain=domain, cookies=cookie)
                _update_count += 1
            elif indexer:
                # 新增站点
                if not status:
                    logger.warn(f"站点【{indexer.get('name')}】无法登录，"
                                f"可能原因：没有该站点账号/站点处于关闭状态/Cookie已失效，暂不新增站点，"
                                f"下次同步将偿试重新添加，也可手动添加该站点")
                    continue
                self.siteoper.add(name=indexer.get("name"),
                                  url=indexer.get("domain"),
                                  domain=domain,
                                  cookie=cookie,
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
        if manual:
            self.message.put(f"CookieCloud同步成功, {ret_msg}")
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
