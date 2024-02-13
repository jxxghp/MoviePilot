from typing import Tuple

from app.chain import ChainBase
from app.chain.site import SiteChain
from app.core.config import settings
from app.core.event import EventManager
from app.db.site_oper import SiteOper
from app.helper.cloudflare import under_challenge
from app.helper.cookiecloud import CookieCloudHelper
from app.helper.message import MessageHelper
from app.helper.rss import RssHelper
from app.helper.sites import SitesHelper
from app.log import logger
from app.schemas.types import EventType
from app.utils.http import RequestUtils
from app.utils.site import SiteUtils


class CookieCloudChain(ChainBase):
    """
    CookieCloud处理链
    """

    def __init__(self):
        super().__init__()
        self.siteoper = SiteOper()
        self.siteshelper = SitesHelper()
        self.rsshelper = RssHelper()
        self.sitechain = SiteChain()
        self.message = MessageHelper()
        self.cookiecloud = CookieCloudHelper(
            server=settings.COOKIECLOUD_HOST,
            key=settings.COOKIECLOUD_KEY,
            password=settings.COOKIECLOUD_PASSWORD
        )

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
                    # 更新站点rss地址
                    if not site_info.public and not site_info.rss:
                        # 自动生成rss地址
                        rss_url, errmsg = self.rsshelper.get_rss_link(
                            url=site_info.url,
                            cookie=cookie,
                            ua=settings.USER_AGENT,
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
                # 获取rss地址
                rss_url = None
                if not indexer.get("public") and indexer.get("domain"):
                    # 自动生成rss地址
                    rss_url, errmsg = self.rsshelper.get_rss_link(url=indexer.get("domain"),
                                                                  cookie=cookie,
                                                                  ua=settings.USER_AGENT)
                    if errmsg:
                        logger.warn(errmsg)
                # 插入数据库
                logger.info(f"新增站点 {indexer.get('name')} ...")
                self.siteoper.add(name=indexer.get("name"),
                                  url=indexer.get("domain"),
                                  domain=domain,
                                  cookie=cookie,
                                  rss=rss_url,
                                  public=1 if indexer.get("public") else 0)
                _add_count += 1

            # 通知缓存站点图标
            if indexer:
                EventManager().send_event(EventType.CacheSiteIcon, {
                    "domain": domain,
                })
        # 处理完成
        ret_msg = f"更新了{_update_count}个站点，新增了{_add_count}个站点"
        if _fail_count > 0:
            ret_msg += f"，{_fail_count}个站点添加失败，下次同步时将重试，也可以手动添加"
        if manual:
            self.message.put(f"CookieCloud同步成功, {ret_msg}")
        logger.info(f"CookieCloud同步成功：{ret_msg}")
        return True, ret_msg
