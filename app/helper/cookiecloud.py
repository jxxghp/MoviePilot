import json

from typing import Tuple, Optional
from hashlib import md5

from app.utils.http import RequestUtils
from app.utils.string import StringUtils
from app.utils.common import decrypt


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
        ret = self._req.get_res(url=req_url)
        if ret and ret.status_code == 200:
            result = ret.json()
            if not result:
                return {}, "未下载到数据"        
            encrypted = result.get("encrypted")
            if not encrypted:
                return {}, "未获取到cookie密文"
            else:
                crypt_key = self.get_crypt_key()
                try:
                    decrypted_data = decrypt(encrypted, crypt_key).decode('utf-8')
                    result = json.loads(decrypted_data)
                except Exception as e:
                    return {}, "cookie解密失败" + str(e)

            if not result:
                return {}, "cookie解密为空"
            
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
        
    def get_crypt_key(self) -> bytes:
        """
        使用UUID和密码生成CookieCloud的加解密密钥
        """
        md5_generator = md5()
        md5_generator.update((str(self._key).strip() + '-' + str(self._password).strip()).encode('utf-8'))
        return (md5_generator.hexdigest()[:16]).encode('utf-8')
