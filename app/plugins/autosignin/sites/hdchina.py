import json
from typing import Tuple

from lxml import etree
from ruamel.yaml import CommentedMap

from app.core.config import settings
from app.log import logger
from app.plugins.autosignin.sites import _ISiteSigninHandler
from app.utils.http import RequestUtils
from app.utils.string import StringUtils


class HDChina(_ISiteSigninHandler):
    """
    瓷器签到
    """
    # 匹配的站点Url，每一个实现类都需要设置为自己的站点Url
    site_url = "hdchina.org"

    # 已签到
    _sign_regex = ['<a class="label label-default" href="#">已签到</a>']

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
        proxies = settings.PROXY if site_info.get("proxy") else None

        # 尝试解决瓷器cookie每天签到后过期,只保留hdchina=部分
        cookie = ""
        # 按照分号进行字符串拆分
        sub_strs = site_cookie.split(";")
        # 遍历每个子字符串
        for sub_str in sub_strs:
            if "hdchina=" in sub_str:
                # 如果子字符串包含"hdchina="，则保留该子字符串
                cookie += sub_str + ";"

        if "hdchina=" not in cookie:
            logger.error(f"{site} 签到失败，Cookie已失效")
            return False, '签到失败，Cookie已失效'

        site_cookie = cookie
        # 获取页面html
        html_res = RequestUtils(cookies=site_cookie,
                                ua=ua,
                                proxies=proxies
                                ).get_res(url="https://hdchina.org/index.php")
        if not html_res or html_res.status_code != 200:
            logger.error(f"{site} 签到失败，请检查站点连通性")
            return False, '签到失败，请检查站点连通性'

        if "login.php" in html_res.text or "阻断页面" in html_res.text:
            logger.error(f"{site} 签到失败，Cookie失效")
            return False, '签到失败，Cookie失效'

        # 获取新返回的cookie进行签到
        site_cookie = ';'.join(['{}={}'.format(k, v) for k, v in html_res.cookies.get_dict().items()])

        # 判断是否已签到
        html_res.encoding = "utf-8"
        sign_status = self.sign_in_result(html_res=html_res.text,
                                          regexs=self._sign_regex)
        if sign_status:
            logger.info(f"{site} 今日已签到")
            return True, '今日已签到'

        # 没有签到则解析html
        html = etree.HTML(html_res.text)

        if not html:
            return False, '签到失败'

        # x_csrf
        x_csrf = html.xpath("//meta[@name='x-csrf']/@content")[0]
        if not x_csrf:
            logger.error("{site} 签到失败，获取x-csrf失败")
            return False, '签到失败'
        logger.debug(f"获取到x-csrf {x_csrf}")

        # 签到
        data = {
            'csrf': x_csrf
        }
        sign_res = RequestUtils(cookies=site_cookie,
                                ua=ua,
                                proxies=proxies
                                ).post_res(url="https://hdchina.org/plugin_sign-in.php?cmd=signin", data=data)
        if not sign_res or sign_res.status_code != 200:
            logger.error(f"{site} 签到失败，签到接口请求失败")
            return False, '签到失败，签到接口请求失败'

        sign_dict = json.loads(sign_res.text)
        logger.debug(f"签到返回结果 {sign_dict}")
        if sign_dict['state']:
            # {'state': 'success', 'signindays': 10, 'integral': 20}
            logger.info(f"{site} 签到成功")
            return True, '签到成功'
        else:
            # {'state': False, 'msg': '不正确的CSRF / Incorrect CSRF token'}
            logger.error(f"{site} 签到失败，不正确的CSRF / Incorrect CSRF token")
            return False, '签到失败'
