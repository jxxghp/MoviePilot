"""
按配置为指定域名并行查询 DoH 服务，并管理系统 DNS 补丁的启停。

原始实现来源：https://github.com/C5H12O5/syno-videoinfo-plugin
"""
import base64
import json
import socket
import struct
import urllib
import urllib.request
from concurrent.futures import Future, as_completed
from threading import Lock
from time import monotonic
from typing import Any, Callable, Dict, Optional, cast

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
# 所有解析器都失败后的冷却秒数；冷却期内该域名直接使用系统 DNS，避免每次请求都等待解析超时并重复告警
_DOH_FAILURE_COOLDOWN_SECONDS = 300.0
_doh_cache: Dict[str, str] = {}
# 域名到解析失败冷却截止时间（monotonic 秒）的映射
_doh_failure_until: Dict[str, float] = {}
# 解析状态代次；每次清空缓存时递增，查询结果只能写回发起查询时的代次，避免旧配置结果回填
_doh_generation = 0
_doh_lock = Lock()
# DNS 报文固定头部长度（RFC 1035）
_DNS_HEADER_LENGTH = 12
# 资源记录在域名之后的固定字段长度：type:2 + class:2 + ttl:4 + rdlength:2
_DNS_RECORD_FIXED_LENGTH = 10
# A 记录类型与互联网类别
_DNS_TYPE_A = 1
_DNS_CLASS_IN = 1
# 保存原始的 socket.getaddrinfo 方法
_orig_getaddrinfo: Callable[..., Any] = socket.getaddrinfo


def _doh_setting(key: str) -> Any:
    """
    读取 DoH 热更新配置，组合根未装配时兼容旧 Settings。
    """
    return get_runtime_setting(key)


def _get_executor_locked() -> OwnedThreadPoolExecutor:
    """
    在持有执行器锁时按需获取 DoH 查询线程池。
    """
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

    def _patched_getaddrinfo(host: str, *args: Any, **kwargs: Any) -> Any:
        """
        socket.getaddrinfo的补丁版本。
        """
        if host not in _doh_setting("DOH_DOMAINS").split(","):
            return _orig_getaddrinfo(host, *args, **kwargs)
        # 检查主机是否已解析，或仍处于全部解析器失败后的冷却期
        with _doh_lock:
            ip = _doh_cache.get(host, None)
            cooling_down = _doh_failure_until.get(host, 0.0) > monotonic()
            generation = _doh_generation
        if ip is not None:
            logger.info(f"已解析 [{host}] 为 [{ip}] (缓存)")
            return _orig_getaddrinfo(ip, *args, **kwargs)
        # 使用DoH解析主机
        futures: list[Future[Optional[str]]] = []
        with _executor_lock:
            use_system_dns = not _doh_enabled or cooling_down
            if not use_system_dns:
                executor = _get_executor_locked()
                # 一次解析的任务必须在同一临界区提交完，避免关闭过程中部分任务落入新线程池
                futures = [
                    submit_with_context(executor, _doh_query, resolver, host)
                    for resolver in _doh_setting("DOH_RESOLVERS").split(",")
                ]
        # 系统 DNS 可能阻塞，必须在释放执行器锁后调用，避免拖住其他查询和有界关闭
        if use_system_dns:
            return _orig_getaddrinfo(host, *args, **kwargs)
        for future in as_completed(futures):
            ip = future.result()
            if ip is not None:
                break
        _remember_doh_result(host, ip, generation)
        return _orig_getaddrinfo(ip or host, *args, **kwargs)

    with _executor_lock:
        if enable and _executor is not None and not _executor.accepting:
            # 上一轮 shutdown 超时后必须继续持有原 owner；只有真实收敛才能替换执行器。
            if not _executor.shutdown_bounded(timeout=0.0):
                _doh_enabled = False
                socket.getaddrinfo = _orig_getaddrinfo
                return False
            _executor = None
        _doh_enabled = enable
        socket.getaddrinfo = cast(
            Any, _patched_getaddrinfo if enable else _orig_getaddrinfo
        )
        return True


def _remember_doh_result(host: str, ip: Optional[str], generation: int) -> None:
    """
    记录一次 DoH 并行查询的结果。

    成功时缓存解析地址并解除该域名的失败冷却；所有解析器都失败时进入冷却期，
    冷却期内该域名直接使用系统 DNS，并发查询只告警一次。
    DoH 已关闭或配置已变更时丢弃结果，避免旧配置的结果回填到下一轮配置。

    :param host: 查询的域名
    :param ip: 第一个成功解析器返回的 IPv4 地址；全部失败时为 None
    :param generation: 发起查询时的解析状态代次
    """
    if ip is not None:
        logger.info(f"已解析 [{host}] 为 [{ip}]")
    with _executor_lock:
        cache_allowed = _doh_enabled
    with _doh_lock:
        if not cache_allowed or generation != _doh_generation:
            return
        if ip is not None:
            _doh_cache[host] = ip
            _doh_failure_until.pop(host, None)
            return
        now = monotonic()
        if _doh_failure_until.get(host, 0.0) > now:
            # 并发查询已记录冷却，不再重复告警
            return
        _doh_failure_until[host] = now + _DOH_FAILURE_COOLDOWN_SECONDS
    logger.warning(
        f"所有 DoH 解析器均未能解析 [{host}]，"
        f"{_DOH_FAILURE_COOLDOWN_SECONDS:.0f} 秒内改用系统 DNS，请检查 DOH_RESOLVERS 是否可达"
    )


def _reset_doh_state() -> None:
    """
    清空解析缓存与失败冷却，并递增代次让仍在进行的旧查询结果失效。
    """
    global _doh_generation
    with _doh_lock:
        _doh_cache.clear()
        _doh_failure_until.clear()
        _doh_generation += 1


class DohHelper(ConfigReloadMixin, metaclass=Singleton):
    """
    DoH帮助类，用于处理DNS over HTTPS解析。
    """
    CONFIG_WATCH = {"DOH_ENABLE", "DOH_DOMAINS", "DOH_RESOLVERS"}

    def __init__(self) -> None:
        """
        按当前配置安装或移除 DoH 解析器。
        """
        enable_doh(_doh_setting("DOH_ENABLE"))

    def on_config_changed(self) -> None:
        """
        配置变化时清理缓存并重新应用 DoH 状态。
        """
        if not _doh_setting("DOH_ENABLE"):
            if not self.shutdown():
                logger.error("DoH配置关闭后查询线程池未在预算内收敛")
            return
        # DOH配置有变动的情况下，清空缓存和失败冷却，让新解析器立即生效
        _reset_doh_state()
        if not enable_doh(True):
            logger.error("DoH查询线程池尚未收敛，暂不重新启用")

    def get_reload_name(self) -> str:
        """
        返回 DoH 配置重载名称。
        """
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
        _reset_doh_state()
        return converged


def _doh_query(resolver: str, host: str) -> Optional[str]:
    """
    使用给定的DoH解析器查询给定主机的IP地址。
    """

    # 构造DNS查询消息（RFC 1035）
    header = b"".join(
        [
            # 查询标识固定为 0
            b"\x00\x00",
            # 标志位：标准递归查询
            b"\x01\x00",
            # 问题记录数为 1
            b"\x00\x01",
            # 应答记录数为 0
            b"\x00\x00",
            # 授权记录数为 0
            b"\x00\x00",
            # 附加记录数为 0
            b"\x00\x00",
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
            # 查询域名，末尾添加零字节
            + b"\x00",
            # 查询类型为 IPv4 地址记录
            b"\x00\x01",
            # 查询类别为互联网
            b"\x00\x01",
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

        ip = _parse_dns_a_record(resp_body)
        if ip is None:
            logger.debug(f"解析器({resolver})未返回 [{host}] 的 A 记录")
        return ip
    except Exception as e:
        # 单个解析器失败很常见，由调用方在全部失败时统一告警
        logger.debug(f"解析器({resolver})请求错误: {e}")
        return None


def _skip_dns_name(message: bytes, offset: int) -> Optional[int]:
    """
    跳过 DNS 报文中的一个域名字段。

    域名由若干长度前缀标签组成，以零字节或压缩指针结尾；保留的标签类型按无效报文处理。

    :param message: 完整 DNS 报文
    :param offset: 域名字段的起始位置
    :return: 域名之后第一个字节的位置；报文截断或标签无效时返回 None
    """
    while offset < len(message):
        length = message[offset]
        if length & 0xC0 == 0xC0:
            return offset + 2
        if length & 0xC0:
            return None
        if length == 0:
            return offset + 1
        offset += 1 + length
    return None


def _parse_dns_a_record(message: bytes) -> Optional[str]:
    """
    从 DNS 应答报文中取第一条 A 记录的 IPv4 地址（RFC 1035）。

    应答区可能先给出 CNAME 等其他类型的记录，必须逐条跳过，
    不能按固定偏移读取第一条记录，否则会把别名的域名字节误当成 IP。

    :param message: 解析器返回的完整 DNS 报文
    :return: 第一条 A 记录的点分十进制地址；响应码非成功、没有 A 记录或报文截断时返回 None
    """
    if len(message) < _DNS_HEADER_LENGTH or message[3] & 0x0F:
        return None
    question_count, answer_count = struct.unpack_from(">HH", message, 4)
    offset = _DNS_HEADER_LENGTH
    for _ in range(question_count):
        name_end = _skip_dns_name(message, offset)
        if name_end is None:
            return None
        # 跳过问题的 type 与 class
        offset = name_end + 4
    for _ in range(answer_count):
        name_end = _skip_dns_name(message, offset)
        if name_end is None or name_end + _DNS_RECORD_FIXED_LENGTH > len(message):
            return None
        record_type, record_class, _, data_length = struct.unpack_from(">HHIH", message, name_end)
        offset = name_end + _DNS_RECORD_FIXED_LENGTH
        if offset + data_length > len(message):
            return None
        if record_type == _DNS_TYPE_A and record_class == _DNS_CLASS_IN and data_length == 4:
            return socket.inet_ntoa(message[offset:offset + 4])
        offset += data_length
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
            answer = json.loads(response_body).get("Answer")
            if not isinstance(answer, list):
                return None
            # 应答可能先给出 CNAME 链，只取 A 记录的地址
            for record in answer:
                if isinstance(record, dict) and record.get("type") == _DNS_TYPE_A:
                    value = record.get("data")
                    if isinstance(value, str):
                        return value
            return None
    except Exception as e:
        # 单个解析器失败很常见，由调用方在全部失败时统一告警
        logger.debug(f"解析器({resolver})请求错误: {e}")
        return None
