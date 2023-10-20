import json
from typing import Tuple

from lxml import etree
from ruamel.yaml import CommentedMap

from app.core.config import settings
from app.log import logger
from app.plugins.autosignin.sites import _ISiteSigninHandler
from app.utils.http import RequestUtils
from app.utils.string import StringUtils


class ZhuQue(_ISiteSigninHandler):
    """
    ZHUQUE签到
    """
    # 匹配的站点Url，每一个实现类都需要设置为自己的站点Url
    site_url = "zhuque.in"

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
        html_text = self.get_page_source(url="https://zhuque.in",
                                         cookie=site_cookie,
                                         ua=ua,
                                         proxy=proxy,
                                         render=render)
        if not html_text:
            logger.error(f"{site} 模拟登录失败，请检查站点连通性")
            return False, '模拟登录失败，请检查站点连通性'

        if "login.php" in html_text:
            logger.error(f"{site} 模拟登录失败，Cookie已失效")
            return False, '模拟登录失败，Cookie已失效'

        html = etree.HTML(html_text)

        if not html:
            return False, '模拟登录失败'

        # 释放技能
        msg = '失败'
        x_csrf_token = html.xpath("//meta[@name='x-csrf-token']/@content")[0]
        if x_csrf_token:
            data = {
                "all": 1,
                "resetModal": "true"
            }
            headers = {
                "x-csrf-token": str(x_csrf_token),
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": ua
            }
            skill_res = RequestUtils(cookies=site_cookie,
                                     headers=headers,
                                     proxies=settings.PROXY if proxy else None
                                     ).post_res(url="https://zhuque.in/api/gaming/fireGenshinCharacterMagic", json=data)
            if not skill_res or skill_res.status_code != 200:
                logger.error(f"模拟登录失败，释放技能失败")

            # '{"status":200,"data":{"code":"FIRE_GENSHIN_CHARACTER_MAGIC_SUCCESS","bonus":0}}'
            skill_dict = json.loads(skill_res.text)
            if skill_dict['status'] == 200:
                bonus = int(skill_dict['data']['bonus'])
                msg = f'成功，获得{bonus}魔力'

        logger.info(f'【{site}】模拟登录成功，技能释放{msg}')
        return True, f'模拟登录成功，技能释放{msg}'
