from typing import Tuple, Optional

from app.utils.http import RequestUtils
from app.utils.string import StringUtils


class CookieCloudHelper:

    _ignore_cookies: list = ["CookieAutoDeleteBrowsingDataCleanup", "CookieAutoDeleteCleaningDiscarded"]

    def __init__(self, server: str, key: str, password: str):
        self._server = server
        self._key = key
        self._password = password
        self._req = RequestUtils(content_type="application/json")

    def download(self) -> Tuple[Optional[dict], str]:
        """
        从CookieCloud下载数据
        :return: Cookie数据、错误信息
        """
        if not self._server or not self._key or not self._password:
            return None, "CookieCloud参数不正确"
        req_url = "%s/get/%s" % (self._server, str(self._key).strip())
        ret = self._req.post_res(url=req_url, json={"password": str(self._password).strip()})
        if ret and ret.status_code == 200:
            result = ret.json()
            if not result:
                return {}, "未下载到数据"
            if result.get("cookie_data"):
                contents = result.get("cookie_data")
            else:
                contents = result
            # 整理数据,使用domain域名的最后两级作为分组依据
            domain_groups = {}
            for site, cookies in contents.items():
                for cookie in cookies:
                    domain_key = StringUtils.get_url_domain(cookie.get("domain"))
                    if not domain_groups.get(domain_key):
                        domain_groups[domain_key] = [cookie]
                    else:
                        domain_groups[domain_key].append(cookie)
            # 返回错误
            ret_cookies = {}
            # 索引器
            for domain, content_list in domain_groups.items():
                if not content_list:
                    continue
                # 只有cf的cookie过滤掉
                cloudflare_cookie = True
                for content in content_list:
                    if content["name"] != "cf_clearance":
                        cloudflare_cookie = False
                        break
                if cloudflare_cookie:
                    continue
                # 站点Cookie
                cookie_str = ";".join(
                    [f"{content.get('name')}={content.get('value')}"
                     for content in content_list
                     if content.get("name") and content.get("name") not in self._ignore_cookies]
                )
                ret_cookies[domain] = cookie_str
            return ret_cookies, ""
        elif ret:
            return None, f"同步CookieCloud失败，错误码：{ret.status_code}"
        else:
            return None, "CookieCloud请求失败，请检查服务器地址、用户KEY及加密密码是否正确"
