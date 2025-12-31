import uuid
from typing import Callable, Any, Optional

from cf_clearance import sync_cf_retry, sync_stealth
from playwright.sync_api import sync_playwright, Page

from app.core.config import settings
from app.log import logger
from app.utils.http import RequestUtils, cookie_parse


class PlaywrightHelper:
    def __init__(self, browser_type=settings.PLAYWRIGHT_BROWSER_TYPE):
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
                               proxy_config: Optional[dict] = None,
                               timeout: Optional[int] = 60) -> Optional[dict]:
        """
        调用 FlareSolverr 解决 Cloudflare 并返回 solution 结果
        参考: https://github.com/FlareSolverr/FlareSolverr
        """
        if not settings.FLARESOLVERR_URL:
            logger.warn("未配置 FLARESOLVERR_URL，无法使用 FlareSolverr")
            return None

        fs_api = settings.FLARESOLVERR_URL.rstrip("/") + "/v1"
        session_id = None

        try:
            # 检查是否需要代理认证
            need_proxy_auth = (proxy_config and proxy_config.get("server") and
                               (proxy_config.get("username") or proxy_config.get("password")))

            if need_proxy_auth:
                # 使用 session 模式支持代理认证
                logger.debug("检测到flaresolverr代理需要认证，使用 session 模式")

                # 1. 创建会话
                session_id = str(uuid.uuid4())
                create_payload: dict = {
                    "cmd": "sessions.create",
                    "session": session_id
                }

                # 添加代理配置到会话创建请求
                if proxy_config and proxy_config.get("server"):
                    proxy_payload: dict = {"url": proxy_config["server"]}
                    if proxy_config.get("username"):
                        proxy_payload["username"] = proxy_config["username"]
                    if proxy_config.get("password"):
                        proxy_payload["password"] = proxy_config["password"]
                    create_payload["proxy"] = proxy_payload

                # 创建会话
                create_result = RequestUtils(content_type="application/json",
                                             timeout=timeout or 60).post_json(url=fs_api, json=create_payload)
                if not create_result or create_result.get("status") != "ok":
                    logger.error(
                        f"创建 FlareSolverr 会话失败: {create_result.get('message') if create_result else '无响应'}")
                    return None

                # 2. 使用会话发送请求
                request_payload = {
                    "cmd": "request.get",
                    "url": url,
                    "session": session_id,
                    "maxTimeout": int(timeout or 60) * 1000,
                }
            else:
                # 使用普通模式（无代理认证）
                request_payload = {
                    "cmd": "request.get",
                    "url": url,
                    "maxTimeout": int(timeout or 60) * 1000,
                }
                # 添加代理配置（仅 URL，无认证）
                if proxy_config and proxy_config.get("server"):
                    request_payload["proxy"] = {"url": proxy_config["server"]}

            # 将 cookies 以数组形式传递给 FlareSolverr
            if cookies:
                try:
                    request_payload["cookies"] = cookie_parse(cookies, array=True)
                except Exception as e:
                    logger.debug(f"解析 cookies 失败，忽略: {str(e)}")

            # 发送请求
            data = RequestUtils(content_type="application/json",
                                timeout=timeout or 60).post_json(url=fs_api, json=request_payload)
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
        finally:
            # 清理会话
            if session_id:
                try:
                    destroy_payload = {
                        "cmd": "sessions.destroy",
                        "session": session_id
                    }
                    RequestUtils(content_type="application/json",
                                 timeout=10).post_json(url=fs_api, json=destroy_payload)
                    logger.debug(f"已清理 FlareSolverr 会话: {session_id}")
                except Exception as e:
                    logger.warning(f"清理 FlareSolverr 会话失败: {str(e)}")

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
                        solution = self.__flaresolverr_request(url=url, cookies=cookies,
                                                               proxy_config=proxies, timeout=timeout)
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
                solution = self.__flaresolverr_request(url=url, cookies=cookies,
                                                       proxy_config=proxies, timeout=timeout)
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
