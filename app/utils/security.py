import asyncio
import hmac
import ipaddress
import socket
import threading
from dataclasses import dataclass, field
from enum import Enum
from hashlib import sha256
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Union
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse

from anyio import Path as AsyncPath
from cachetools import TTLCache

from app.core.config import settings
from app.log import logger
from app.utils.coalesce import (
    CoalesceDecision,
    CoalesceSummary,
    EventCoalescer,
)


# DNS 解析结果缓存。
# 正向缓存 TTL 选择 120s，短于常见 CDN / fake-ip 的 DNS TTL，避免长期持有失效 IP；
# 负向缓存 TTL 选择 15s，避免临时解析失败把目标长时间拉黑。
_DNS_CACHE_MAXSIZE = 1024
_DNS_CACHE_TTL_POSITIVE = 120
_DNS_CACHE_TTL_NEGATIVE = 15
_dns_positive_cache: "TTLCache[str, List[ipaddress._BaseAddress]]" = TTLCache(
    maxsize=_DNS_CACHE_MAXSIZE, ttl=_DNS_CACHE_TTL_POSITIVE
)
_dns_negative_cache: "TTLCache[str, bool]" = TTLCache(
    maxsize=_DNS_CACHE_MAXSIZE, ttl=_DNS_CACHE_TTL_NEGATIVE
)
# 同步路径下保护 TTLCache 读写：`cachetools.TTLCache` 本身非线程安全。
# 锁只覆盖缓存读写，不包 `getaddrinfo`，避免把 DNS 查询本身串行化。
_dns_cache_lock = threading.Lock()
# 同 hostname 的并发异步解析去重：同一 hostname 首次未命中时建立锁，
# 后续并发请求 await 同一把锁，避免对同一目标重复发起 `getaddrinfo`。
_dns_inflight_locks: Dict[str, asyncio.Lock] = {}
_dns_inflight_meta_lock = threading.Lock()


class UrlSafetyReason(str, Enum):
    """
    `evaluate_url_safety` 返回的诊断原因枚举。

    成员值为稳定的小写蛇形字符串，可直接作为日志字段或告警标签使用，
    扩展枚举时保留既有成员的取值，避免破坏下游聚合系统对原因的归类。
    """

    # 通过全部校验，URL 可被请求
    ALLOWED = "allowed"
    # 协议非 http/https，或 netloc 无效，或域名不在允许列表内
    DOMAIN_NOT_ALLOWED = "domain_not_allowed"
    # 已通过域名 allowlist，但 DNS 解析失败（无返回或抛错）
    DNS_RESOLUTION_FAILED = "dns_resolution_failed"
    # DNS 解析到至少一个非公网地址，且未配置 `allowed_private_ranges`
    NON_GLOBAL_DNS_RESULT = "non_global_dns_result"
    # 配置了 `allowed_private_ranges`，但仍存在不在允许网段内的解析结果
    MIXED_OR_DISALLOWED_PRIVATE_RESULT = "mixed_or_disallowed_private_result"


@dataclass(frozen=True)
class UrlSafetyDiagnosis:
    """
    URL 安全校验的结构化诊断结果，由 `evaluate_url_safety(_async)` 返回。

    `is_safe_url` 仅使用 `allowed` 字段；日志、告警、运维诊断需要细分原因或
    解析 IP 时通过本对象消费。字段约束：
    - `host` 仅在通过域名 allowlist 后才被填充；DOMAIN_NOT_ALLOWED 场景为 None。
    - `ips` 仅在执行过 DNS 阶段后才可能非空；不含纯字符串协议失败场景。
    - `matched_private_ranges` 仅在通过 `allowed_private_ranges` 放行时填充。
    """

    # 是否放行
    allowed: bool
    # 放行/拦截的具体原因
    reason: UrlSafetyReason
    # 通过 allowlist 后从 URL 解析出的 hostname，未通过时为 None
    host: Optional[str] = None
    # DNS 解析结果（含命中或未命中私网放行的 IP），格式化为字符串
    ips: List[str] = field(default_factory=list)
    # 命中允许放行的非公网网段，仅 `ALLOWED` 且走私网放行分支时非空
    matched_private_ranges: List[str] = field(default_factory=list)


def _resolve_addrinfo_to_ips(
    address_infos: Iterable,
) -> Optional[List[ipaddress._BaseAddress]]:
    """
    将 `socket.getaddrinfo` 返回的结果归一化为 IP 列表。

    任一条目无法解析为 IP 即视为异常情况，整体返回 None 让上层按"不安全目标"
    处理，避免出现"部分 IP 漏校验"的情况。
    """
    addresses: List[ipaddress._BaseAddress] = []
    for address_info in address_infos:
        try:
            addresses.append(ipaddress.ip_address(address_info[4][0]))
        except ValueError:
            return None
    return addresses or None


class SecurityUtils:
    _SIGNED_URL_PURPOSE = "image-proxy"

    @staticmethod
    def is_safe_path(base_path: Path, user_path: Path,
                     allowed_suffixes: Optional[Union[Set[str], List[str]]] = None) -> bool:
        """
        验证用户提供的路径是否在基准目录内，并检查文件类型是否合法，防止目录遍历攻击

        :param base_path: 基准目录，允许访问的根目录
        :param user_path: 用户提供的路径，需检查其是否位于基准目录内
        :param allowed_suffixes: 允许的文件后缀名集合，用于验证文件类型
        :return: 如果用户路径安全且位于基准目录内，且文件类型合法，返回 True；否则返回 False
        :raises Exception: 如果解析路径时发生错误，则捕获并记录异常
        """
        try:
            # resolve() 将相对路径转换为绝对路径，并处理符号链接和'..'
            base_path_resolved = base_path.resolve()
            user_path_resolved = user_path.resolve()

            # 检查用户路径是否在基准目录或基准目录的子目录内
            if base_path_resolved != user_path_resolved and base_path_resolved not in user_path_resolved.parents:
                return False

            if allowed_suffixes is not None:
                allowed_suffixes = set(allowed_suffixes)
                if user_path.suffix.lower() not in allowed_suffixes:
                    return False

            return True
        except Exception as e:
            logger.debug(f"Error occurred while validating paths: {e}")
            return False

    @staticmethod
    async def async_is_safe_path(base_path: AsyncPath, user_path: AsyncPath,
                                 allowed_suffixes: Optional[Union[Set[str], List[str]]] = None) -> bool:
        """
        异步验证用户提供的路径是否在基准目录内，并检查文件类型是否合法，防止目录遍历攻击

        :param base_path: 基准目录，允许访问的根目录
        :param user_path: 用户提供的路径，需检查其是否位于基准目录内
        :param allowed_suffixes: 允许的文件后缀名集合，用于验证文件类型
        :return: 如果用户路径安全且位于基准目录内，且文件类型合法，返回 True；否则返回 False
        :raises Exception: 如果解析路径时发生错误，则捕获并记录异常
        """
        try:
            # resolve() 将相对路径转换为绝对路径，并处理符号链接和'..'
            base_path_resolved = await base_path.resolve()
            user_path_resolved = await user_path.resolve()

            # 检查用户路径是否在基准目录或基准目录的子目录内
            if base_path_resolved != user_path_resolved and base_path_resolved not in user_path_resolved.parents:
                return False

            if allowed_suffixes is not None:
                allowed_suffixes = set(allowed_suffixes)
                if user_path.suffix.lower() not in allowed_suffixes:
                    return False

            return True
        except Exception as e:
            logger.debug(f"Error occurred while validating paths: {e}")
            return False

    @staticmethod
    def _literal_ip(hostname: str) -> Optional[ipaddress._BaseAddress]:
        """
        若 hostname 是字面量 IP（含 IPv6 的 `[::1]` 形式）则返回 IP 对象，否则 None。
        """
        if not hostname:
            return None
        candidate = hostname
        if candidate.startswith("[") and candidate.endswith("]"):
            candidate = candidate[1:-1]
        try:
            return ipaddress.ip_address(candidate)
        except ValueError:
            return None

    @staticmethod
    def _cache_lookup(hostname: str) -> tuple[bool, Optional[List[ipaddress._BaseAddress]]]:
        """
        在 TTL 缓存中查找 hostname，返回 (是否命中, 命中值)。

        命中值为 `None` 表示命中负向缓存（先前解析失败）。
        """
        with _dns_cache_lock:
            cached = _dns_positive_cache.get(hostname)
            if cached is not None:
                return True, cached
            if hostname in _dns_negative_cache:
                return True, None
        return False, None

    @staticmethod
    def _cache_store(
        hostname: str, addresses: Optional[List[ipaddress._BaseAddress]]
    ) -> None:
        """
        将解析结果写入对应的正向/负向缓存。
        """
        with _dns_cache_lock:
            if addresses is None:
                _dns_negative_cache[hostname] = True
            else:
                _dns_positive_cache[hostname] = addresses

    @staticmethod
    def _hostname_addresses(hostname: str) -> Optional[List[ipaddress._BaseAddress]]:
        """
        同步解析主机名并返回全部 IP 地址，结果走 TTL 缓存。

        字面量 IP 直接返回自身；DNS 解析失败或结果异常时返回 None，由上层按
        不安全目标处理。async 调用方应使用 `_hostname_addresses_async`。
        """
        if not hostname:
            return None
        literal = SecurityUtils._literal_ip(hostname)
        if literal is not None:
            return [literal]

        hit, value = SecurityUtils._cache_lookup(hostname)
        if hit:
            return value

        try:
            address_infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
        except socket.gaierror:
            SecurityUtils._cache_store(hostname, None)
            return None
        addresses = _resolve_addrinfo_to_ips(address_infos)
        SecurityUtils._cache_store(hostname, addresses)
        return addresses

    @staticmethod
    def _get_inflight_lock(hostname: str) -> asyncio.Lock:
        """
        取得 hostname 对应的 in-flight 锁，不存在则按需创建。

        用 `threading.Lock` 保护字典写入，避免多个事件循环线程并发创建出多把锁
        破坏去重语义；锁本身是 `asyncio.Lock`，归属当前事件循环。
        """
        with _dns_inflight_meta_lock:
            lock = _dns_inflight_locks.get(hostname)
            if lock is None:
                lock = asyncio.Lock()
                _dns_inflight_locks[hostname] = lock
            return lock

    @staticmethod
    def _release_inflight_lock(hostname: str, lock: asyncio.Lock) -> None:
        """
        请求结束后清理 in-flight 锁，避免长期持有大量已闲置的 `asyncio.Lock`。

        仅当字典中登记的仍是当前 lock，且 `lock.locked()` 为 False 时才删除。
        `asyncio.Lock` 公平 FIFO：持有者释放后若仍有等待者，锁会立刻被下一个
        等待者接走、`locked()` 重新变为 True，因此该守卫可同时排除"仍有持有者"
        与"刚被等待者接走"两种情况，避免误删后续协程仍在使用的字典条目。
        """
        with _dns_inflight_meta_lock:
            current = _dns_inflight_locks.get(hostname)
            if current is lock and not lock.locked():
                _dns_inflight_locks.pop(hostname, None)

    @staticmethod
    async def _hostname_addresses_async(
        hostname: str,
    ) -> Optional[List[ipaddress._BaseAddress]]:
        """
        异步解析主机名并返回全部 IP 地址，与同步版本共用同一份 TTL 缓存。

        通过事件循环的默认线程池执行 `getaddrinfo`，不阻塞 asyncio 事件循环；
        同 hostname 的并发未命中请求通过 in-flight 锁去重，只发起一次 DNS 查询。
        """
        if not hostname:
            return None
        literal = SecurityUtils._literal_ip(hostname)
        if literal is not None:
            return [literal]

        hit, value = SecurityUtils._cache_lookup(hostname)
        if hit:
            return value

        lock = SecurityUtils._get_inflight_lock(hostname)
        try:
            async with lock:
                # 等到锁后再查一次缓存，前一个持锁者可能已经回填结果
                hit, value = SecurityUtils._cache_lookup(hostname)
                if hit:
                    return value

                loop = asyncio.get_running_loop()
                try:
                    address_infos = await loop.getaddrinfo(
                        hostname, None, type=socket.SOCK_STREAM
                    )
                except socket.gaierror:
                    SecurityUtils._cache_store(hostname, None)
                    return None
                addresses = _resolve_addrinfo_to_ips(address_infos)
                SecurityUtils._cache_store(hostname, addresses)
                return addresses
        finally:
            # 必须在 `async with` 释放锁之后再清理字典：`_release_inflight_lock`
            # 以 `not lock.locked()` 为清理守卫，持锁状态下调用会跳过 pop。
            SecurityUtils._release_inflight_lock(hostname, lock)

    @staticmethod
    def _addresses_all_global(
        addresses: Optional[List[ipaddress._BaseAddress]],
    ) -> bool:
        """
        判断解析结果是否全部为公网地址（空列表/None 视为非公网）。
        """
        if not addresses:
            return False
        return all(address.is_global for address in addresses)

    @staticmethod
    def _is_global_hostname(hostname: str) -> bool:
        """
        判断主机名解析结果是否全部为公网地址（同步版本）。

        图片代理会访问用户可控的 URL，这里必须在 allowlist 命中前后都排除
        私有、回环、链路本地、保留地址等非公网目标，避免通过 DNS 或字面量 IP
        绕过域名白名单访问内网服务。
        """
        return SecurityUtils._addresses_all_global(
            SecurityUtils._hostname_addresses(hostname)
        )

    @staticmethod
    async def _is_global_hostname_async(hostname: str) -> bool:
        """
        判断主机名解析结果是否全部为公网地址（异步版本）。语义与 `_is_global_hostname` 一致。
        """
        return SecurityUtils._addresses_all_global(
            await SecurityUtils._hostname_addresses_async(hostname)
        )

    @staticmethod
    def _parse_ip_networks(ranges: Optional[Iterable[str]]) -> List[ipaddress._BaseNetwork]:
        """
        解析用户配置的 IP/CIDR 网段。

        配置错误的条目会被忽略并写入 debug 日志，避免单个无效值导致所有图片代理
        校验失败。调用方仍然需要先完成域名白名单匹配，不能单独依赖该网段放行。
        """
        networks = []
        for value in ranges or []:
            if not value:
                continue
            try:
                networks.append(ipaddress.ip_network(str(value).strip(), strict=False))
            except ValueError:
                logger.debug(f"忽略无效的图片代理允许网段配置: {value}")
        return networks

    @staticmethod
    def _match_private_addresses(
        addresses: Optional[List[ipaddress._BaseAddress]],
        networks: List[ipaddress._BaseNetwork],
    ) -> Optional[tuple[List[ipaddress._BaseAddress], List[ipaddress._BaseNetwork]]]:
        """
        在已解析出的地址列表中匹配显式允许的非公网网段。

        所有解析地址都必须命中至少一个允许网段才放行；只要有一个 IP 落在允许
        网段外（或解析结果是全公网），就视为不匹配私网放行规则。
        """
        if not addresses or not networks:
            return None
        if all(address.is_global for address in addresses):
            return None

        matched_networks: List[ipaddress._BaseNetwork] = []
        for address in addresses:
            matched_for_address = [
                network for network in networks if address in network
            ]
            if not matched_for_address:
                return None
            matched_networks.extend(matched_for_address)
        return addresses, list(dict.fromkeys(matched_networks))

    @staticmethod
    def _is_allowed_private_hostname(
        hostname: str,
        allowed_private_ranges: Optional[Iterable[str]],
    ) -> Optional[tuple[List[ipaddress._BaseAddress], List[ipaddress._BaseNetwork]]]:
        """
        返回主机名命中的显式允许非公网地址和网段（同步版本）。

        该能力只用于图片代理的受控例外，例如 TUN fake-ip 或内网 CDN。必须由
        `is_safe_url` 先完成域名 allowlist 校验后再调用，避免把任意用户 URL
        变成 SSRF 绕过入口。
        """
        networks = SecurityUtils._parse_ip_networks(allowed_private_ranges)
        if not networks:
            return None
        return SecurityUtils._match_private_addresses(
            SecurityUtils._hostname_addresses(hostname), networks
        )

    @staticmethod
    async def _is_allowed_private_hostname_async(
        hostname: str,
        allowed_private_ranges: Optional[Iterable[str]],
    ) -> Optional[tuple[List[ipaddress._BaseAddress], List[ipaddress._BaseNetwork]]]:
        """
        `_is_allowed_private_hostname` 的异步版本，语义保持一致。
        """
        networks = SecurityUtils._parse_ip_networks(allowed_private_ranges)
        if not networks:
            return None
        return SecurityUtils._match_private_addresses(
            await SecurityUtils._hostname_addresses_async(hostname), networks
        )

    @staticmethod
    def _url_signature_payload(url: str, purpose: str) -> bytes:
        """
        构造 URL 签名载荷。

        签名覆盖用途与完整 URL，确保同一个签名不能挪用到其它代理用途或其它 URL。
        """
        return f"{purpose}\n{url}".encode("utf-8")

    @staticmethod
    def _sign_url_payload(url: str, purpose: str) -> str:
        """
        使用 RESOURCE_SECRET_KEY 对 URL 签名载荷生成 HMAC。

        相同 `(url, purpose, RESOURCE_SECRET_KEY)` 组合在进程生命周期内输出
        完全一致；签名的失效边界绑定在 `RESOURCE_SECRET_KEY` 上，进程重启
        或显式轮换密钥时所有旧签名一起作废。
        """
        return hmac.new(
            settings.RESOURCE_SECRET_KEY.encode("utf-8"),
            SecurityUtils._url_signature_payload(url, purpose),
            sha256,
        ).hexdigest()

    @staticmethod
    def strip_url_signature(url: str) -> str:
        """
        移除 URL fragment 中的代理签名信息，得到真正要请求的地址。

        图片代理签名放在 fragment 中，浏览器会把它传给 MoviePilot，但 HTTP
        客户端请求媒体服务器前不能把这些内部参数带过去。
        """
        if not url:
            return url
        parsed_url = urlparse(url)
        return urlunparse(parsed_url._replace(fragment=""))

    @staticmethod
    def sign_url(
        url: str,
        purpose: str = _SIGNED_URL_PURPOSE,
    ) -> str:
        """
        给服务端返回的资源 URL 添加稳定签名。

        签名作为 `/system/img` 代理放行私网图片 URL 的能力凭证：图片代理默认
        拒绝解析到非公网地址的 URL（防 SSRF），合法媒体服务器 URL 必须由后端
        预先签名后才能跳过该限制。

        签名为 `(url, purpose, RESOURCE_SECRET_KEY)` 的确定性 HMAC，**不带
        过期时间**：相同 URL 多次调用结果完全一致，让浏览器与 Service Worker
        的缓存能稳定命中；失效边界由 `RESOURCE_SECRET_KEY` 控制——进程重启
        自动重生成、或者运维显式轮换后所有历史签名一起作废。
        """
        if not url:
            return url
        parsed_url = urlparse(url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            return url
        clean_url = SecurityUtils.strip_url_signature(url)
        signature = SecurityUtils._sign_url_payload(clean_url, purpose)
        fragment = urlencode(
            {
                "mp_sig": signature,
                "mp_purpose": purpose,
            }
        )
        return urlunparse(urlparse(clean_url)._replace(fragment=fragment))

    @staticmethod
    def verify_signed_url(
        url: str,
        purpose: str = _SIGNED_URL_PURPOSE,
    ) -> Optional[str]:
        """
        验证 URL fragment 中的代理签名，成功时返回去签名后的真实 URL。

        签名只校验 `(url, purpose, RESOURCE_SECRET_KEY)`，密钥轮换/进程重启
        后旧签名自动失效。
        """
        if not url:
            return None
        parsed_url = urlparse(url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            return None
        fragment_params = dict(parse_qsl(parsed_url.fragment, keep_blank_values=True))
        signature = fragment_params.get("mp_sig")
        signed_purpose = fragment_params.get("mp_purpose")
        if not signature or signed_purpose != purpose:
            return None

        clean_url = SecurityUtils.strip_url_signature(url)
        expected_signature = SecurityUtils._sign_url_payload(clean_url, purpose)
        if not hmac.compare_digest(signature, expected_signature):
            return None
        return clean_url

    @staticmethod
    def _check_url_allowlist(
        url: str,
        allowed_domains: Union[Set[str], List[str]],
        strict: bool,
    ) -> Optional[str]:
        """
        执行"协议 + netloc + 域名白名单"前置校验，命中返回 hostname，未命中返回 None。

        DNS 校验（SSRF 防御）由调用方自行接续，本方法不发起 DNS 查询。
        """
        try:
            parsed_url = urlparse(url)
        except Exception as e:  # noqa: BLE001 - 任何解析异常都视为不安全 URL
            logger.debug(f"Error occurred while validating URL: {e}")
            return None

        # 如果 URL 没有包含有效的 scheme，或者无法从中提取到有效的 netloc，则认为该 URL 是无效的
        if not parsed_url.scheme or not parsed_url.netloc:
            return None
        # 仅允许 http 或 https 协议
        if parsed_url.scheme not in {"http", "https"}:
            return None

        # 获取完整的 netloc（包括 IP 和端口）并转换为小写
        netloc = parsed_url.netloc.lower()
        if not netloc:
            return None

        # 检查每个允许的域名
        normalized_allowed = {d.lower() for d in allowed_domains}
        domain_allowed = False
        for domain in normalized_allowed:
            parsed_allowed_url = urlparse(domain)
            allowed_netloc = parsed_allowed_url.netloc or parsed_allowed_url.path

            if strict:
                # 严格模式下，要求完全匹配域名和端口
                if netloc == allowed_netloc:
                    domain_allowed = True
                    break
            else:
                # 非严格模式下，允许子域名匹配
                if netloc == allowed_netloc or netloc.endswith("." + allowed_netloc):
                    domain_allowed = True
                    break

        if not domain_allowed:
            return None
        return parsed_url.hostname or ""

    @staticmethod
    def _log_private_range_allowed(
        url: str,
        match: tuple[List[ipaddress._BaseAddress], List[ipaddress._BaseNetwork]],
    ) -> None:
        """
        记录"图片代理允许访问配置的非公网网段"放行日志，便于运维排查。
        """
        addresses, matched_networks = match
        logger.debug(
            "图片代理允许访问配置的非公网网段: "
            f"url={url}, ips={','.join(map(str, addresses))}, "
            f"ranges={','.join(map(str, matched_networks))}"
        )

    @staticmethod
    def is_safe_url(
        url: str,
        allowed_domains: Union[Set[str], List[str]],
        strict: bool = False,
        block_private: bool = False,
        allowed_private_ranges: Optional[Iterable[str]] = None,
    ) -> bool:
        """
        验证 URL 是否在允许的域名列表中，包括带有端口的域名（同步版本）。

        :param url: 需要验证的 URL
        :param allowed_domains: 允许的域名集合，域名可以包含端口
        :param strict: 是否严格匹配一级域名（默认 False，允许多级域名）
        :param block_private: 是否拦截解析到非公网地址的 URL，防止 SSRF
        :param allowed_private_ranges: 域名命中后额外允许的非公网 IP/CIDR 网段
        :return: URL 合法且通过安全校验时返回 True，否则返回 False

        校验细节与失败原因由 `evaluate_url_safety` 返回；本方法只暴露布尔结果，
        作为只关心通过/拒绝判断的调用方的最薄入口。`block_private=True` 时会
        同步调用 `getaddrinfo`；async 上下文请改用 `is_safe_url_async`。
        """
        return SecurityUtils.evaluate_url_safety(
            url,
            allowed_domains,
            strict=strict,
            block_private=block_private,
            allowed_private_ranges=allowed_private_ranges,
        ).allowed

    @staticmethod
    async def is_safe_url_async(
        url: str,
        allowed_domains: Union[Set[str], List[str]],
        strict: bool = False,
        block_private: bool = False,
        allowed_private_ranges: Optional[Iterable[str]] = None,
    ) -> bool:
        """
        判定 URL 是否在允许的域名列表中，包括带有端口的域名。

        DNS 解析通过事件循环线程池执行，并复用 TTL 缓存，不阻塞调用方所在的
        事件循环。参数与返回值含义同 `is_safe_url`；需要失败原因/解析 IP
        等结构化信息时调用 `evaluate_url_safety_async`。
        """
        diagnosis = await SecurityUtils.evaluate_url_safety_async(
            url,
            allowed_domains,
            strict=strict,
            block_private=block_private,
            allowed_private_ranges=allowed_private_ranges,
        )
        return diagnosis.allowed

    @staticmethod
    def evaluate_url_safety(
        url: str,
        allowed_domains: Union[Set[str], List[str]],
        strict: bool = False,
        block_private: bool = False,
        allowed_private_ranges: Optional[Iterable[str]] = None,
    ) -> "UrlSafetyDiagnosis":
        """
        在 `is_safe_url` 的判定路径上输出结构化诊断结果（同步版本）。

        与 `is_safe_url` 共用同一套校验顺序：协议/域名 allowlist → 可选 DNS 解析
        → 可选非公网放行匹配；本方法额外返回失败原因、解析到的 IP 列表和命中的
        私网网段，供日志与告警渲染消费。校验中遇到未预期异常时按默认拒绝原则
        归类为 `DOMAIN_NOT_ALLOWED`，避免任何解析路径漏过 SSRF 校验。
        """
        try:
            hostname = SecurityUtils._check_url_allowlist(url, allowed_domains, strict)
            if hostname is None:
                return UrlSafetyDiagnosis(
                    allowed=False,
                    reason=UrlSafetyReason.DOMAIN_NOT_ALLOWED,
                )
            if not block_private:
                return UrlSafetyDiagnosis(
                    allowed=True,
                    reason=UrlSafetyReason.ALLOWED,
                    host=hostname,
                )
            addresses = SecurityUtils._hostname_addresses(hostname)
            return SecurityUtils._diagnose_resolved_addresses(
                url, hostname, addresses, allowed_private_ranges
            )
        except Exception as e:  # noqa: BLE001 - 默认拒绝，避免漏过 SSRF 校验
            logger.debug(f"Error occurred while validating URL: {e}")
            return UrlSafetyDiagnosis(
                allowed=False,
                reason=UrlSafetyReason.DOMAIN_NOT_ALLOWED,
            )

    @staticmethod
    async def evaluate_url_safety_async(
        url: str,
        allowed_domains: Union[Set[str], List[str]],
        strict: bool = False,
        block_private: bool = False,
        allowed_private_ranges: Optional[Iterable[str]] = None,
    ) -> "UrlSafetyDiagnosis":
        """
        输出与 `evaluate_url_safety` 完全一致的结构化诊断结果。

        DNS 解析通过事件循环线程池执行，并复用 TTL 缓存，不阻塞调用方所在的
        事件循环；校验顺序、字段含义、异常归类均与同步版本相同。
        """
        try:
            hostname = SecurityUtils._check_url_allowlist(url, allowed_domains, strict)
            if hostname is None:
                return UrlSafetyDiagnosis(
                    allowed=False,
                    reason=UrlSafetyReason.DOMAIN_NOT_ALLOWED,
                )
            if not block_private:
                return UrlSafetyDiagnosis(
                    allowed=True,
                    reason=UrlSafetyReason.ALLOWED,
                    host=hostname,
                )
            addresses = await SecurityUtils._hostname_addresses_async(hostname)
            return SecurityUtils._diagnose_resolved_addresses(
                url, hostname, addresses, allowed_private_ranges
            )
        except Exception as e:  # noqa: BLE001 - 默认拒绝，避免漏过 SSRF 校验
            logger.debug(f"Error occurred while validating URL: {e}")
            return UrlSafetyDiagnosis(
                allowed=False,
                reason=UrlSafetyReason.DOMAIN_NOT_ALLOWED,
            )

    @staticmethod
    async def is_safe_image_url_async(
        url: str,
        allowed_domains: Union[Set[str], List[str]],
        allowed_private_ranges: Optional[Iterable[str]] = None,
    ) -> bool:
        """
        判定 URL 是否可作为图片代理请求目标。

        校验顺序：协议 + 域名 allowlist + DNS SSRF 拦截 + 非公网放行匹配；标准
        校验失败时再用 `verify_signed_url` 兜底，允许后端预签名的媒体服务器
        URL 跳过私网拦截。两者皆失败才视为拒绝。

        拒绝路径会输出结构化阻断日志：单次拦截立即打印一条 warning，同
        `(host, reason)` 的连续命中在 `_IMAGE_PROXY_BLOCK_LOG_WINDOW_SECONDS`
        窗口内合并为一条聚合摘要，避免媒体详情页一次请求把日志刷爆。日志字段
        范围严格限定为 URL、host、reason、解析 IP 与允许网段配置；cookies、
        签名串、token、请求头等敏感材料一律不进入日志。
        """
        diagnosis = await SecurityUtils.evaluate_url_safety_async(
            url,
            allowed_domains,
            block_private=True,
            allowed_private_ranges=allowed_private_ranges,
        )
        if diagnosis.allowed:
            return True
        if SecurityUtils.verify_signed_url(url) is not None:
            return True
        await _emit_image_proxy_block_warning(
            url=url,
            diagnosis=diagnosis,
            signature_carried=_url_carries_signature(url),
            allowed_private_ranges=allowed_private_ranges,
        )
        return False

    @staticmethod
    def _diagnose_resolved_addresses(
        url: str,
        hostname: str,
        addresses: Optional[List[ipaddress._BaseAddress]],
        allowed_private_ranges: Optional[Iterable[str]],
    ) -> "UrlSafetyDiagnosis":
        """
        对已完成 DNS 解析的地址列表执行非公网放行判断，并归一化诊断结果。

        - 地址列表为空/None：视为 DNS 不可信，拒绝并标记 `DNS_RESOLUTION_FAILED`。
        - 全部公网地址：直接放行。
        - 存在非公网地址且未配置允许网段：拒绝并标记 `NON_GLOBAL_DNS_RESULT`，
          供日志附带"如使用 fake-ip 需要配置 IMAGE_PROXY_ALLOWED_PRIVATE_RANGES"
          的提示。
        - 存在非公网地址且配置了允许网段但未全部命中：拒绝并标记
          `MIXED_OR_DISALLOWED_PRIVATE_RESULT`，提示存在不允许的解析结果。
        - 全部命中允许网段：放行并附带命中的 IP 与网段，由
          `_log_private_range_allowed` 输出排查日志。
        """
        if not addresses:
            return UrlSafetyDiagnosis(
                allowed=False,
                reason=UrlSafetyReason.DNS_RESOLUTION_FAILED,
                host=hostname,
            )
        if SecurityUtils._addresses_all_global(addresses):
            return UrlSafetyDiagnosis(
                allowed=True,
                reason=UrlSafetyReason.ALLOWED,
                host=hostname,
                ips=[str(addr) for addr in addresses],
            )
        networks = SecurityUtils._parse_ip_networks(allowed_private_ranges)
        if not networks:
            return UrlSafetyDiagnosis(
                allowed=False,
                reason=UrlSafetyReason.NON_GLOBAL_DNS_RESULT,
                host=hostname,
                ips=[str(addr) for addr in addresses],
            )
        match = SecurityUtils._match_private_addresses(addresses, networks)
        if match is None:
            return UrlSafetyDiagnosis(
                allowed=False,
                reason=UrlSafetyReason.MIXED_OR_DISALLOWED_PRIVATE_RESULT,
                host=hostname,
                ips=[str(addr) for addr in addresses],
            )
        matched_addresses, matched_networks = match
        SecurityUtils._log_private_range_allowed(url, match)
        return UrlSafetyDiagnosis(
            allowed=True,
            reason=UrlSafetyReason.ALLOWED,
            host=hostname,
            ips=[str(addr) for addr in matched_addresses],
            matched_private_ranges=[str(net) for net in matched_networks],
        )

    @staticmethod
    def sanitize_url_path(url: str, max_length: int = 120) -> str:
        """
        将 URL 的路径部分进行编码，确保合法字符，并对路径长度进行压缩处理（如果超出最大长度）

        :param url: 需要处理的 URL
        :param max_length: 路径允许的最大长度，超出时进行压缩
        :return: 处理后的路径字符串
        """
        # 解析 URL，获取路径部分
        parsed_url = urlparse(url)
        path = parsed_url.path.lstrip("/")

        # 对路径中的特殊字符进行编码
        safe_path = quote(path)

        # 如果路径过长，进行压缩处理
        if len(safe_path) > max_length:
            # 使用 SHA-256 对路径进行哈希，取前 16 位作为压缩后的路径
            hash_value = sha256(safe_path.encode()).hexdigest()[:16]
            # 使用哈希值代替过长的路径，同时保留文件扩展名
            file_extension = Path(safe_path).suffix.lower() if Path(safe_path).suffix else ""
            safe_path = f"compressed_{hash_value}{file_extension}"

        return safe_path


# 图片代理阻断日志聚合窗口（秒）。媒体详情页一次请求会批量触发同 host/同原因的拦截，
# 按 (host, reason) 合并后只输出首条 warning + 窗口结束的聚合摘要，避免日志刷屏。
_IMAGE_PROXY_BLOCK_LOG_WINDOW_SECONDS = 60.0

# fake-ip / 旁路 DNS 用户最常因 IMAGE_PROXY_ALLOWED_PRIVATE_RANGES 未配置而踩坑，
# 在 reason=NON_GLOBAL_DNS_RESULT 且当前未配置允许网段时随 warning 一起输出，指向正确的修复开关。
_IMAGE_PROXY_FAKEIP_HINT = (
    "提示：若使用 fake-ip / 旁路 DNS（常见网段 198.18.0.0/15、100.64.0.0/10），"
    "请将对应网段加入 IMAGE_PROXY_ALLOWED_PRIVATE_RANGES"
)

# URL fragment 中实际携带代理签名但校验失败时附在 reason 末尾的标记。
# 仅起标识作用，签名串本身不写入日志，避免泄露签名材料。
_INVALID_SIGNATURE_TAG = "invalid_signature"


def _url_carries_signature(url: str) -> bool:
    """
    判断 URL 是否在 fragment 中显式携带代理签名参数 `mp_sig`。

    仅做轻量字符串匹配，避免对普通图片 URL 跑完整签名校验路径；未携带签名
    的外链不会触发 `invalid_signature` 标记，避免阻断日志误导未签名调用方。
    """
    if not url:
        return False
    fragment_start = url.find("#")
    if fragment_start < 0:
        return False
    return "mp_sig=" in url[fragment_start + 1:]


def _format_image_proxy_block_warning(
    *,
    url: str,
    reason: str,
    host: Optional[str],
    ips: List[str],
    allowed_private_ranges: List[str],
    hint: Optional[str],
) -> str:
    """
    渲染图片代理首条阻断 warning 文案。

    字段范围严格限定为 URL、host、reason、IP 与允许网段配置；hint 仅在
    reason 与配置缺失同时满足时由调用方填充。其余敏感材料（cookies、签名
    串、token、请求头）不允许进入该日志路径。
    """
    fields = [
        f"url={url}",
        f"reason={reason}",
        f"host={host or ''}",
        f"ips={','.join(ips)}",
        f"allowed_private_ranges={','.join(allowed_private_ranges)}",
    ]
    line = "Blocked unsafe image URL: " + ", ".join(fields)
    if hint:
        line = f"{line} | {hint}"
    return line


def _log_image_proxy_block_summary(summary: CoalesceSummary) -> None:
    """
    图片代理阻断日志聚合窗口到期回调，输出窗口内的命中计数与首条样例。

    summary.key 由 `_emit_image_proxy_block_warning` 固定构造为
    `(host, reason_label)` 二元组；摘要保留首条事件的 URL 与解析 IP，
    避免运维只看到 count 而无法定位是哪批请求被合并。
    """
    host, reason = summary.key
    payload = summary.first_payload or {}
    sample_ips = ",".join(payload.get("ips") or [])
    logger.warn(
        "Blocked unsafe image URL (aggregated): "
        f"host={host or ''}, reason={reason}, "
        f"count={summary.count}, window={summary.window_seconds:g}s, "
        f"sample_url={payload.get('url', '')}, sample_ips={sample_ips}"
    )


# 图片代理阻断日志聚合器。同 (host, reason) 高频拦截在窗口内合并为一条聚合摘要，避免媒体详情页一次请求把日志刷爆；
# 放行 debug 日志与诊断布尔结果不受聚合影响。
_image_proxy_block_log_coalescer = EventCoalescer(
    window_seconds=_IMAGE_PROXY_BLOCK_LOG_WINDOW_SECONDS,
    on_flush=_log_image_proxy_block_summary,
    source="image_proxy",
)


async def _emit_image_proxy_block_warning(
    *,
    url: str,
    diagnosis: "UrlSafetyDiagnosis",
    signature_carried: bool,
    allowed_private_ranges: Optional[Iterable[str]],
) -> None:
    """
    把诊断结果转写为结构化阻断 warning，并交由 coalescer 决定是否实际输出。

    `signature_carried=True` 表示请求 URL 在 fragment 里实际携带了代理签名但
    校验失败，此时在 reason 末尾追加 `invalid_signature` 标记，便于区分
    "未签名外链直接撞 allowlist"与"签名 URL 已失效"两种排查路径。
    """
    # reason_label 既作为 warning 字段，也作为 coalescer 桶键的一部分；签名
    # 标记拼接到同一字符串里是为了让"带签名失败"的命中与"裸 URL 失败"分桶，
    # 各自独立计数与摘要，不要在不引入新桶维度的情况下拆开。
    reason_label = diagnosis.reason.value
    if signature_carried:
        reason_label = f"{reason_label}+{_INVALID_SIGNATURE_TAG}"
    allowed_ranges = [str(r) for r in (allowed_private_ranges or [])]
    hint = (
        _IMAGE_PROXY_FAKEIP_HINT
        if diagnosis.reason is UrlSafetyReason.NON_GLOBAL_DNS_RESULT
        and not allowed_ranges
        else None
    )
    key = (diagnosis.host or "", reason_label)
    payload = {"url": url, "ips": list(diagnosis.ips)}
    decision = await _image_proxy_block_log_coalescer.record(key=key, payload=payload)
    if decision is CoalesceDecision.EMIT:
        logger.warn(
            _format_image_proxy_block_warning(
                url=url,
                reason=reason_label,
                host=diagnosis.host,
                ips=list(diagnosis.ips),
                allowed_private_ranges=allowed_ranges,
                hint=hint,
            )
        )
