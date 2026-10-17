"""
网络探测规则、结果和传输端口类型。
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Optional, Protocol
from urllib.parse import urlparse


class NetworkTestResponse(Protocol):
    """
    网络探测所需的最小异步响应合同。

    :ivar status_code: 探测收到的 HTTP 状态码
    :ivar headers: 用于检查重定向和内容类型的响应头
    :ivar text: 解码后的响应正文，供预期内容匹配使用
    """

    status_code: int
    headers: Mapping[str, str]
    text: str

    async def aclose(self) -> None:
        """
        释放响应占用的传输资源。
        """


class NetworkTestTransport(Protocol):
    """
    网络探测使用的受限 HTTP 与 WebSocket 传输端口。
    """

    async def get(
        self,
        url: str,
        *,
        proxy: Any = None,
        headers: Optional[Mapping[str, str]] = None,
        user_agent: Optional[str] = None,
    ) -> Optional[NetworkTestResponse]:
        """
        关闭自动重定向并校验证书后请求指定 HTTPS 地址。

        :param url: 经上层策略校验的探测目标地址
        :param proxy: 本次探测使用的代理配置；None 不显式指定代理
        :param headers: 探测专用请求头；None 使用传输端默认请求头
        :param user_agent: 探测使用的用户代理；None 使用传输端默认值
        :return: 探测响应；请求失败且没有响应时为 None，取得响应后由调用方关闭
        """

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
        """
        使用指定 HTTP 方法请求 HTTPS 地址，并关闭自动重定向。

        :param url: 经上层策略校验的探测目标地址
        :param method: HTTP 请求方法
        :param proxy: 本次探测使用的代理配置；None 不显式指定代理
        :param headers: 探测专用请求头；None 使用传输端默认请求头
        :param user_agent: 探测使用的用户代理；None 使用传输端默认值
        :param json_body: POST 探测请求的 JSON 字段；None 表示不发送请求体
        :return: 探测响应；请求失败且没有响应时为 None，取得响应后由调用方关闭
        """

    async def websocket(
        self,
        url: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        user_agent: Optional[str] = None,
        timeout: float = 10,
    ) -> bool:
        """
        执行 WebSocket 握手并在确认连接后立即关闭。

        :param url: 经上层策略校验的探测目标地址
        :param headers: 探测专用请求头；None 使用传输端默认请求头
        :param user_agent: 探测使用的用户代理；None 使用传输端默认值
        :param timeout: 握手超时，单位为秒，默认 10 秒
        :return: 握手成功并关闭连接时为 True；未能建立连接时为 False
        """


class NetworkTestLogger(Protocol):
    """
    网络探测所需的最小日志端口。
    """

    def debug(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """
        记录不影响探测结果的诊断信息。

        :param msg: 需要记录的中文诊断消息或格式模板
        :param args: 日志模板的位置参数
        :param kwargs: 日志调用的格式化或上下文关键字参数
        """

    def warning(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """
        记录被安全策略阻断的网络行为。

        :param msg: 需要记录的中文诊断消息或格式模板
        :param args: 日志模板的位置参数
        :param kwargs: 日志调用的格式化或上下文关键字参数
        """


@dataclass(frozen=True, slots=True)
class NetworkTestTarget:
    """
    可安全返回给客户端的网络测试目标投影。

    :param id: 测试目标的稳定标识
    :param name: 向用户展示的目标名称
    :param address: 已移除路径、查询参数和凭据的目标地址
    :param icon: 前端展示目标时使用的图标名称
    """

    id: str
    name: str
    address: str
    icon: str


@dataclass(frozen=True, slots=True)
class NetworkTestRule:
    """
    仅由宿主构造、不会向客户端暴露的完整网络测试规则。

    :param id: 测试目标的稳定标识
    :param name: 向用户展示的目标名称
    :param icon: 前端展示目标时使用的图标名称
    :param url: 实际探测地址，可包含不对用户公开的路径或凭据
    :param proxy: 是否使用宿主配置的出站代理
    :param allowed_redirect_prefixes: 允许继续跟随的重定向地址前缀白名单
    :param probe_protocol: 探测协议，默认 http；支持按规则使用 WebSocket
    :param http_method: HTTP 探测方法，默认 GET
    :param request_json: JSON 请求体的键值对；None 表示不发送请求体
    :param module_ids: 控制目标可见性的模块标识；空元组表示不受模块启用状态限制
    :param success_status_codes: 视为连通成功的 HTTP 状态码，默认仅 200
    :param expected_text: 响应正文必须包含的文本，不区分大小写；None 表示不检查正文片段
    :param invalid_message: 响应不符合规则时使用的失败说明；None 使用默认提示
    :param proxy_name: 被探测加速代理的展示名称；None 表示普通目标
    :param headers: 探测专用请求头的键值对
    :param display_address: 替代实际探测地址的展示入口；None 从 url 提取安全地址
    """

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
        """
        投影展示字段，只公开目标主机而不暴露请求路径和凭据。
        """
        return NetworkTestTarget(
            id=self.id,
            name=self.name,
            address=_safe_display_address(self.display_address or self.url),
            icon=self.icon,
        )

    def request_headers(self) -> Optional[dict[str, str]]:
        """
        为单次请求创建与冻结规则隔离的请求头字典。
        """
        return dict(self.headers) or None

    def request_body(self) -> Optional[dict[str, Any]]:
        """
        为 HTTP 探测创建隔离的 JSON 请求体。
        """
        return dict(self.request_json) if self.request_json is not None else None


@dataclass(frozen=True, slots=True)
class NetworkTestResult:
    """
    网络测试应用结果，不包含 HTTP 响应层对象。

    :param success: 探测是否满足该目标的成功条件
    :param message: 探测结果或失败原因的说明；成功时可为 None
    :param elapsed_ms: 探测耗时，单位为毫秒；未得到耗时时为 None
    """

    success: bool
    message: Optional[str] = None
    elapsed_ms: Optional[int] = None


SettingsReader = Callable[[str, Any], Any]


def _safe_display_address(url: str) -> str:
    """
    只返回来源协议、主机和端口，隐藏路径、查询参数和凭据。
    """
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
