"""网络探测规则、结果和传输端口类型。"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Optional, Protocol
from urllib.parse import urlparse


class NetworkTestResponse(Protocol):
    """网络探测所需的最小异步响应合同。"""

    status_code: int
    headers: Mapping[str, str]
    text: str

    async def aclose(self) -> None:
        """释放响应占用的传输资源。"""


class NetworkTestTransport(Protocol):
    """网络探测使用的受限 HTTP 与 WebSocket 传输端口。"""

    async def get(
        self,
        url: str,
        *,
        proxy: Any = None,
        headers: Optional[Mapping[str, str]] = None,
        user_agent: Optional[str] = None,
    ) -> Optional[NetworkTestResponse]:
        """关闭自动重定向并校验证书后请求指定 HTTPS 地址。"""

    async def request(
        self,
        method: str,
        url: str,
        *,
        proxy: Any = None,
        headers: Optional[Mapping[str, str]] = None,
        user_agent: Optional[str] = None,
        json_body: Optional[Mapping[str, Any]] = None,
    ) -> Optional[NetworkTestResponse]:
        """使用指定 HTTP 方法请求 HTTPS 地址，并关闭自动重定向。"""

    async def websocket(
        self,
        url: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        user_agent: Optional[str] = None,
        timeout: float = 10,
    ) -> bool:
        """执行 WebSocket 握手并在确认连接后立即关闭。"""


class NetworkTestLogger(Protocol):
    """网络探测所需的最小日志端口。"""

    def debug(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """记录不影响探测结果的诊断信息。"""

    def warning(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """记录被安全策略阻断的网络行为。"""


@dataclass(frozen=True, slots=True)
class NetworkTestTarget:
    """可安全返回给客户端的网络测试目标投影。"""

    id: str
    name: str
    address: str
    icon: str


@dataclass(frozen=True, slots=True)
class NetworkTestRule:
    """仅由宿主构造、不会向客户端暴露的完整网络测试规则。"""

    id: str
    name: str
    icon: str
    url: str
    proxy: bool
    allowed_redirect_prefixes: tuple[str, ...]
    probe_protocol: str = "http"
    http_method: str = "GET"
    request_json: Optional[tuple[tuple[str, Any], ...]] = None
    module_ids: tuple[str, ...] = ()
    success_status_codes: tuple[int, ...] = (200,)
    expected_text: Optional[str] = None
    invalid_message: Optional[str] = None
    proxy_name: Optional[str] = None
    headers: tuple[tuple[str, str], ...] = ()
    display_address: Optional[str] = None

    def public_target(self) -> NetworkTestTarget:
        """投影展示字段，只公开目标主机而不暴露请求路径和凭据。"""
        return NetworkTestTarget(
            id=self.id,
            name=self.name,
            address=_safe_display_address(self.display_address or self.url),
            icon=self.icon,
        )

    def request_headers(self) -> Optional[dict[str, str]]:
        """为单次请求创建与冻结规则隔离的请求头字典。"""
        return dict(self.headers) or None

    def request_body(self) -> Optional[dict[str, Any]]:
        """为 HTTP 探测创建隔离的 JSON 请求体。"""
        return dict(self.request_json) if self.request_json is not None else None


@dataclass(frozen=True, slots=True)
class NetworkTestResult:
    """网络测试应用结果，不包含 HTTP 响应层对象。"""

    success: bool
    message: Optional[str] = None
    elapsed_ms: Optional[int] = None


SettingsReader = Callable[[str, Any], Any]


def _safe_display_address(url: str) -> str:
    """只返回来源协议、主机和端口，隐藏路径、查询参数和凭据。"""
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return ""
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https", "ws", "wss", "socks5", "socks5h"} or not hostname:
        return ""
    display_host = f"[{hostname}]" if ":" in hostname else hostname
    default_ports = {"http": 80, "https": 443, "ws": 80, "wss": 443}
    if port and port != default_ports.get(scheme):
        display_host = f"{display_host}:{port}"
    return f"{scheme}://{display_host}"
