"""
通过两个外部 IP 归属服务查询地址文本，并在首选服务无结果时回退。
"""

from app.adapters.network.http import RequestUtils
from app.schemas.common import JsonData


class WebUtils:
    """
    通过外部网络服务查询 IP 归属信息。
    """

    @staticmethod
    def get_location(ip: str) -> JsonData:
        """
        依次查询两个 IP 归属服务，首选服务无结果时再调用备用服务。

        :param ip: 待查询的 IPv4 或 IPv6 地址文本
        :return: 服务返回的地址信息；两者均无有效结果时返回空字符串
        """
        return WebUtils.get_location1(ip) or WebUtils.get_location2(ip)

    @staticmethod
    def get_location1(ip: str) -> JsonData:
        """
        通过 api.mir6.com 查询 IP 归属地。

        :param ip: 待查询的 IP 地址
        :return: 响应 data.location 的值；请求或解析失败时返回空字符串
        """
        try:
            r = RequestUtils().get_res(f"https://api.mir6.com/api/ip?ip={ip}&type=json")
            if r:
                location: JsonData = r.json().get("data", {}).get("location") or ''
                return location
        except Exception as err:
            print(str(err))
        return ""

    @staticmethod
    def get_location2(ip: str) -> JsonData:
        """
        通过太平洋网络的归属地接口查询 IP 地址。

        :param ip: 待查询的 IP 地址
        :return: 响应 addr 的值；请求或解析失败时返回空字符串
        """
        try:
            r = RequestUtils().get_res(f"https://whois.pconline.com.cn/ipJson.jsp?json=true&ip={ip}")
            if r:
                location: JsonData = r.json().get("addr") or ''
                return location
        except Exception as err:
            print(str(err))
        return ""
