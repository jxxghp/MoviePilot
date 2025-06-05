import json
from typing import Any, Dict, Tuple, Optional

from app.core.config import settings
from app.log import logger
from app.utils.crypto import CryptoJsUtils, HashUtils
from app.utils.http import RequestUtils
from app.utils.string import StringUtils
from app.utils.url import UrlUtils


class CookieCloudHelper:
    _ignore_cookies: list = ["CookieAutoDeleteBrowsingDataCleanup", "CookieAutoDeleteCleaningDiscarded"]

    def __init__(self):
        self.__sync_setting()

    def __sync_setting(self):
        """
        同步CookieCloud配置项
        """
        self._server = UrlUtils.standardize_base_url(settings.COOKIECLOUD_HOST)
        self._key = StringUtils.safe_strip(settings.COOKIECLOUD_KEY)
        self._password = StringUtils.safe_strip(settings.COOKIECLOUD_PASSWORD)
        self._enable_local = settings.COOKIECLOUD_ENABLE_LOCAL
        self._local_path = settings.COOKIE_PATH

    def download(self) -> Tuple[Optional[dict], str]:
        """
        从CookieCloud下载数据
        :return: Cookie数据、错误信息
        """
        # 更新为最新设置
        self.__sync_setting()

        if ((not self._server and not self._enable_local)
                or not self._key
                or not self._password):
            return None, "CookieCloud参数不正确"

        if self._enable_local:
            # 开启本地服务时，从本地直接读取数据
            result = self.__load_local_encrypt_data(self._key)
            if not result:
                return {}, "未从本地CookieCloud服务加载到cookie数据，请检查服务器设置、用户KEY及加密密码是否正确"
        else:
            req_url = UrlUtils.combine_url(host=self._server, path=f"get/{self._key}")
            ret = RequestUtils(content_type="application/json").get_res(url=req_url)
            if ret and ret.status_code == 200:
                try:
                    result = ret.json()
                    if not result:
                        return {}, f"未从{self._server}下载到cookie数据"
                except Exception as err:
                    return {}, f"从{self._server}下载cookie数据错误：{str(err)}"
            elif ret:
                return None, f"远程同步CookieCloud失败，错误码：{ret.status_code}"
            else:
                return None, "CookieCloud请求失败，请检查服务器地址、用户KEY及加密密码是否正确"

        encrypted = result.get("encrypted")
        if not encrypted:
            return {}, "未获取到cookie密文"
        else:
            crypt_key = self.__get_crypt_key()
            try:
                decrypted_data = CryptoJsUtils.decrypt(encrypted, crypt_key).decode("utf-8")
                result = json.loads(decrypted_data)
            except Exception as e:
                return {}, "cookie解密失败：" + str(e)

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

    def __get_crypt_key(self) -> bytes:
        """
        使用UUID和密码生成CookieCloud的加解密密钥
        """
        combined_string = f"{self._key}-{self._password}"
        return HashUtils.md5(combined_string)[:16].encode("utf-8")

    def __load_local_encrypt_data(self, uuid: str) -> Dict[str, Any]:
        """
        获取本地CookieCloud数据
        """
        file_path = self._local_path / f"{uuid}.json"
        # 检查文件是否存在
        if not file_path.exists():
            logger.warn(f"本地CookieCloud文件不存在：{file_path}")
            return {}

        # 读取文件
        with open(file_path, encoding="utf-8", mode="r") as file:
            read_content = file.read()
        data = json.loads(read_content.encode("utf-8"))
        return data
