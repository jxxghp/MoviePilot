import base64
from typing import Tuple, Optional

from lxml import etree
from playwright.sync_api import Page

from app.helper.browser import PlaywrightHelper
from app.helper.ocr import OcrHelper
from app.helper.twofa import TwoFactorAuth
from app.log import logger
from app.utils.http import RequestUtils
from app.utils.site import SiteUtils
from app.utils.string import StringUtils


class CookieHelper:
    # 站点登录界面元素XPATH
    _SITE_LOGIN_XPATH = {
        "username": [
            '//input[@name="username"]',
            '//input[@id="form_item_username"]',
            '//input[@id="username"]'
        ],
        "password": [
            '//input[@name="password"]',
            '//input[@id="form_item_password"]',
            '//input[@id="password"]',
            '//input[@type="password"]'
        ],
        "captcha": [
            '//input[@name="imagestring"]',
            '//input[@name="captcha"]',
            '//input[@id="form_item_captcha"]',
            '//input[@placeholder="驗證碼"]'
        ],
        "captcha_img": [
            '//img[@alt="captcha"]/@src',
            '//img[@alt="CAPTCHA"]/@src',
            '//img[@alt="SECURITY CODE"]/@src',
            '//img[@id="LAY-user-get-vercode"]/@src',
            '//img[contains(@src,"/api/getCaptcha")]/@src'
        ],
        "submit": [
            '//input[@type="submit"]',
            '//button[@type="submit"]',
            '//button[@lay-filter="login"]',
            '//button[@lay-filter="formLogin"]',
            '//input[@type="button"][@value="登录"]'
        ],
        "error": [
            "//table[@class='main']//td[@class='text']/text()"
        ],
        "twostep": [
            '//input[@name="two_step_code"]',
            '//input[@name="2fa_secret"]',
            '//input[@name="otp"]'
        ]
    }

    @staticmethod
    def parse_cookies(cookies: list) -> str:
        """
        将浏览器返回的cookies转化为字符串
        """
        if not cookies:
            return ""
        cookie_str = ""
        for cookie in cookies:
            cookie_str += f"{cookie['name']}={cookie['value']}; "
        return cookie_str

    def get_site_cookie_ua(self,
                           url: str,
                           username: str,
                           password: str,
                           two_step_code: str = None,
                           proxies: dict = None) -> Tuple[Optional[str], Optional[str], str]:
        """
        获取站点cookie和ua
        :param url: 站点地址
        :param username: 用户名
        :param password: 密码
        :param two_step_code: 二步验证码或密钥
        :param proxies: 代理
        :return: cookie、ua、message
        """

        def __page_handler(page: Page) -> Tuple[Optional[str], Optional[str], str]:
            """
            页面处理
            :return: Cookie和UA
            """
            # 登录页面代码
            html_text = page.content()
            if not html_text:
                return None, None, "获取源码失败"
            # 查找用户名输入框
            html = etree.HTML(html_text)
            username_xpath = None
            for xpath in self._SITE_LOGIN_XPATH.get("username"):
                if html.xpath(xpath):
                    username_xpath = xpath
                    break
            if not username_xpath:
                return None, None, "未找到用户名输入框"
            # 查找密码输入框
            password_xpath = None
            for xpath in self._SITE_LOGIN_XPATH.get("password"):
                if html.xpath(xpath):
                    password_xpath = xpath
                    break
            if not password_xpath:
                return None, None, "未找到密码输入框"
            # 处理二步验证码
            otp_code = TwoFactorAuth(two_step_code).get_code()
            # 查找二步验证码输入框
            twostep_xpath = None
            if otp_code:
                for xpath in self._SITE_LOGIN_XPATH.get("twostep"):
                    if html.xpath(xpath):
                        twostep_xpath = xpath
                        break
            # 查找验证码输入框
            captcha_xpath = None
            for xpath in self._SITE_LOGIN_XPATH.get("captcha"):
                if html.xpath(xpath):
                    captcha_xpath = xpath
                    break
            # 查找验证码图片
            captcha_img_url = None
            if captcha_xpath:
                for xpath in self._SITE_LOGIN_XPATH.get("captcha_img"):
                    if html.xpath(xpath):
                        captcha_img_url = html.xpath(xpath)[0]
                        break
                if not captcha_img_url:
                    return None, None, "未找到验证码图片"
            # 查找登录按钮
            submit_xpath = None
            for xpath in self._SITE_LOGIN_XPATH.get("submit"):
                if html.xpath(xpath):
                    submit_xpath = xpath
                    break
            if not submit_xpath:
                return None, None, "未找到登录按钮"
            # 点击登录按钮
            try:
                # 等待登录按钮准备好
                page.wait_for_selector(submit_xpath)
                # 输入用户名
                page.fill(username_xpath, username)
                # 输入密码
                page.fill(password_xpath, password)
                # 输入二步验证码
                if twostep_xpath:
                    page.fill(twostep_xpath, otp_code)
                # 识别验证码
                if captcha_xpath and captcha_img_url:
                    captcha_element = page.query_selector(captcha_xpath)
                    if captcha_element.is_visible():
                        # 验证码图片地址
                        code_url = self.__get_captcha_url(url, captcha_img_url)
                        # 获取当前的cookie和ua
                        cookie = self.parse_cookies(page.context.cookies())
                        ua = page.evaluate("() => window.navigator.userAgent")
                        # 自动OCR识别验证码
                        captcha = self.__get_captcha_text(cookie=cookie, ua=ua, code_url=code_url)
                        if captcha:
                            logger.info("验证码地址为：%s，识别结果：%s" % (code_url, captcha))
                        else:
                            return None, None, "验证码识别失败"
                        # 输入验证码
                        captcha_element.fill(captcha)
                    else:
                        # 不可见元素不处理
                        pass
                # 点击登录按钮
                page.click(submit_xpath)
                page.wait_for_load_state("networkidle", timeout=30 * 1000)
            except Exception as e:
                logger.error(f"仿真登录失败：{str(e)}")
                return None, None, f"仿真登录失败：{str(e)}"
            # 对于某二次验证码为单页面的站点，输入二次验证码
            if "verify" in page.url:
                if not otp_code:
                    return None, None, "需要二次验证码"
                html = etree.HTML(page.content())
                for xpath in self._SITE_LOGIN_XPATH.get("twostep"):
                    if html.xpath(xpath):
                        try:
                            # 刷新一下 2fa code
                            otp_code = TwoFactorAuth(two_step_code).get_code()
                            page.fill(xpath, otp_code)
                            # 登录按钮 xpath 理论上相同，不再重复查找
                            page.click(submit_xpath)
                            page.wait_for_load_state("networkidle", timeout=30 * 1000)
                        except Exception as e:
                            logger.error(f"二次验证码输入失败：{str(e)}")
                            return None, None, f"二次验证码输入失败：{str(e)}"
                        break
            # 登录后的源码
            html_text = page.content()
            if not html_text:
                return None, None, "获取网页源码失败"
            if SiteUtils.is_logged_in(html_text):
                return self.parse_cookies(page.context.cookies()), \
                    page.evaluate("() => window.navigator.userAgent"), ""
            else:
                # 读取错误信息
                error_xpath = None
                for xpath in self._SITE_LOGIN_XPATH.get("error"):
                    if html.xpath(xpath):
                        error_xpath = xpath
                        break
                if not error_xpath:
                    return None, None, "登录失败"
                else:
                    error_msg = html.xpath(error_xpath)[0]
                    return None, None, error_msg

        if not url or not username or not password:
            return None, None, "参数错误"

        return PlaywrightHelper().action(url=url,
                                         callback=__page_handler,
                                         proxies=proxies)

    @staticmethod
    def __get_captcha_text(cookie: str, ua: str, code_url: str) -> str:
        """
        识别验证码图片的内容
        """
        if not code_url:
            return ""
        ret = RequestUtils(ua=ua, cookies=cookie).get_res(code_url)
        if ret:
            if not ret.content:
                return ""
            return OcrHelper().get_captcha_text(
                image_b64=base64.b64encode(ret.content).decode()
            )
        else:
            return ""

    @staticmethod
    def __get_captcha_url(siteurl: str, imageurl: str) -> str:
        """
        获取验证码图片的URL
        """
        if not siteurl or not imageurl:
            return ""
        if imageurl.startswith("/"):
            imageurl = imageurl[1:]
        return "%s/%s" % (StringUtils.get_base_url(siteurl), imageurl)
