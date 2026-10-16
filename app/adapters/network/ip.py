import ipaddress
import socket
from urllib.parse import urlparse


class IpUtils:
    """提供 IP 地址、域名和内外网判断能力。"""

    @staticmethod
    def is_ipv4(ip: str) -> bool:
        """
        判断是不是ipv4
        """
        try:
            socket.inet_pton(socket.AF_INET, ip)
        except AttributeError:  # no inet_pton here,sorry
            try:
                socket.inet_aton(ip)
            except socket.error:
                return False
            return ip.count('.') == 3
        except socket.error:  # not a valid ip
            return False
        return True

    @staticmethod
    def is_ipv6(ip: str) -> bool:
        """
        判断是不是ipv6
        """
        try:
            socket.inet_pton(socket.AF_INET6, ip)
        except socket.error:  # not a valid ip
            return False
        return True

    @staticmethod
    def is_internal(hostname: str) -> bool:
        """
        判断一个 host 是否为内网地址，无法提取主机名时按外网处理
        """
        parsed_hostname = urlparse(hostname).hostname
        if not parsed_hostname:
            return False
        if IpUtils.is_ip(parsed_hostname):
            return IpUtils.is_private_ip(parsed_hostname)
        else:
            return IpUtils.is_internal_domain(parsed_hostname)

    @staticmethod
    def is_ip(addr: str) -> bool:
        """
        判断是不是ip
        """
        try:
            socket.inet_aton(addr)
            return True
        except socket.error:
            return False

    @staticmethod
    def is_internal_domain(domain: str) -> bool:
        """
        判断域名是否为内部域名
        """
        # 获取域名对应的 IP 地址
        try:
            ip = socket.gethostbyname(domain)
        except socket.error:
            return False

        # 判断 IP 地址是否属于内网 IP 地址范围
        return IpUtils.is_private_ip(ip)

    @staticmethod
    def is_private_ip(ip_str: str) -> bool:
        """
        判断是不是内网ip
        """
        try:
            return ipaddress.ip_address(ip_str.strip()).is_private
        except Exception as e:
            print(str(e))
            return False
