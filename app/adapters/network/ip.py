"""
基于系统套接字和标准库解析 IP，并提供兼容的内网地址判定。
"""

import ipaddress
import socket
from urllib.parse import urlparse


class IpUtils:
    """
    提供 IP 地址、域名和内外网判断能力。
    """

    @staticmethod
    def is_ipv4(ip: str) -> bool:
        """
        检查 IPv4 地址文本，系统不提供 inet_pton 时回退至 inet_aton 并检查分段。

        :param ip: 要检查的地址文本
        :return: 可解析为 IPv4 地址时返回 True
        """
        try:
            socket.inet_pton(socket.AF_INET, ip)
        # 部分平台没有 inet_pton，使用旧接口保留 IPv4 检查能力。
        except AttributeError:
            try:
                socket.inet_aton(ip)
            except socket.error:
                return False
            return ip.count('.') == 3
        except socket.error:
            return False
        return True

    @staticmethod
    def is_ipv6(ip: str) -> bool:
        """
        检查 IPv6 地址文本。

        :param ip: 要检查的地址文本
        :return: 系统可以将其解析为 IPv6 地址时返回 True
        """
        try:
            socket.inet_pton(socket.AF_INET6, ip)
        except socket.error:
            return False
        return True

    @staticmethod
    def is_internal(hostname: str) -> bool:
        """
        从 URL 提取主机名，并检查地址或域名解析结果是否为内网地址。

        :param hostname: 包含主机部分的 URL；无法提取主机名时按非内网处理
        :return: 主机被识别为私有地址时返回 True，否则返回 False
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
        使用系统 inet_aton 检查 IPv4 地址，沿用其对缩写格式的接受规则。

        :param addr: 待解析的 IPv4 文本；本方法不检查 IPv6
        :return: 系统可以解析时返回 True，否则返回 False
        """
        try:
            socket.inet_aton(addr)
            return True
        except socket.error:
            return False

    @staticmethod
    def is_internal_domain(domain: str) -> bool:
        """
        同步查询域名的单个 IPv4 地址并判断是否为私有地址。

        :param domain: 要提交给系统 DNS 的域名
        :return: 解析地址为私有地址时返回 True；解析失败时返回 False
        """
        try:
            ip = socket.gethostbyname(domain)
        except socket.error:
            return False

        return IpUtils.is_private_ip(ip)

    @staticmethod
    def is_private_ip(ip_str: str) -> bool:
        """
        按 Python 标准库的 is_private 规则检查 IPv4 或 IPv6 地址。

        :param ip_str: 地址文本，首尾空白会被去除
        :return: 标准库判定为私有地址时返回 True；无效地址返回 False
        """
        try:
            return ipaddress.ip_address(ip_str.strip()).is_private
        except Exception as e:
            print(str(e))
            return False
