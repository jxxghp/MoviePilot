import asyncio
import hmac
import ipaddress
import socket
import threading
import time
from hashlib import sha256
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Union
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse

from anyio import Path as AsyncPath
from cachetools import TTLCache

from app.core.config import settings
from app.log import logger


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
    _SIGNED_URL_EXPIRE_SECONDS = 86400

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
        只有当前 lock 仍是字典里登记的那把、且没有其它协程在等待时才删除。
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
        async with lock:
            # 等到锁后再查一次缓存，前一个持锁者可能已经回填结果
            hit, value = SecurityUtils._cache_lookup(hostname)
            if hit:
                SecurityUtils._release_inflight_lock(hostname, lock)
                return value

            loop = asyncio.get_running_loop()
            try:
                address_infos = await loop.getaddrinfo(
                    hostname, None, type=socket.SOCK_STREAM
                )
            except socket.gaierror:
                SecurityUtils._cache_store(hostname, None)
                SecurityUtils._release_inflight_lock(hostname, lock)
                return None
            addresses = _resolve_addrinfo_to_ips(address_infos)
            SecurityUtils._cache_store(hostname, addresses)
        SecurityUtils._release_inflight_lock(hostname, lock)
        return addresses

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
    def _url_signature_payload(url: str, expires_at: int, purpose: str) -> bytes:
        """
        构造 URL 签名载荷。

        签名覆盖用途、过期时间和完整 URL，确保同一个签名不能挪用到其它
        内网地址或其它代理用途。
        """
        return f"{purpose}\n{expires_at}\n{url}".encode("utf-8")

    @staticmethod
    def _sign_url_payload(url: str, expires_at: int, purpose: str) -> str:
        """
        使用 RESOURCE_SECRET_KEY 对 URL 签名载荷生成 HMAC。
        """
        return hmac.new(
            settings.RESOURCE_SECRET_KEY.encode("utf-8"),
            SecurityUtils._url_signature_payload(url, expires_at, purpose),
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
        expires_in: int = _SIGNED_URL_EXPIRE_SECONDS,
        purpose: str = _SIGNED_URL_PURPOSE,
    ) -> str:
        """
        给服务端返回的资源 URL 添加临时签名。

        该签名用于允许 `/system/img` 代理访问服务端已经确认过的私网图片 URL，
        避免代理端点重新依赖媒体服务器的具体路径规则。
        """
        if not url:
            return url
        parsed_url = urlparse(url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            return url
        clean_url = SecurityUtils.strip_url_signature(url)
        expires_at = int(time.time() + expires_in)
        signature = SecurityUtils._sign_url_payload(clean_url, expires_at, purpose)
        fragment = urlencode(
            {
                "mp_exp": str(expires_at),
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
        """
        if not url:
            return None
        parsed_url = urlparse(url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            return None
        fragment_params = dict(parse_qsl(parsed_url.fragment, keep_blank_values=True))
        expires_at = fragment_params.get("mp_exp")
        signature = fragment_params.get("mp_sig")
        signed_purpose = fragment_params.get("mp_purpose")
        if not expires_at or not signature or signed_purpose != purpose:
            return None
        try:
            expires_at_int = int(expires_at)
        except ValueError:
            return None
        if expires_at_int < int(time.time()):
            return None

        clean_url = SecurityUtils.strip_url_signature(url)
        expected_signature = SecurityUtils._sign_url_payload(
            clean_url, expires_at_int, purpose
        )
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
        验证URL是否在允许的域名列表中，包括带有端口的域名（同步版本）

        :param url: 需要验证的 URL
        :param allowed_domains: 允许的域名集合，域名可以包含端口
        :param strict: 是否严格匹配一级域名（默认为 False，允许多级域名）
        :param block_private: 是否拦截解析到非公网地址的 URL，防止 SSRF
        :param allowed_private_ranges: 域名命中后额外允许的非公网 IP/CIDR 网段
        :return: 如果URL合法且在允许的域名列表中，返回 True；否则返回 False

        注意：`block_private=True` 时会同步调用 `getaddrinfo`；async 上下文请改用
        `is_safe_url_async`。
        """
        try:
            hostname = SecurityUtils._check_url_allowlist(url, allowed_domains, strict)
            if hostname is None:
                return False

            if block_private and not SecurityUtils._is_global_hostname(hostname):
                private_match = SecurityUtils._is_allowed_private_hostname(
                    hostname, allowed_private_ranges
                )
                if private_match:
                    SecurityUtils._log_private_range_allowed(url, private_match)
                    return True
                return False

            return True
        except Exception as e:
            logger.debug(f"Error occurred while validating URL: {e}")
            return False

    @staticmethod
    async def is_safe_url_async(
        url: str,
        allowed_domains: Union[Set[str], List[str]],
        strict: bool = False,
        block_private: bool = False,
        allowed_private_ranges: Optional[Iterable[str]] = None,
    ) -> bool:
        """
        `is_safe_url` 的异步版本，参数与返回值含义不变。

        DNS 解析通过事件循环线程池执行，并复用 TTL 缓存。
        """
        try:
            hostname = SecurityUtils._check_url_allowlist(url, allowed_domains, strict)
            if hostname is None:
                return False

            if block_private and not await SecurityUtils._is_global_hostname_async(
                hostname
            ):
                private_match = await SecurityUtils._is_allowed_private_hostname_async(
                    hostname, allowed_private_ranges
                )
                if private_match:
                    SecurityUtils._log_private_range_allowed(url, private_match)
                    return True
                return False

            return True
        except Exception as e:
            logger.debug(f"Error occurred while validating URL: {e}")
            return False

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
