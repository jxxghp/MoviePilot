"""受控外部网络探测应用服务。"""

from collections.abc import Collection, Mapping
from time import monotonic
from typing import Any, Callable, Optional
from urllib.parse import urljoin, urlparse

from app.application.nettest.catalogue import build_network_rules
from app.application.nettest.domain import (
    NetworkTestLogger,
    NetworkTestResponse,
    NetworkTestResult,
    NetworkTestRule,
    NetworkTestTarget,
    NetworkTestTransport,
    SettingsReader,
)

_REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}
_MAX_REDIRECTS = 3


class NetworkTestService:
    """维护目标目录、安全准入并执行服务端受控连通性测试。"""

    def __init__(
        self,
        *,
        transport: NetworkTestTransport,
        settings: SettingsReader,
        logger: NetworkTestLogger,
        enabled_module_ids: Optional[Callable[[], Collection[str]]] = None,
    ) -> None:
        """注入网络端口、部署设置和当前启用模块读取器。"""
        self._transport = transport
        self._settings = settings
        self._logger = logger
        self._enabled_module_ids = enabled_module_ids

    def list_targets(self) -> tuple[NetworkTestTarget, ...]:
        """返回不含请求细节的当前网络测试目标目录。"""
        return tuple(rule.public_target() for rule in self._build_rules())

    async def execute(
        self,
        *,
        target_id: Optional[str] = None,
        url: Optional[str] = None,
        include: Optional[str] = None,
    ) -> NetworkTestResult:
        """解析内置目标、执行安全校验并返回连通性测试结果。"""
        target = self._find_rule(target_id=target_id, url=url)
        if target is None:
            return NetworkTestResult(success=False, message="测试目标不存在")
        invalid_message = self._validate_rule(target)
        if invalid_message:
            self._logger.warning(f"拦截不安全的网络测试地址: {target.url}")
            return NetworkTestResult(success=False, message=invalid_message)
        if include:
            self._logger.debug("nettest include 参数已忽略，改为服务端固定校验")
        return await self._request(target)

    async def _request(self, target: NetworkTestRule) -> NetworkTestResult:
        """手动处理受控重定向并校验最终响应。"""
        started_at = monotonic()
        if target.probe_protocol == "websocket":
            return await self._request_websocket(target, started_at)

        current_url = target.url
        current_method = target.http_method
        current_json = target.request_body()
        redirect_count = 0
        response: Optional[NetworkTestResponse] = None
        while redirect_count <= _MAX_REDIRECTS:
            response = await self._request_http(
                target=target,
                url=current_url,
                method=current_method,
                json_body=current_json,
            )
            if response is None or response.status_code not in _REDIRECT_STATUS_CODES:
                break
            location = response.headers.get("location")
            if not location:
                break
            next_url = urljoin(current_url, location)
            if not self._is_allowed_redirect(next_url, target):
                await self._close_response(response)
                self._logger.warning(f"拦截网络测试重定向: {current_url} -> {next_url}")
                return NetworkTestResult(
                    success=False,
                    message="测试目标发生了未授权跳转",
                )
            response_status = response.status_code
            await self._close_response(response)
            response = None
            current_url = next_url
            if current_method == "POST" and response_status in {301, 302, 303}:
                current_method = "GET"
                current_json = None
            redirect_count += 1

        elapsed_ms = round((monotonic() - started_at) * 1000)
        if redirect_count > _MAX_REDIRECTS:
            await self._close_response(response)
            return NetworkTestResult(success=False, message="测试目标重定向次数过多")
        if response is None:
            label = target.proxy_name or target.name
            return NetworkTestResult(
                success=False,
                message=f"{label}无法连接",
                elapsed_ms=elapsed_ms,
            )

        try:
            return self._build_response_result(target, response, elapsed_ms)
        finally:
            await self._close_response(response)

    async def _request_http(
        self,
        *,
        target: NetworkTestRule,
        url: str,
        method: str,
        json_body: Optional[Mapping[str, Any]],
    ) -> Optional[NetworkTestResponse]:
        """按目标声明的方法发送 HTTP 探测，同时兼容旧 GET 传输端口。"""
        options = {
            "proxy": self._settings("PROXY", None) if target.proxy else None,
            "headers": target.request_headers(),
            "user_agent": self._settings("NORMAL_USER_AGENT", None),
        }
        request = getattr(self._transport, "request", None)
        if callable(request):
            return await request(method, url, json_body=json_body, **options)
        if method == "GET":
            return await self._transport.get(url, **options)
        return None

    async def _request_websocket(
        self,
        target: NetworkTestRule,
        started_at: float,
    ) -> NetworkTestResult:
        """探测 WebSocket 握手，不把 HTTP GET 当作长连接可用性。"""
        probe = getattr(self._transport, "websocket", None)
        connected = False
        if callable(probe):
            try:
                connected = await probe(
                    target.url,
                    headers=target.request_headers(),
                    user_agent=self._settings("NORMAL_USER_AGENT", None),
                    timeout=10,
                )
            except Exception as err:  # noqa: BLE001 - 探测失败转成稳定的结果消息
                self._logger.debug(f"WebSocket 网络探测失败: {err}")
        elapsed_ms = round((monotonic() - started_at) * 1000)
        if connected:
            return NetworkTestResult(success=True, elapsed_ms=elapsed_ms)
        label = target.proxy_name or target.name
        return NetworkTestResult(
            success=False,
            message=f"{label}无法连接",
            elapsed_ms=elapsed_ms,
        )

    @staticmethod
    def _build_response_result(
        target: NetworkTestRule,
        response: NetworkTestResponse,
        elapsed_ms: int,
    ) -> NetworkTestResult:
        """把传输响应归一为稳定的应用结果。"""
        if response.status_code in target.success_status_codes:
            if target.expected_text and target.expected_text.lower() not in (response.text or "").lower():
                return NetworkTestResult(
                    success=False,
                    message=target.invalid_message or "无效响应",
                    elapsed_ms=elapsed_ms,
                )
            return NetworkTestResult(success=True, elapsed_ms=elapsed_ms)
        if target.proxy_name:
            message = f"{target.proxy_name}已失效，错误码：{response.status_code}"
        else:
            message = f"错误码：{response.status_code}"
            if "github" in target.url:
                if response.status_code == 401:
                    message = "Github Token已失效，请检查配置"
                elif response.status_code in {403, 429}:
                    message = "触发限流，请配置Github Token"
        return NetworkTestResult(
            success=False,
            message=message,
            elapsed_ms=elapsed_ms,
        )

    def _find_rule(
        self,
        *,
        target_id: Optional[str],
        url: Optional[str],
    ) -> Optional[NetworkTestRule]:
        """优先按正式 target_id、否则按旧 URL 精确匹配内置规则。"""
        rules = self._build_rules()
        if target_id:
            return next((rule for rule in rules if rule.id == target_id), None)
        if url:
            return next((rule for rule in rules if rule.url == url), None)
        return None

    def _validate_rule(self, target: NetworkTestRule) -> Optional[str]:
        """兜底校验服务端规则协议、请求方法、凭据和目标目录。"""
        parsed = urlparse(target.url)
        expected_scheme = "wss" if target.probe_protocol == "websocket" else "https"
        if target.probe_protocol not in {"http", "websocket"}:
            return "测试协议无效"
        if parsed.scheme.lower() != expected_scheme:
            return f"测试地址仅支持 {expected_scheme.upper()}"
        if target.probe_protocol == "http" and target.http_method not in {"GET", "POST"}:
            return "测试方法无效"
        if not parsed.netloc:
            return "测试地址无效"
        if parsed.username or parsed.password:
            return "测试地址不支持携带账号信息"
        if not any(rule == target for rule in self._build_rules()):
            return "测试地址不在允许的测试目标列表中"
        return None

    @classmethod
    def _is_allowed_redirect(cls, url: str, target: NetworkTestRule) -> bool:
        """只允许重定向到当前测试项声明的协议、主机、端口和路径。"""
        parsed = urlparse(url)
        if parsed.scheme.lower() != "https" or not parsed.netloc:
            return False
        if parsed.username or parsed.password:
            return False
        return any(cls._matches_prefix(url, prefix) for prefix in target.allowed_redirect_prefixes)

    @staticmethod
    def _matches_prefix(url: str, prefix: str) -> bool:
        """按协议、主机、有效端口和路径前缀匹配允许的跳转范围。"""
        parsed_url = urlparse(url)
        parsed_prefix = urlparse(prefix)
        if parsed_url.scheme.lower() != parsed_prefix.scheme.lower():
            return False
        if (parsed_url.hostname or "").lower() != (parsed_prefix.hostname or "").lower():
            return False
        try:
            url_port = parsed_url.port or (443 if parsed_url.scheme.lower() == "https" else 80)
            prefix_port = parsed_prefix.port or (443 if parsed_prefix.scheme.lower() == "https" else 80)
        except ValueError:
            return False
        if url_port != prefix_port:
            return False
        prefix_path = parsed_prefix.path or "/"
        if prefix_path.endswith("/"):
            return parsed_url.path.startswith(prefix_path)
        return parsed_url.path == prefix_path or parsed_url.path.startswith(f"{prefix_path}/")

    async def _close_response(
        self,
        response: Optional[NetworkTestResponse],
    ) -> None:
        """安静释放已读取响应，关闭失败不覆盖真实探测结果。"""
        if response is None or not hasattr(response, "aclose"):
            return
        try:
            await response.aclose()
        except Exception as err:  # noqa: BLE001 - 资源回收失败仅记录诊断
            self._logger.debug(f"关闭网络测试响应失败: {err}")

    def _build_rules(self) -> tuple[NetworkTestRule, ...]:
        """根据当前部署设置和已启用模块构建唯一网络测试目录。"""
        return build_network_rules(self._settings, self._enabled_module_ids)


_configured_network_test_service: Optional[NetworkTestService] = None


def configure_network_test_service(service: NetworkTestService) -> None:
    """由启动组合根登记唯一的网络测试应用服务。"""
    global _configured_network_test_service
    _configured_network_test_service = service


def reset_network_test_service() -> None:
    """清除当前 lifespan 的网络测试应用服务。"""
    global _configured_network_test_service
    _configured_network_test_service = None


def get_configured_network_test_service() -> NetworkTestService:
    """返回启动阶段登记的网络测试应用服务。"""
    if _configured_network_test_service is None:
        raise RuntimeError("网络测试服务尚未装配")
    return _configured_network_test_service
