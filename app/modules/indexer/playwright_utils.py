from playwright.sync_api import sync_playwright


class PlaywrightUtils:
    def __init__(self, browser_type="chromium"):
        self.browser_type = browser_type

    def get_page_source(self, url: str,
                        cookie: str = None,
                        ua: str = None,
                        proxy: dict = None,
                        headless: bool = True):
        """
        获取网页源码
        :param url: 网页地址
        :param cookie: cookie
        :param ua: user-agent
        :param proxy: 代理
        :param headless: 是否无头模式
        """
        with sync_playwright() as playwright:
            browser = playwright[self.browser_type].launch(headless=headless)
            context = browser.new_context(user_agent=ua, proxy=proxy)
            page = context.new_page()
            if cookie:
                page.set_extra_http_headers({"cookie": cookie})
            page.goto(url)
            page.wait_for_load_state("networkidle")
            source = page.content()
            browser.close()

        return source


# 示例用法
if __name__ == "__main__":
    utils = PlaywrightUtils()
    test_url = "https://www.baidu.com"
    test_cookies = "cookie1=value1; cookie2=value2"
    test_user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/93.0.4577.63 Safari/537.36"
    source_code = utils.get_page_source(test_url, cookie=test_cookies, ua=test_user_agent)
    print(source_code)
