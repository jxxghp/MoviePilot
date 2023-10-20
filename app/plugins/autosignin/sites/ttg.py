import re
from typing import Tuple

from ruamel.yaml import CommentedMap

from app.core.config import settings
from app.log import logger
from app.plugins.autosignin.sites import _ISiteSigninHandler
from app.utils.http import RequestUtils
from app.utils.string import StringUtils


class TTG(_ISiteSigninHandler):
    """
    TTG签到
    """
    # 匹配的站点Url，每一个实现类都需要设置为自己的站点Url
    site_url = "totheglory.im"

    # 已签到
    _sign_regex = ['<b style="color:green;">已签到</b>']
    _sign_text = '亲，您今天已签到过，不要太贪哦'

    # 签到成功
    _success_text = '您已连续签到'

    @classmethod
    def match(cls, url: str) -> bool:
        """
        根据站点Url判断是否匹配当前站点签到类，大部分情况使用默认实现即可
        :param url: 站点Url
        :return: 是否匹配，如匹配则会调用该类的signin方法
        """
        return True if StringUtils.url_equal(url, cls.site_url) else False

    def signin(self, site_info: CommentedMap) -> Tuple[bool, str]:
        """
        执行签到操作
        :param site_info: 站点信息，含有站点Url、站点Cookie、UA等信息
        :return: 签到结果信息
        """
        site = site_info.get("name")
        site_cookie = site_info.get("cookie")
        ua = site_info.get("ua")
        proxy = site_info.get("proxy")
        render = site_info.get("render")

        # 获取页面html
        html_text = self.get_page_source(url="https://totheglory.im",
                                         cookie=site_cookie,
                                         ua=ua,
                                         proxy=proxy,
                                         render=render)
        if not html_text:
            logger.error(f"{site} 签到失败，请检查站点连通性")
            return False, '签到失败，请检查站点连通性'

        if "login.php" in html_text:
            logger.error(f"{site} 签到失败，Cookie已失效")
            return False, '签到失败，Cookie已失效'

        # 判断是否已签到
        sign_status = self.sign_in_result(html_res=html_text,
                                          regexs=self._sign_regex)
        if sign_status:
            logger.info(f"{site} 今日已签到")
            return True, '今日已签到'

        # 获取签到参数
        signed_timestamp = re.search('(?<=signed_timestamp: ")\\d{10}', html_text).group()
        signed_token = re.search('(?<=signed_token: ").*(?=")', html_text).group()
        logger.debug(f"signed_timestamp={signed_timestamp} signed_token={signed_token}")

        data = {
            'signed_timestamp': signed_timestamp,
            'signed_token': signed_token
        }
        # 签到
        sign_res = RequestUtils(cookies=site_cookie,
                                ua=ua,
                                proxies=settings.PROXY if proxy else None
                                ).post_res(url="https://totheglory.im/signed.php",
                                           data=data)
        if not sign_res or sign_res.status_code != 200:
            logger.error(f"{site} 签到失败，签到接口请求失败")
            return False, '签到失败，签到接口请求失败'

        sign_res.encoding = "utf-8"
        if self._success_text in sign_res.text:
            logger.info(f"{site} 签到成功")
            return True, '签到成功'
        if self._sign_text in sign_res.text:
            logger.info(f"{site} 今日已签到")
            return True, '今日已签到'

        logger.error(f"{site} 签到失败，未知原因")
        return False, '签到失败，未知原因'
