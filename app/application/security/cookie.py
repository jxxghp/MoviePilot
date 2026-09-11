import time
from typing import Any, Callable, Optional, Protocol, Tuple
from urllib.parse import urljoin, urlparse

from lxml import etree

from app.application.security.twofactor import TwoFactorAuth
from app.domain.site import SiteUtils
from app.runtime.log import logger

CookieResult = Tuple[Optional[str], Optional[str], str]


class CookieBrowserPort(Protocol):
    """声明站点登录用例所需的受控浏览器执行能力。"""

    def action(self, *, url: str, callback: Callable[[Any], CookieResult],
               proxies: Optional[dict[str, Any]], timeout: Optional[int]) -> CookieResult:
        """在受控页面会话内执行登录回调并返回兼容三元组。"""


class CaptchaHttpPort(Protocol):
    """声明验证码图片下载能力。"""

    def fetch(self, *, url: str, cookie: str, ua: str) -> Optional[bytes]:
        """下载验证码图片字节。"""


class CaptchaOcrPort(Protocol):
    """声明验证码图片识别能力。"""

    def recognize(self, image_data: bytes) -> str:
        """识别原始验证码图片字节，避免在应用层重复 Base64 编码。"""


_cookie_browser_port: Optional[CookieBrowserPort] = None
_captcha_http_port: Optional[CaptchaHttpPort] = None
_captcha_ocr_port: Optional[CaptchaOcrPort] = None


def configure_cookie_ports(*, browser: CookieBrowserPort, http: CaptchaHttpPort,
                           ocr: CaptchaOcrPort) -> None:
    """由组合根装配站点登录与验证码端口。"""
    global _cookie_browser_port, _captcha_http_port, _captcha_ocr_port
    _cookie_browser_port = browser
    _captcha_http_port = http
    _captcha_ocr_port = ocr


def reset_cookie_ports() -> None:
    """清除站点登录端口，避免跨生命周期保留 Adapter。"""
    global _cookie_browser_port, _captcha_http_port, _captcha_ocr_port
    _cookie_browser_port = None
    _captcha_http_port = None
    _captcha_ocr_port = None


def _require_cookie_ports() -> Tuple[CookieBrowserPort, CaptchaHttpPort, CaptchaOcrPort]:
    """返回已装配端口，缺失时明确拒绝隐式构造 Adapter。"""
    if _cookie_browser_port is None or _captcha_http_port is None or _captcha_ocr_port is None:
        raise RuntimeError("站点登录端口尚未由启动组合根装配")
    return _cookie_browser_port, _captcha_http_port, _captcha_ocr_port


class CookieLoginFormMixin:
    """提供跨站点登录表单、验证码和页面错误识别的共享能力。"""

    # 站点登录界面元素XPATH
    _SITE_LOGIN_XPATH = {
        "username": [
            '//input[@name="username"]',
            '//input[@name="user"]',
            '//input[@name="user_email"]',
            '//input[@name="txt_user"]',
            '//input[@name="txt_email"]',
            '//input[@name="email"]',
            '//input[@id="form_item_username"]',
            '//input[@id="username"]',
            '//input[@id="user"]',
            '//input[@id="email"]',
            '//input[contains(@placeholder,"用户名")]',
            '//input[contains(@placeholder,"邮箱")]',
            (
                '//input[not(translate(@type,"ABCDEFGHIJKLMNOPQRSTUVWXYZ",'
                '"abcdefghijklmnopqrstuvwxyz")="hidden") and '
                '(contains(translate(@name,"ABCDEFGHIJKLMNOPQRSTUVWXYZ",'
                '"abcdefghijklmnopqrstuvwxyz"),"user") or '
                'contains(translate(@name,"ABCDEFGHIJKLMNOPQRSTUVWXYZ",'
                '"abcdefghijklmnopqrstuvwxyz"),"email") or '
                'contains(translate(@id,"ABCDEFGHIJKLMNOPQRSTUVWXYZ",'
                '"abcdefghijklmnopqrstuvwxyz"),"user") or '
                'contains(translate(@id,"ABCDEFGHIJKLMNOPQRSTUVWXYZ",'
                '"abcdefghijklmnopqrstuvwxyz"),"email"))]'
            ),
        ],
        "password": [
            '//input[@name="password"]',
            '//input[@id="form_item_password"]',
            '//input[@id="password"]',
            '//input[@type="password"]',
            '//form[.//input[@type="password"]][1]//input[@type="password"][1]',
        ],
        "captcha": [
            '//input[@name="imagestring"]',
            '//input[@name="captcha"]',
            '//input[@name="captcha_code"]',
            '//input[@name="verifycode"]',
            '//input[@name="verification_code"]',
            '//input[@name="security_code"]',
            '//input[@name="imagecode"]',
            '//input[@id="form_item_captcha"]',
            '//input[@placeholder="驗證碼"]',
            '//input[contains(@placeholder,"验证码")]',
            (
                '//input[not(translate(@type,"ABCDEFGHIJKLMNOPQRSTUVWXYZ",'
                '"abcdefghijklmnopqrstuvwxyz")="hidden") and '
                '(contains(translate(@name,"ABCDEFGHIJKLMNOPQRSTUVWXYZ",'
                '"abcdefghijklmnopqrstuvwxyz"),"captcha") or '
                'contains(translate(@name,"ABCDEFGHIJKLMNOPQRSTUVWXYZ",'
                '"abcdefghijklmnopqrstuvwxyz"),"verify") or '
                'contains(translate(@id,"ABCDEFGHIJKLMNOPQRSTUVWXYZ",'
                '"abcdefghijklmnopqrstuvwxyz"),"captcha") or '
                'contains(translate(@id,"ABCDEFGHIJKLMNOPQRSTUVWXYZ",'
                '"abcdefghijklmnopqrstuvwxyz"),"verify"))]'
            ),
        ],
        "captcha_img": [
            '//img[@alt="captcha"]/@src',
            '//img[@alt="CAPTCHA"]/@src',
            '//img[@alt="SECURITY CODE"]/@src',
            '//img[@id="LAY-user-get-vercode"]/@src',
            '//img[contains(@src,"/api/getCaptcha")]/@src',
            (
                '//img[contains(translate(concat(@alt," ",@title," ",@class," ",@src),'
                '"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"),"captcha")]/@src'
            ),
            (
                '//img[contains(translate(concat(@alt," ",@title," ",@class," ",@src),'
                '"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"),"verify")]/@src'
            ),
        ],
        "submit": [
            '//input[@type="submit"]',
            '//button[@type="submit"]',
            '//button[@lay-filter="login"]',
            '//button[@lay-filter="formLogin"]',
            '//input[@type="button"][@value="登录"]',
            '//input[@id="submit-btn"]',
            '//button[contains(translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"),"login")]',
            '//button[contains(normalize-space(.),"登录") or contains(normalize-space(.),"登入") or contains(normalize-space(.),"提交")]',
            '//input[contains(translate(@value,"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"),"login")]',
        ],
        "error": [
            "//table[@class='main']//td[@class='text']/text()",
            '//*[@role="alert"]//text()',
            '//*[contains(translate(@class,"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"),"error")]//text()',
            '//*[contains(translate(@class,"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"),"alert")]//text()',
            '//*[contains(translate(@class,"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"),"message")]//text()',
        ],
        "remember": [
            '//input[@type="checkbox"][contains(@name,"remember") or contains(@id,"remember")]',
            '//*[@role="checkbox"][contains(.,"保持登录") or contains(.,"记住我") or contains(.,"自动登录")]',
        ],
        "twostep": [
            '//input[@name="two_step_code"]',
            '//input[@name="2fa_secret"]',
            '//input[@name="otp"]',
        ]
    }

    @classmethod
    def _first_xpath(cls, html: etree._Element, key: str) -> Optional[str]:
        """返回页面上第一个命中的字段 XPath。"""
        for xpath in cls._SITE_LOGIN_XPATH.get(key, []):
            if html.xpath(xpath):
                return xpath
        return None

    @classmethod
    def _find_login_fields(
        cls,
        html: etree._Element,
    ) -> tuple[Optional[str], Optional[str]]:
        """按常见命名、表单结构和输入类型推断用户名与密码字段。"""
        username_xpath = cls._first_xpath(html, "username")
        password_xpath = cls._first_xpath(html, "password")
        form_xpath = "//form[.//input[translate(@type,\"ABCDEFGHIJKLMNOPQRSTUVWXYZ\",\"abcdefghijklmnopqrstuvwxyz\")=\"password\"]][1]"
        form_nodes = html.xpath(form_xpath)
        if form_nodes:
            login_form = form_nodes[0]

            def belongs_to_login_form(xpath: Optional[str]) -> bool:
                """判断全局候选是否属于含密码字段的登录表单。"""
                if not xpath:
                    return False
                nodes = html.xpath(xpath)
                return bool(nodes) and nodes[0] in login_form.iter()

            if not belongs_to_login_form(username_xpath):
                username_xpath = None
            if not belongs_to_login_form(password_xpath):
                password_xpath = None

            if not password_xpath:
                password_xpath = f'{form_xpath}//input[@type="password"][1]'
            if not username_xpath:
                scoped_candidates: tuple[str, ...] = (
                    f'{form_xpath}//input[@name="username"][1]',
                    f'{form_xpath}//input[@name="user"][1]',
                    f'{form_xpath}//input[@name="user_email"][1]',
                    f'{form_xpath}//input[@name="txt_user"][1]',
                    f'{form_xpath}//input[@name="txt_email"][1]',
                    f'{form_xpath}//input[@name="email"][1]',
                    f'{form_xpath}//input[@id="username"][1]',
                    f'{form_xpath}//input[@id="user"][1]',
                    f'{form_xpath}//input[@id="email"][1]',
                    f'{form_xpath}//input[@type="email"][1]',
                    f'{form_xpath}//input[@type="text"][1]',
                    f'{form_xpath}//input[not(@type) and not(@name="password")][1]',
                )
                username_xpath = next(
                    (candidate for candidate in scoped_candidates if html.xpath(candidate)),
                    None,
                )

        if password_xpath and username_xpath:
            return username_xpath, password_xpath

        if not password_xpath and html.xpath(f"{form_xpath}//input[@type='password']"):
            password_xpath = f"{form_xpath}//input[@type='password'][1]"
        if not username_xpath:
            scoped_candidates = (
                f"{form_xpath}//input[@type='email'][1]",
                f"{form_xpath}//input[@type='text'][1]",
                f"{form_xpath}//input[not(@type) and not(@name='password')][1]",
            )
            username_xpath = next(
                (candidate for candidate in scoped_candidates if html.xpath(candidate)),
                None,
            )
        if not password_xpath and html.xpath("//input[@type='password'][1]"):
            password_xpath = "//input[@type='password'][1]"
        if not username_xpath:
            fallback_candidates = (
                "//input[@type='email'][1]",
                "//input[@type='text'][1]",
            )
            username_xpath = next(
                (candidate for candidate in fallback_candidates if html.xpath(candidate)),
                None,
            )
        return username_xpath, password_xpath

    @classmethod
    def _find_captcha_source(
        cls,
        html: etree._Element,
    ) -> tuple[Optional[str], Optional[str]]:
        """查找验证码输入字段及图片地址，兼容常见命名和站点自定义 class。"""
        captcha_xpath = cls._first_xpath(html, "captcha")
        if not captcha_xpath:
            return None, None
        for image_xpath in cls._SITE_LOGIN_XPATH.get("captcha_img", []):
            values = html.xpath(image_xpath)
            if values and isinstance(values[0], str):
                return captcha_xpath, values[0]
        return captcha_xpath, None

    @classmethod
    def _page_issue(cls, html_text: str, page_url: str) -> Optional[str]:
        """把站点不可用和人机挑战归类为 Agent 可采取行动的提示。"""
        text = " ".join((html_text or "").split())
        lowered = text.lower()
        url_lowered = (page_url or "").lower()
        if any(
            marker in lowered or marker in url_lowered
            for marker in (
                "cf-chl-",
                "cloudflare",
                "cf-turnstile",
                "turnstile",
                "challenge-platform",
                "captcha verification token is missing",
            )
        ):
            return "站点需要完成 Cloudflare/人机验证，无法自动登录，请手动登录后提供 Cookie"
        if any(
            marker in lowered or marker in url_lowered
            for marker in (
                "404 not found",
                "502 bad gateway",
                "503 service unavailable",
                "site not found",
                "没有找到站点",
                "站点不存在",
                "域名未绑定",
                "无法连接到站点",
            )
        ):
            return "站点不可用或域名未绑定源站，请先确认站点地址和网络状态"
        return None

    @classmethod
    def _error_message(cls, html_text: str) -> str:
        """提取登录页可见错误，并去除重复或过长的 HTML 文本。"""
        html = etree.HTML(html_text or "")
        if html is None:
            return ""
        messages: list[str] = []
        for xpath in cls._SITE_LOGIN_XPATH.get("error", []):
            for value in html.xpath(xpath):
                text = " ".join(str(value).split())
                if text and text not in messages:
                    messages.append(text)
        return "；".join(messages)[:500]

    @staticmethod
    def _page_user_agent(page: Any) -> str:
        """读取当前页面 User-Agent，浏览器实现不支持时返回空字符串。"""
        try:
            return str(page.evaluate("() => window.navigator.userAgent") or "")
        except Exception:
            return ""

    @staticmethod
    def _click_with_fallback(page: Any, selector: str, timeout: int = 5000) -> None:
        """依次尝试普通、强制、元素和 XPath JavaScript 点击。"""
        first_error: Optional[Exception] = None
        try:
            page.click(selector)
            return
        except Exception as error:
            first_error = error
        try:
            page.click(selector, timeout=timeout, force=True)
            return
        except Exception:
            pass
        try:
            element = page.query_selector(selector)
            if element is not None and hasattr(element, "click"):
                element.click(timeout=timeout, force=True)
                return
        except Exception:
            pass
        try:
            clicked = page.evaluate(
                """
                (xpath) => {
                    const node = document.evaluate(
                        xpath, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null
                    ).singleNodeValue;
                    if (!node) return false;
                    node.click();
                    return true;
                }
                """,
                selector,
            )
            if clicked is True or clicked is None:
                return
        except Exception:
            pass
        raise first_error or RuntimeError("无法点击页面元素")

    @classmethod
    def _refresh_captcha(cls, page: Any, html: etree._Element) -> bool:
        """点击验证码图片或调用页面 reload，尽量获取下一张验证码。"""
        for image_xpath in cls._SITE_LOGIN_XPATH.get("captcha_img", []):
            if not html.xpath(image_xpath):
                continue
            selector = image_xpath[:-5] if image_xpath.endswith("/@src") else image_xpath
            try:
                cls._click_with_fallback(page, selector, timeout=3000)
                return True
            except Exception:
                continue
        reload_page = getattr(page, "reload", None)
        if callable(reload_page):
            try:
                reload_page(wait_until="domcontentloaded", timeout=10000)
                return True
            except Exception:
                pass
        return False

class CookieHelper(CookieLoginFormMixin):
    """处理站点登录表单、验证码和 Cookie 获取流程。"""

    _MAX_CAPTCHA_ATTEMPTS = 3

    @staticmethod
    def get_page_content(page: Any, retries: int = 3, interval: float = 1.0) -> Optional[str]:
        """
        获取页面源码，页面跳转中（如登录前后的重定向）会导致 page.content() 抛出
        "Unable to retrieve content because the page is navigating" 异常，等待加载完成后重试
        :param page: 浏览器页面
        :param retries: 最大重试次数
        :param interval: 重试间隔（秒）
        :return: 页面源码
        """
        for i in range(retries):
            # 等待加载失败不代表源码不可读取，最后一次等待失败时仍尝试直接获取源码
            try:
                page.wait_for_load_state("domcontentloaded", timeout=10 * 1000)
            except Exception as e:
                if i < retries - 1:
                    logger.warning(f"等待页面加载完成失败：{str(e)}，{interval}秒后重试 ({i + 1}/{retries - 1})")
                    time.sleep(interval)
                    continue
                logger.warning(f"等待页面加载完成失败：{str(e)}，尝试直接获取源码")
            try:
                return page.content()
            except Exception as e:
                if i >= retries - 1:
                    logger.error(f"获取页面源码失败：{str(e)}")
                    return None
                logger.warning(f"获取页面源码失败：{str(e)}，{interval}秒后重试 ({i + 1}/{retries - 1})")
                time.sleep(interval)
        return None

    @staticmethod
    def parse_cookies(cookies: list) -> str:
        """将浏览器 Cookie 列表转成请求头字符串，并忽略不完整条目。"""
        if not cookies:
            return ""
        values: list[str] = []
        for cookie in cookies:
            if not isinstance(cookie, dict):
                continue
            name = cookie.get("name")
            value = cookie.get("value")
            if name is not None and value is not None:
                values.append(f"{name}={value}")
        return "; ".join(values) + ("; " if values else "")

    @staticmethod
    def _find_login_page_url(html: etree._Element, current_url: str) -> Optional[str]:
        """从首页查找同源登录入口，避免把账号密码提交到跨域页面。"""
        login_hrefs = html.xpath(
            "//a["
            "contains(translate(@href, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'login')"
            " or contains(translate(@href, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'signin')"
            "]/@href"
        )
        current = urlparse(current_url)
        for href in login_hrefs:
            login_url = urljoin(current_url, href)
            target = urlparse(login_url)
            if target.scheme in ("http", "https") and \
                    target.scheme == current.scheme and target.netloc == current.netloc:
                return str(login_url)
        return None

    def get_site_cookie_ua(self,
                           url: str,
                           username: str,
                           password: str,
                           two_step_code: Optional[str] = None,
                           proxies: Optional[dict] = None,
                           timeout: int = None) -> Tuple[Optional[str], Optional[str], str]:
        """获取站点 Cookie、User-Agent 和兼容错误消息。"""
        return self._get_site_cookie_ua_impl(
            url=url,
            username=username,
            password=password,
            two_step_code=two_step_code,
            proxies=proxies,
            timeout=timeout,
        )

    def _get_site_cookie_ua_impl(self,
                                 url: str,
                                 username: str,
                                 password: str,
                                 two_step_code: Optional[str] = None,
                                 proxies: Optional[dict] = None,
                                 timeout: int = None) -> Tuple[Optional[str], Optional[str], str]:
        """
        获取站点cookie和ua
        :param url: 站点地址
        :param username: 用户名
        :param password: 密码
        :param two_step_code: 二步验证码或密钥
        :param proxies: 代理
        :param timeout: 超时时间
        :return: cookie、ua、message
        """

        def __page_handler(page: Any) -> CookieResult:
            """在受控浏览器页面内完成登录并返回当前会话凭据。"""
            html_text = self.get_page_content(page)
            if not html_text:
                return None, None, "获取源码失败"
            html = etree.HTML(html_text)
            if html is None:
                return None, None, "解析网页源码失败"
            issue = self._page_issue(html_text, getattr(page, "url", "") or url)
            if issue:
                return None, None, issue

            username_xpath, password_xpath = self._find_login_fields(html)
            if not username_xpath or not password_xpath:
                login_url = self._find_login_page_url(html, getattr(page, "url", "") or url)
                if login_url:
                    try:
                        page.goto(
                            login_url,
                            wait_until="domcontentloaded",
                            timeout=(timeout or 60) * 1000,
                        )
                    except Exception as error:
                        return None, None, f"打开登录页面失败：{str(error)}"
                    html_text = self.get_page_content(page)
                    html = etree.HTML(html_text) if html_text else None
                if html is None:
                    return None, None, "解析网页源码失败"
                issue = self._page_issue(html_text or "", getattr(page, "url", "") or url)
                if issue:
                    return None, None, issue
                username_xpath, password_xpath = self._find_login_fields(html)
            if not username_xpath or not password_xpath:
                try:
                    page.wait_for_selector("xpath=//input[@type='password']", timeout=5000)
                except Exception:
                    pass
                latest_text = self.get_page_content(page)
                html = etree.HTML(latest_text) if latest_text else None
                if html is not None:
                    html_text = latest_text or html_text
                    username_xpath, password_xpath = self._find_login_fields(html)
            if not username_xpath:
                return None, None, "未找到用户名输入框，登录表单字段无法识别"
            if not password_xpath:
                return None, None, "未找到密码输入框，登录表单字段无法识别"

            otp_code = TwoFactorAuth(two_step_code).get_code()
            twostep_xpath = self._first_xpath(html, "twostep") if otp_code else None
            captcha_xpath, captcha_img_url = self._find_captcha_source(html)
            if captcha_xpath and not captcha_img_url:
                return None, None, "检测到验证码输入框，但未找到验证码图片"
            submit_xpath = self._first_xpath(html, "submit")
            if not submit_xpath:
                return None, None, "未找到登录按钮，请在登录页提供可提交的按钮"

            try:
                page.wait_for_selector(submit_xpath)
                page.fill(username_xpath, username)
                page.fill(password_xpath, password)
                for xpath in self._SITE_LOGIN_XPATH.get("remember", []):
                    remember_element = page.query_selector(xpath)
                    if not remember_element:
                        continue
                    try:
                        checked = remember_element.get_attribute("aria-checked")
                        if checked is None:
                            checked = "true" if remember_element.is_checked() else "false"
                        if checked != "true":
                            remember_element.click(timeout=3000)
                        break
                    except Exception as error:
                        logger.warning(f"勾选记住登录选项失败：{str(error)}，尝试下一候选")
                if twostep_xpath:
                    page.fill(twostep_xpath, otp_code)

                if captcha_xpath and captcha_img_url:
                    captcha_element = page.query_selector(captcha_xpath)
                    if captcha_element is None or captcha_element.is_visible():
                        captcha = ""
                        for attempt in range(self._MAX_CAPTCHA_ATTEMPTS):
                            current_text = self.get_page_content(page)
                            current_html = etree.HTML(current_text) if current_text else html
                            if current_html is None:
                                current_html = html
                            if current_html is None:
                                continue
                            current_xpath, current_image = self._find_captcha_source(current_html)
                            captcha_xpath = current_xpath or captcha_xpath
                            captcha_img_url = current_image or captcha_img_url
                            code_url = self.__get_captcha_url(
                                getattr(page, "url", "") or url,
                                captcha_img_url,
                            )
                            cookie = self.parse_cookies(page.context.cookies())
                            ua = self._page_user_agent(page)
                            captcha = self.__get_captcha_text(
                                cookie=cookie,
                                ua=ua,
                                code_url=code_url,
                            )
                            if captcha:
                                logger.info("验证码已完成识别，第 %s/%s 次尝试", attempt + 1, self._MAX_CAPTCHA_ATTEMPTS)
                                break
                            if attempt < self._MAX_CAPTCHA_ATTEMPTS - 1:
                                self._refresh_captcha(page, current_html)
                                time.sleep(0.5)
                        if not captcha:
                            return None, None, f"验证码识别失败，已尝试 {self._MAX_CAPTCHA_ATTEMPTS} 次，请手动刷新验证码后重试"
                        page.fill(captcha_xpath, captcha)

                self._click_with_fallback(page, submit_xpath)
                page.wait_for_load_state("networkidle", timeout=30 * 1000)
            except Exception as error:
                logger.error(f"仿真登录失败：{str(error)}")
                return None, None, f"仿真登录失败：{str(error)}"

            current_url = (getattr(page, "url", "") or "").lower()
            if "verify" in current_url:
                if not otp_code:
                    return None, None, "站点要求二次验证码，请提供二步验证码或密钥"
                html_text = self.get_page_content(page)
                html = etree.HTML(html_text) if html_text else None
                verify_xpath = self._first_xpath(html, "twostep") if html is not None else None
                if verify_xpath:
                    try:
                        page.fill(verify_xpath, TwoFactorAuth(two_step_code).get_code())
                        self._click_with_fallback(page, submit_xpath)
                        page.wait_for_load_state("networkidle", timeout=30 * 1000)
                    except Exception as error:
                        logger.error(f"二次验证码输入失败：{str(error)}")
                        return None, None, f"二次验证码输入失败：{str(error)}"

            first_failure_html: Optional[str] = None
            for index in range(3):
                if index:
                    time.sleep(2)
                latest_text = self.get_page_content(page)
                if not latest_text:
                    continue
                if SiteUtils.is_logged_in(latest_text):
                    return self.parse_cookies(page.context.cookies()), self._page_user_agent(page), ""
                if first_failure_html is None:
                    first_failure_html = latest_text
                failure_issue = self._page_issue(
                    latest_text,
                    getattr(page, "url", "") or url,
                )
                if failure_issue or self._error_message(latest_text):
                    first_failure_html = latest_text
                    break
            if not first_failure_html:
                return None, None, "获取登录结果源码失败"
            failure_issue = self._page_issue(
                first_failure_html,
                getattr(page, "url", "") or url,
            )
            if failure_issue:
                return None, None, failure_issue
            error_message = self._error_message(first_failure_html)
            return None, None, f"登录失败：{error_message}" if error_message else "登录失败：页面未提供具体原因"

        if not url or not username or not password:
            return None, None, "参数错误"

        browser_port, _, _ = _require_cookie_ports()
        return browser_port.action(url=url,
                                   callback=__page_handler,
                                   proxies=proxies,
                                   timeout=timeout)

    @staticmethod
    def __get_captcha_text(cookie: str, ua: str, code_url: str) -> str:
        """
        识别验证码图片的内容
        """
        if not code_url:
            return ""
        _, http_port, ocr_port = _require_cookie_ports()
        content = http_port.fetch(url=code_url, cookie=cookie, ua=ua)
        if not content:
            return ""
        return ocr_port.recognize(content)

    @staticmethod
    def __get_captcha_url(siteurl: str, imageurl: str) -> str:
        """按页面地址解析验证码图片地址，兼容绝对、根相对和路径相对 URL。"""
        if not siteurl or not imageurl:
            return ""
        return urljoin(siteurl, imageurl)
