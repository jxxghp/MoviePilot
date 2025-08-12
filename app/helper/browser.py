from typing import Callable, Any, Optional

from cf_clearance import sync_cf_retry, sync_stealth
from playwright.sync_api import sync_playwright, Page

from app.core.config import settings
from app.log import logger
from app.utils.http import RequestUtils, cookie_parse


class PlaywrightHelper:
    def __init__(self, browser_type="chromium"):
        self.browser_type = browser_type

    @staticmethod
    def __pass_cloudflare(url: str, page: Page) -> bool:
        """
        尝试跳过cloudfare验证
        """
        sync_stealth(page, pure=True)
        page.goto(url)
        return sync_cf_retry(page)[0]

    @staticmethod
    def __fs_cookie_str(cookies: list) -> str:
        if not cookies:
            return ""
        return "; ".join([f"{c.get('name')}={c.get('value')}" for c in cookies if c and c.get('name') is not None])

    @staticmethod
    def __flaresolverr_request(url: str,
                               cookies: Optional[str] = None,
                               proxy_url: Optional[str] = None,
                               timeout: Optional[int] = 60) -> Optional[dict]:
        """
        调用 FlareSolverr 解决 Cloudflare 并返回 solution 结果
        参考: https://github.com/FlareSolverr/FlareSolverr
        """
        if not settings.FLARESOLVERR_URL:
            logger.warn("未配置 FLARESOLVERR_URL，无法使用 FlareSolverr")
            return None

        payload = {
            "cmd": "request.get",
            "url": url,
            "maxTimeout": int(timeout or 60) * 1000,
        }
        # 将 cookies 以数组形式传递给 FlareSolverr
        if cookies:
            try:
                payload["cookies"] = cookie_parse(cookies, array=True)
            except Exception as e:
                logger.debug(f"解析 cookies 失败，忽略: {str(e)}")
        if proxy_url:
            payload["proxy"] = {"url": proxy_url}

        try:
            fs_api = settings.FLARESOLVERR_URL.rstrip("/") + "/v1"
            data = RequestUtils(content_type="application/json",
                                timeout=timeout).post_json(url=fs_api, json=payload)
            if not data:
                logger.error("FlareSolverr 返回空响应")
                return None
            if data.get("status") != "ok":
                logger.error(f"FlareSolverr 调用失败: {data.get('message')}")
                return None
            return data.get("solution")
        except Exception as e:
            logger.error(f"调用 FlareSolverr 失败: {str(e)}")
            return None

    def action(self, url: str,
               callback: Callable,
               cookies: Optional[str] = None,
               ua: Optional[str] = None,
               proxies: Optional[dict] = None,
               headless: Optional[bool] = False,
               timeout: Optional[int] = 60) -> Any:
        """
        访问网页，接收Page对象并执行操作
        :param url: 网页地址
        :param callback: 回调函数，需要接收page对象
        :param cookies: cookies
        :param ua: user-agent
        :param proxies: 代理
        :param headless: 是否无头模式
        :param timeout: 超时时间
        """
        result = None
        try:
            with sync_playwright() as playwright:
                browser = None
                context = None
                page = None
                try:
                    # 如果配置使用 FlareSolverr，先通过其获取清除后的 cookies 与 UA
                    fs_cookie_header = None
                    fs_ua = None
                    if settings.BROWSER_EMULATION == "flaresolverr":
                        proxy_url = None
                        if proxies and isinstance(proxies, dict):
                            proxy_url = proxies.get("server")
                        solution = self.__flaresolverr_request(url=url, cookies=cookies,
                                                               proxy_url=proxy_url, timeout=timeout)
                        if solution:
                            fs_cookie_header = self.__fs_cookie_str(solution.get("cookies", []))
                            fs_ua = solution.get("userAgent")

                    browser = playwright[self.browser_type].launch(headless=headless)
                    context = browser.new_context(user_agent=fs_ua or ua, proxy=proxies)
                    page = context.new_page()

                    # 优先使用 FlareSolverr 返回，其次使用入参
                    merged_cookie = fs_cookie_header or cookies
                    if merged_cookie:
                        page.set_extra_http_headers({"cookie": merged_cookie})

                    if settings.BROWSER_EMULATION == "playwright":
                        if not self.__pass_cloudflare(url, page):
                            logger.warn("cloudflare challenge fail！")
                    else:
                        page.goto(url)
                    page.wait_for_load_state("networkidle", timeout=timeout * 1000)

                    # 回调函数
                    result = callback(page)

                except Exception as e:
                    logger.error(f"网页操作失败: {str(e)}")
                finally:
                    if page:
                        page.close()
                    if context:
                        context.close()
                    if browser:
                        browser.close()
        except Exception as e:
            logger.error(f"Playwright初始化失败: {str(e)}")

        return result

    def get_page_source(self, url: str,
                        cookies: Optional[str] = None,
                        ua: Optional[str] = None,
                        proxies: Optional[dict] = None,
                        headless: Optional[bool] = False,
                        timeout: Optional[int] = 60) -> Optional[str]:
        """
        获取网页源码
        :param url: 网页地址
        :param cookies: cookies
        :param ua: user-agent
        :param proxies: 代理
        :param headless: 是否无头模式
        :param timeout: 超时时间
        """
        source = None
        # 如果配置为 FlareSolverr，则直接调用获取页面源码
        if settings.BROWSER_EMULATION == "flaresolverr":
            try:
                proxy_url = None
                if proxies and isinstance(proxies, dict):
                    proxy_url = proxies.get("server")
                solution = self.__flaresolverr_request(url=url, cookies=cookies,
                                                       proxy_url=proxy_url, timeout=timeout)
                if solution:
                    return solution.get("response")
            except Exception as e:
                logger.error(f"FlareSolverr 获取源码失败: {str(e)}")
        try:
            with sync_playwright() as playwright:
                browser = None
                context = None
                page = None
                try:
                    browser = playwright[self.browser_type].launch(headless=headless)
                    context = browser.new_context(user_agent=ua, proxy=proxies)
                    page = context.new_page()

                    if cookies:
                        page.set_extra_http_headers({"cookie": cookies})

                    if not self.__pass_cloudflare(url, page):
                        logger.warn("cloudflare challenge fail！")
                    page.wait_for_load_state("networkidle", timeout=timeout * 1000)

                    source = page.content()

                except Exception as e:
                    logger.error(f"获取网页源码失败: {str(e)}")
                    source = None
                finally:
                    # 确保资源被正确清理
                    if page:
                        page.close()
                    if context:
                        context.close()
                    if browser:
                        browser.close()
        except Exception as e:
            logger.error(f"Playwright初始化失败: {str(e)}")

        return source
