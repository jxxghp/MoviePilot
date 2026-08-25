"""
doh函数的实现。
author: https://github.com/C5H12O5/syno-videoinfo-plugin
"""
import base64
import json
import socket
import struct
import urllib
import urllib.request
from concurrent.futures import as_completed
from threading import Lock
from typing import Dict, Optional

from app.foundation.singleton import Singleton
from app.runtime.execution import OwnedThreadPoolExecutor, submit_with_context
from app.runtime.log import logger
from app.runtime.reload import ConfigReloadMixin
from app.runtime.settings import get_runtime_setting

# DoH 关闭时需要释放线程池；保持惰性创建可避免未启用 DoH 时占用进程级资源
_executor: Optional[OwnedThreadPoolExecutor] = None
_executor_lock = Lock()
_doh_enabled = False
_DOH_EXECUTOR_STOP_TIMEOUT_SECONDS = 10.0

# 定义默认的DoH配置
_doh_timeout = 5
_doh_cache: Dict[str, str] = {}
_doh_lock = Lock()
# 保存原始的 socket.getaddrinfo 方法
_orig_getaddrinfo = socket.getaddrinfo


def _doh_setting(key: str):
    """读取 DoH 热更新配置，组合根未装配时兼容旧 Settings。"""
    return get_runtime_setting(key)


def _get_executor_locked() -> OwnedThreadPoolExecutor:
    """在持有执行器锁时按需获取 DoH 查询线程池。"""
    global _executor
    if _executor is None:
        _executor = OwnedThreadPoolExecutor()
    return _executor


def enable_doh(enable: bool) -> bool:
    """
    对 socket.getaddrinfo 进行补丁。

    :param enable: 是否启用 DoH 解析
    :return: 状态切换成功时返回 True；旧 executor 未收敛时返回 False
    """

    global _doh_enabled, _executor

    def _patched_getaddrinfo(host: str, *args, **kwargs):
        """
        socket.getaddrinfo的补丁版本。
        """
        if host not in _doh_setting("DOH_DOMAINS").split(","):
            return _orig_getaddrinfo(host, *args, **kwargs)
        # 检查主机是否已解析
        with _doh_lock:
            ip = _doh_cache.get(host, None)
        if ip is not None:
            logger.info(f"已解析 [{host}] 为 [{ip}] (缓存)")
            return _orig_getaddrinfo(ip, *args, **kwargs)
        # 使用DoH解析主机
        with _executor_lock:
            if not _doh_enabled:
                return _orig_getaddrinfo(host, *args, **kwargs)
            executor = _get_executor_locked()
            # 一次解析的任务必须在同一临界区提交完，避免关闭过程中部分任务落入新线程池
            futures = [
                submit_with_context(executor, _doh_query, resolver, host)
                for resolver in _doh_setting("DOH_RESOLVERS").split(",")
            ]
        for future in as_completed(futures):
            ip = future.result()
            if ip is not None:
                logger.info(f"已解析 [{host}] 为 [{ip}]")
                # 关闭可能在查询等待期间恢复系统 DNS；关闭后的结果不得回填到下一轮配置。
                with _executor_lock:
                    cache_allowed = _doh_enabled
                if cache_allowed:
                    with _doh_lock:
                        _doh_cache[host] = ip
                host = ip
                break
        return _orig_getaddrinfo(host, *args, **kwargs)

    with _executor_lock:
        if enable and _executor is not None and not _executor.accepting:
            # 上一轮 shutdown 超时后必须继续持有原 owner；只有真实收敛才能替换执行器。
            if not _executor.shutdown_bounded(timeout=0.0):
                _doh_enabled = False
                socket.getaddrinfo = _orig_getaddrinfo
                return False
            _executor = None
        _doh_enabled = enable
        socket.getaddrinfo = _patched_getaddrinfo if enable else _orig_getaddrinfo
        return True


class DohHelper(ConfigReloadMixin, metaclass=Singleton):
    """
    DoH帮助类，用于处理DNS over HTTPS解析。
    """
    CONFIG_WATCH = {"DOH_ENABLE", "DOH_DOMAINS", "DOH_RESOLVERS"}

    def __init__(self) -> None:
        """按当前配置安装或移除 DoH 解析器。"""
        enable_doh(_doh_setting("DOH_ENABLE"))

    def on_config_changed(self) -> None:
        """配置变化时清理缓存并重新应用 DoH 状态。"""
        if not _doh_setting("DOH_ENABLE"):
            if not self.shutdown():
                logger.error("DoH配置关闭后查询线程池未在预算内收敛")
            return
        with _doh_lock:
            # DOH配置有变动的情况下，清空缓存
            _doh_cache.clear()
        if not enable_doh(True):
            logger.error("DoH查询线程池尚未收敛，暂不重新启用")

    def get_reload_name(self) -> str:
        """返回 DoH 配置重载名称。"""
        return 'DoH'

    def shutdown(
            self,
            timeout: float = _DOH_EXECUTOR_STOP_TIMEOUT_SECONDS,
    ) -> bool:
        """
        恢复系统 DNS 并有限等待 DoH 查询线程池。

        :param timeout: 等待已接受查询和 worker 终止的最长秒数
        :return: 查询线程池真实终止时返回 True，否则返回 False
        """
        global _executor, _doh_enabled
        with _executor_lock:
            _doh_enabled = False
            socket.getaddrinfo = _orig_getaddrinfo
            executor = _executor
            converged = executor is None or executor.shutdown_bounded(timeout=timeout)
            if converged and _executor is executor:
                _executor = None
        with _doh_lock:
            _doh_cache.clear()
        return converged


def _doh_query(resolver: str, host: str) -> Optional[str]:
    """
    使用给定的DoH解析器查询给定主机的IP地址。
    """

    # 构造DNS查询消息（RFC 1035）
    header = b"".join(
        [
            b"\x00\x00",  # ID: 0
            b"\x01\x00",  # FLAGS: 标准递归查询
            b"\x00\x01",  # QDCOUNT: 1
            b"\x00\x00",  # ANCOUNT: 0
            b"\x00\x00",  # NSCOUNT: 0
            b"\x00\x00",  # ARCOUNT: 0
        ]
    )
    question = b"".join(
        [
            b"".join(
                [
                    struct.pack("B", len(item)) + item.encode("utf-8")
                    for item in host.split(".")
                ]
            )
            + b"\x00",  # QNAME: 域名序列
            b"\x00\x01",  # QTYPE: A
            b"\x00\x01",  # QCLASS: IN
        ]
    )
    message = header + question

    try:
        # 发送GET请求到DoH解析器（RFC 8484）
        b64message = base64.b64encode(message).decode("utf-8").rstrip("=")
        url = f"https://{resolver}/dns-query?dns={b64message}"
        headers = {"Content-Type": "application/dns-message"}
        logger.debug(f"DoH请求: {url}")

        request = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(request, timeout=_doh_timeout) as response:
            logger.debug(f"解析器({resolver})响应: {response.status}")
            if response.status != 200:
                return None
            resp_body = response.read()

        # 解析DNS响应消息（RFC 1035）
        # name（压缩）:2 + type:2 + class:2 + ttl:4 + rdlength:2 = 12字节
        first_rdata_start = len(header) + len(question) + 12
        # rdata（A记录）= 4字节
        first_rdata_end = first_rdata_start + 4
        # 将rdata转换为IP地址
        return socket.inet_ntoa(resp_body[first_rdata_start:first_rdata_end])
    except Exception as e:
        logger.error(f"解析器({resolver})请求错误: {e}")
        return None


def doh_query_json(resolver: str, host: str) -> Optional[str]:
    """
    使用给定的DoH解析器查询给定主机的IP地址。
    """
    url = f"https://{resolver}/dns-query?name={host}&type=A"
    headers = {"Accept": "application/dns-json"}
    logger.debug(f"DoH请求: {url}")
    try:
        request = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(request, timeout=_doh_timeout) as response:
            logger.debug(f"解析器({resolver})响应: {response.status}")
            if response.status != 200:
                return None
            response_body = response.read().decode("utf-8")
            logger.debug(f"<== body: {response_body}")
            answer = json.loads(response_body)["Answer"]
            return answer[0]["data"]
    except Exception as e:
        logger.error(f"解析器({resolver})请求错误: {e}")
        return None
