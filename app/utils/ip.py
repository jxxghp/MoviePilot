import ipaddress
import socket
from urllib.parse import urlparse


class IpUtils:

    @staticmethod
    def is_ipv4(ip):
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
    def is_ipv6(ip):
        """
        判断是不是ipv6
        """
        try:
            socket.inet_pton(socket.AF_INET6, ip)
        except socket.error:  # not a valid ip
            return False
        return True

    @staticmethod
    def is_internal(hostname):
        """
        判断一个host是内网还是外网
        """
        hostname = urlparse(hostname).hostname
        if IpUtils.is_ip(hostname):
            return IpUtils.is_private_ip(hostname)
        else:
            return IpUtils.is_internal_domain(hostname)

    @staticmethod
    def is_ip(addr):
        """
        判断是不是ip
        """
        try:
            socket.inet_aton(addr)
            return True
        except socket.error:
            return False

    @staticmethod
    def is_internal_domain(domain):
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
    def is_private_ip(ip_str):
        """
        判断是不是内网ip
        """
        try:
            return ipaddress.ip_address(ip_str.strip()).is_private
        except Exception as e:
            print(e)
            return False
