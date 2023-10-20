import json
import time
from typing import Tuple

from lxml import etree
from ruamel.yaml import CommentedMap

from app.core.config import settings
from app.helper.ocr import OcrHelper
from app.log import logger
from app.plugins.autosignin.sites import _ISiteSigninHandler
from app.utils.http import RequestUtils
from app.utils.string import StringUtils


class Opencd(_ISiteSigninHandler):
    """
    皇后ocr签到
    """
    # 匹配的站点Url，每一个实现类都需要设置为自己的站点Url
    site_url = "open.cd"

    # 已签到
    _repeat_text = "/plugin_sign-in.php?cmd=show-log"

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

        # 判断今日是否已签到
        html_text = self.get_page_source(url='https://www.open.cd',
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

        if self._repeat_text in html_text:
            logger.info(f"{site} 今日已签到")
            return True, '今日已签到'

        # 获取签到参数
        html_text = self.get_page_source(url='https://www.open.cd/plugin_sign-in.php',
                                         cookie=site_cookie,
                                         ua=ua,
                                         proxy=proxy,
                                         render=render)
        if not html_text:
            logger.error(f"{site} 签到失败，请检查站点连通性")
            return False, '签到失败，请检查站点连通性'

        # 没有签到则解析html
        html = etree.HTML(html_text)
        if not html:
            return False, '签到失败'

        # 签到参数
        img_url = html.xpath('//form[@id="frmSignin"]//img/@src')[0]
        img_hash = html.xpath('//form[@id="frmSignin"]//input[@name="imagehash"]/@value')[0]
        if not img_url or not img_hash:
            logger.error(f"{site} 签到失败，获取签到参数失败")
            return False, '签到失败，获取签到参数失败'

        # 完整验证码url
        img_get_url = 'https://www.open.cd/%s' % img_url
        logger.debug(f"{site} 获取到{site}验证码链接 {img_get_url}")

        # ocr识别多次，获取6位验证码
        times = 0
        ocr_result = None
        # 识别几次
        while times <= 3:
            # ocr二维码识别
            ocr_result = OcrHelper().get_captcha_text(image_url=img_get_url,
                                                      cookie=site_cookie,
                                                      ua=ua)
            logger.debug(f"ocr识别{site}验证码 {ocr_result}")
            if ocr_result:
                if len(ocr_result) == 6:
                    logger.info(f"ocr识别{site}验证码成功 {ocr_result}")
                    break
            times += 1
            logger.debug(f"ocr识别{site}验证码失败，正在进行重试，目前重试次数 {times}")
            time.sleep(1)

        if ocr_result:
            # 组装请求参数
            data = {
                'imagehash': img_hash,
                'imagestring': ocr_result
            }
            # 访问签到链接
            sign_res = RequestUtils(cookies=site_cookie,
                                    ua=ua,
                                    proxies=settings.PROXY if proxy else None
                                    ).post_res(url='https://www.open.cd/plugin_sign-in.php?cmd=signin', data=data)
            if sign_res and sign_res.status_code == 200:
                logger.debug(f"sign_res返回 {sign_res.text}")
                # sign_res.text = '{"state":"success","signindays":"0","integral":"10"}'
                sign_dict = json.loads(sign_res.text)
                if sign_dict['state']:
                    logger.info(f"{site} 签到成功")
                    return True, '签到成功'
                else:
                    logger.error(f"{site} 签到失败，签到接口返回 {sign_dict}")
                    return False, '签到失败'

        logger.error(f'{site} 签到失败：未获取到验证码')
        return False, '签到失败：未获取到验证码'
