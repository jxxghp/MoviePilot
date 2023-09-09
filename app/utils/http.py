from typing import Union, Any, Optional

import requests
import urllib3
from requests import Session, Response
from urllib3.exceptions import InsecureRequestWarning

urllib3.disable_warnings(InsecureRequestWarning)


class RequestUtils:
    _headers: dict = None
    _cookies: Union[str, dict] = None
    _proxies: dict = None
    _timeout: int = 20
    _session: Session = None

    def __init__(self,
                 headers: dict = None,
                 ua: str = None,
                 cookies: Union[str, dict] = None,
                 proxies: dict = None,
                 session: Session = None,
                 timeout: int = None,
                 referer: str = None,
                 content_type: str = None,
                 accept_type: str = None):
        if not content_type:
            content_type = "application/x-www-form-urlencoded; charset=UTF-8"
        if headers:
            self._headers = headers
        else:
            self._headers = {
                "User-Agent": ua,
                "Content-Type": content_type,
                "Accept": accept_type,
                "referer": referer
            }
        if cookies:
            if isinstance(cookies, str):
                self._cookies = self.cookie_parse(cookies)
            else:
                self._cookies = cookies
        if proxies:
            self._proxies = proxies
        if session:
            self._session = session
        if timeout:
            self._timeout = timeout

    def post(self, url: str, data: Any = None, json: dict = None) -> Optional[Response]:
        if json is None:
            json = {}
        try:
            if self._session:
                return self._session.post(url,
                                          data=data,
                                          verify=False,
                                          headers=self._headers,
                                          proxies=self._proxies,
                                          timeout=self._timeout,
                                          json=json)
            else:
                return requests.post(url,
                                     data=data,
                                     verify=False,
                                     headers=self._headers,
                                     proxies=self._proxies,
                                     timeout=self._timeout,
                                     json=json)
        except requests.exceptions.RequestException:
            return None

    def get(self, url: str, params: dict = None) -> Optional[str]:
        try:
            if self._session:
                r = self._session.get(url,
                                      verify=False,
                                      headers=self._headers,
                                      proxies=self._proxies,
                                      timeout=self._timeout,
                                      params=params)
            else:
                r = requests.get(url,
                                 verify=False,
                                 headers=self._headers,
                                 proxies=self._proxies,
                                 timeout=self._timeout,
                                 params=params)
            return str(r.content, 'utf-8')
        except requests.exceptions.RequestException:
            return None

    def get_res(self, url: str, params: dict = None,
                allow_redirects: bool = True, raise_exception: bool = False) -> Optional[Response]:
        try:
            if self._session:
                return self._session.get(url,
                                         params=params,
                                         verify=False,
                                         headers=self._headers,
                                         proxies=self._proxies,
                                         cookies=self._cookies,
                                         timeout=self._timeout,
                                         allow_redirects=allow_redirects)
            else:
                return requests.get(url,
                                    params=params,
                                    verify=False,
                                    headers=self._headers,
                                    proxies=self._proxies,
                                    cookies=self._cookies,
                                    timeout=self._timeout,
                                    allow_redirects=allow_redirects)
        except requests.exceptions.RequestException:
            if raise_exception:
                raise requests.exceptions.RequestException
            return None

    def post_res(self, url: str, data: Any = None, params: dict = None,
                 allow_redirects: bool = True,
                 files: Any = None,
                 json: dict = None) -> Optional[Response]:
        try:
            if self._session:
                return self._session.post(url,
                                          data=data,
                                          params=params,
                                          verify=False,
                                          headers=self._headers,
                                          proxies=self._proxies,
                                          cookies=self._cookies,
                                          timeout=self._timeout,
                                          allow_redirects=allow_redirects,
                                          files=files,
                                          json=json)
            else:
                return requests.post(url,
                                     data=data,
                                     params=params,
                                     verify=False,
                                     headers=self._headers,
                                     proxies=self._proxies,
                                     cookies=self._cookies,
                                     timeout=self._timeout,
                                     allow_redirects=allow_redirects,
                                     files=files,
                                     json=json)
        except requests.exceptions.RequestException:
            return None

    @staticmethod
    def cookie_parse(cookies_str: str, array: bool = False) -> Union[list, dict]:
        """
        解析cookie，转化为字典或者数组
        :param cookies_str: cookie字符串
        :param array: 是否转化为数组
        :return: 字典或者数组
        """
        if not cookies_str:
            return {}
        cookie_dict = {}
        cookies = cookies_str.split(';')
        for cookie in cookies:
            cstr = cookie.split('=')
            if len(cstr) > 1:
                cookie_dict[cstr[0].strip()] = cstr[1].strip()
        if array:
            cookiesList = []
            for cookieName, cookieValue in cookie_dict.items():
                cookies = {'name': cookieName, 'value': cookieValue}
                cookiesList.append(cookies)
            return cookiesList
        return cookie_dict


class WebUtils:
    @staticmethod
    def get_location(ip: str):
        """
        https://api.mir6.com/api/ip
        {
            "code": 200,
            "msg": "success",
            "data": {
                "ip": "240e:97c:2f:1::5c",
                "dec": "47925092370311863177116789888333643868",
                "country": "中国",
                "countryCode": "CN",
                "province": "广东省",
                "city": "广州市",
                "districts": "",
                "idc": "",
                "isp": "中国电信",
                "net": "数据中心",
                "zipcode": "510000",
                "areacode": "020",
                "protocol": "IPv6",
                "location": "中国[CN] 广东省 广州市",
                "myip": "125.89.7.89",
                "time": "2023-09-01 17:28:23"
            }
        }
        """
        try:
            r = RequestUtils().get_res(f"https://api.mir6.com/api/ip?ip={ip}&type=json")
            if r:
                return r.json().get("data", {}).get("location") or ''
        except Exception as err:
            return str(err)
