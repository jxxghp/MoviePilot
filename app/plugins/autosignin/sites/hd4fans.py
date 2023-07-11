from app.plugins.modules._autosignin._base import _ISiteSigninHandler
from app.utils import StringUtils, RequestUtils
from config import Config


class HD4FANS(_ISiteSigninHandler):
    """
    兽签到
    """

    # 匹配的站点Url，每一个实现类都需要设置为自己的站点Url
    site_url = "pt.hd4fans.org"

    # 签到成功
    _repeat_text = '<span id="checkedin">[签到成功]</span>'
    _success_text = "签到成功"

    @classmethod
    def match(cls, url):
        """
        根据站点Url判断是否匹配当前站点签到类，大部分情况使用默认实现即可
        :param url: 站点Url
        :return: 是否匹配，如匹配则会调用该类的signin方法
        """
        return True if StringUtils.url_equal(url, cls.site_url) else False

    def signin(self, site_info: dict):
        """
        执行签到操作
        :param site_info: 站点信息，含有站点Url、站点Cookie、UA等信息
        :return: 签到结果信息
        """
        site = site_info.get("name")
        site_cookie = site_info.get("cookie")
        ua = site_info.get("ua")
        proxy = Config().get_proxies() if site_info.get("proxy") else None

        # 获取页面html
        html_res = RequestUtils(cookies=site_cookie,
                                headers=ua,
                                proxies=proxy
                                ).get_res(url="https://pt.hd4fans.org/index.php")
        if not html_res or html_res.status_code != 200:
            self.error(f"签到失败，请检查站点连通性")
            return False, f'【{site}】签到失败，请检查站点连通性'

        if "login.php" in html_res.text:
            self.error(f"签到失败，cookie失效")
            return False, f'【{site}】签到失败，cookie失效'

        # 判断是否已签到
        if self._repeat_text in html_res.text:
            self.info(f"今日已签到")
            return True, f'【{site}】今日已签到'

        # 签到
        data = {
            'action': 'checkin'
        }
        sign_res = RequestUtils(cookies=site_cookie,
                                headers=ua,
                                proxies=proxy
                                ).post_res(url="https://pt.hd4fans.org/checkin.php", data=data)
        if not sign_res or sign_res.status_code != 200:
            self.error(f"签到失败，请检查站点连通性")
            return False, f'【{site}】签到失败，请检查站点连通性'
        if not sign_res.text and int(sign_res.text) > 0:
            self.info(f"签到成功")
            return True, f'【{site}】签到成功'

        self.error(f"签到失败，签到接口返回 {sign_res.text}")
        return False, f'【{site}】签到失败'
