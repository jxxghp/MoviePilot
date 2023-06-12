from playwright.sync_api import sync_playwright
from cf_clearance import sync_cf_retry, sync_stealth
from app.log import logger


class PlaywrightHelper:
    def __init__(self, browser_type="chromium"):
        self.browser_type = browser_type

    def get_page_source(self, url: str,
                        cookies: str = None,
                        ua: str = None,
                        proxies: dict = None,
                        headless: bool = True,
                        timeout: int = 30) -> str:
        """
        获取网页源码
        :param url: 网页地址
        :param cookies: cookies
        :param ua: user-agent
        :param proxies: 代理
        :param headless: 是否无头模式
        :param timeout: 超时时间
        """
        source = ""
        try:
            with sync_playwright() as playwright:
                browser = playwright[self.browser_type].launch(headless=headless)
                context = browser.new_context(user_agent=ua, proxy=proxies)
                page = context.new_page()
                if cookies:
                    page.set_extra_http_headers({"cookie": cookies})
                try:
                    sync_stealth(page, pure=True)
                    page.goto(url)
                    res = sync_cf_retry(page)
                    if not res:
                        logger.warn("cloudflare challenge fail！")
                    page.wait_for_load_state("networkidle", timeout=timeout * 1000)
                    source = page.content()
                except Exception as e:
                    logger.error(f"获取网页源码失败: {e}")
                    source = None
                finally:
                    browser.close()
        except Exception as e:
            logger.error(f"获取网页源码失败: {e}")
        return source


# 示例用法
if __name__ == "__main__":
    utils = PlaywrightHelper()
    test_url = "https://piggo.me"
    test_cookies = ""
    test_user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/93.0.4577.63 Safari/537.36"
    source_code = utils.get_page_source(test_url, cookies=test_cookies, ua=test_user_agent)
    print(source_code)
