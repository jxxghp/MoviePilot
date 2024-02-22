from lxml import etree


class SiteUtils:

    @classmethod
    def is_logged_in(cls, html_text: str) -> bool:
        """
        判断站点是否已经登陆
        :param html_text:
        :return:
        """
        html = etree.HTML(html_text)
        if not html:
            return False
        # 存在明显的密码输入框，说明未登录
        if html.xpath("//input[@type='password']"):
            return False
        # 是否存在登出和用户面板等链接
        xpaths = [
            '//a[contains(@href, "logout")'
            ' or contains(@data-url, "logout")'
            ' or contains(@href, "mybonus") '
            ' or contains(@onclick, "logout")'
            ' or contains(@href, "usercp")]',
            '//form[contains(@action, "logout")]',
            '//div[@class="user-info-side"]',
            '//a[@id="myitem"]'
        ]
        for xpath in xpaths:
            if html.xpath(xpath):
                return True
        return False

    @classmethod
    def is_checkin(cls, html_text: str) -> bool:
        """
        判断站点是否已经签到
        :return True已签到 False未签到
        """
        html = etree.HTML(html_text)
        if not html:
            return False
        # 站点签到支持的识别XPATH
        xpaths = [
            '//a[@id="signed"]',
            '//a[contains(@href, "attendance")]',
            '//a[contains(text(), "签到")]',
            '//a/b[contains(text(), "签 到")]',
            '//span[@id="sign_in"]/a',
            '//a[contains(@href, "addbonus")]',
            '//input[@class="dt_button"][contains(@value, "打卡")]',
            '//a[contains(@href, "sign_in")]',
            '//a[contains(@onclick, "do_signin")]',
            '//a[@id="do-attendance"]',
            '//shark-icon-button[@href="attendance.php"]'
        ]
        for xpath in xpaths:
            if html.xpath(xpath):
                return False

        return True
