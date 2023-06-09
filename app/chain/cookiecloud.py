from typing import Tuple

from app.chain import ChainBase
from app.core.config import settings
from app.db.sites import Sites
from app.helper.cookiecloud import CookieCloudHelper
from app.helper.sites import SitesHelper
from app.log import logger


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
            if self.sites.exists(domain):
                # 更新站点Cookie
                self.sites.update_cookie(domain, cookie)
                _update_count += 1
            else:
                # 获取站点信息
                indexer = self.siteshelper.get_indexer(domain)
                if indexer:
                    # 新增站点
                    self.sites.add(name=indexer.get("name"),
                                   url=indexer.get("domain"),
                                   domain=domain,
                                   cookie=cookie)
                    _add_count += 1
        ret_msg = f"更新了{_update_count}个站点，新增了{_add_count}个站点"
        logger.info(f"CookieCloud同步成功：{ret_msg}")
        return True, ret_msg
