"""受控外部网络探测应用服务。"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from time import monotonic
from typing import Any, Optional, Protocol
from urllib.parse import urljoin, urlparse

from app.foundation.url import UrlUtils

_REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}
_MAX_REDIRECTS = 3


class NetworkTestResponse(Protocol):
    """网络探测所需的最小异步响应合同。"""

    status_code: int
    headers: Mapping[str, str]
    text: str

    async def aclose(self) -> None:
        """释放响应占用的传输资源。"""


class NetworkTestTransport(Protocol):
    """网络探测使用的受限 HTTPS GET 端口。"""

    async def get(
        self,
        url: str,
        *,
        proxy: Any = None,
        headers: Optional[Mapping[str, str]] = None,
        user_agent: Optional[str] = None,
    ) -> Optional[NetworkTestResponse]:
        """关闭自动重定向并校验证书后请求指定 HTTPS 地址。"""


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
    expected_text: Optional[str] = None
    invalid_message: Optional[str] = None
    proxy_name: Optional[str] = None
    headers: tuple[tuple[str, str], ...] = ()

    def public_target(self) -> NetworkTestTarget:
        """投影客户端展示字段，避免泄露 URL、凭据和准入规则。"""
        return NetworkTestTarget(id=self.id, name=self.name, icon=self.icon)

    def request_headers(self) -> Optional[dict[str, str]]:
        """为单次请求创建与冻结规则隔离的请求头字典。"""
        return dict(self.headers) or None


@dataclass(frozen=True, slots=True)
class NetworkTestResult:
    """网络测试应用结果，不包含 HTTP 响应层对象。"""

    success: bool
    message: Optional[str] = None
    elapsed_ms: Optional[int] = None


SettingsReader = Callable[[str, Any], Any]


class NetworkTestService:
    """维护目标目录、安全准入并执行服务端受控连通性测试。"""

    def __init__(
        self,
        *,
        transport: NetworkTestTransport,
        settings: SettingsReader,
        logger: NetworkTestLogger,
    ) -> None:
        """注入通用传输、部署设置读取和运行日志端口。"""
        self._transport = transport
        self._settings = settings
        self._logger = logger

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
        current_url = target.url
        redirect_count = 0
        response: Optional[NetworkTestResponse] = None
        while redirect_count <= _MAX_REDIRECTS:
            response = await self._transport.get(
                current_url,
                proxy=self._settings("PROXY", None) if target.proxy else None,
                headers=target.request_headers(),
                user_agent=self._settings("NORMAL_USER_AGENT", None),
            )
            if response is None or response.status_code not in _REDIRECT_STATUS_CODES:
                break
            location = response.headers.get("location")
            if not location:
                break
            next_url = urljoin(current_url, location)
            if not self._is_allowed_redirect(next_url, target):
                await self._close_response(response)
                self._logger.warning(
                    f"拦截网络测试重定向: {current_url} -> {next_url}"
                )
                return NetworkTestResult(
                    success=False,
                    message="测试目标发生了未授权跳转",
                )
            await self._close_response(response)
            response = None
            current_url = next_url
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

    @staticmethod
    def _build_response_result(
        target: NetworkTestRule,
        response: NetworkTestResponse,
        elapsed_ms: int,
    ) -> NetworkTestResult:
        """把传输响应归一为稳定的应用结果。"""
        if response.status_code == 200:
            if target.expected_text and target.expected_text.lower() not in (
                response.text or ""
            ).lower():
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
        """兜底校验服务端规则未生成非 HTTPS、带凭据或目录外地址。"""
        parsed = urlparse(target.url)
        if parsed.scheme.lower() != "https":
            return "测试地址仅支持 HTTPS"
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
        return any(
            cls._matches_prefix(url, prefix)
            for prefix in target.allowed_redirect_prefixes
        )

    @staticmethod
    def _matches_prefix(url: str, prefix: str) -> bool:
        """按协议、主机、有效端口和路径前缀匹配允许的跳转范围。"""
        parsed_url = urlparse(url)
        parsed_prefix = urlparse(prefix)
        if parsed_url.scheme.lower() != parsed_prefix.scheme.lower():
            return False
        if (parsed_url.hostname or "").lower() != (
            parsed_prefix.hostname or ""
        ).lower():
            return False
        try:
            url_port = parsed_url.port or (
                443 if parsed_url.scheme.lower() == "https" else 80
            )
            prefix_port = parsed_prefix.port or (
                443 if parsed_prefix.scheme.lower() == "https" else 80
            )
        except ValueError:
            return False
        if url_port != prefix_port:
            return False
        prefix_path = parsed_prefix.path or "/"
        if prefix_path.endswith("/"):
            return parsed_url.path.startswith(prefix_path)
        return parsed_url.path == prefix_path or parsed_url.path.startswith(
            f"{prefix_path}/"
        )

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
        """根据当前部署设置构建唯一网络测试目录和安全准入规则。"""
        github_proxy = UrlUtils.standardize_base_url(
            self._settings("GITHUB_PROXY", None) or ""
        )
        pip_proxy = UrlUtils.standardize_base_url(
            self._settings("PIP_PROXY", None) or "https://pypi.org/simple/"
        )
        tmdb_key = self._settings("TMDB_API_KEY", None)
        tmdb_domain = (
            self._settings("TMDB_API_DOMAIN", None) or "api.themoviedb.org"
        )
        github_headers = self._freeze_headers(
            self._settings("GITHUB_HEADERS", None)
        )
        github_readme_url = "https://github.com/jxxghp/MoviePilot/blob/v2/README.md"
        raw_readme_url = (
            "https://raw.githubusercontent.com/jxxghp/MoviePilot/v2/README.md"
        )
        rules = [
            NetworkTestRule(
                id="tmdb_api",
                name="api.themoviedb.org",
                icon="tmdb",
                url=f"https://api.themoviedb.org/3/movie/550?api_key={tmdb_key}",
                proxy=True,
                allowed_redirect_prefixes=("https://api.themoviedb.org/3/",),
            ),
            NetworkTestRule(
                id="tmdb_api_alt",
                name="api.tmdb.org",
                icon="tmdb",
                url=f"https://api.tmdb.org/3/movie/550?api_key={tmdb_key}",
                proxy=True,
                allowed_redirect_prefixes=("https://api.tmdb.org/3/",),
            ),
            NetworkTestRule(
                id="tmdb_web",
                name="www.themoviedb.org",
                icon="tmdb",
                url="https://www.themoviedb.org",
                proxy=True,
                allowed_redirect_prefixes=("https://www.themoviedb.org/",),
            ),
            NetworkTestRule(
                id="tvdb_api",
                name="api.thetvdb.com",
                icon="tvdb",
                url="https://api.thetvdb.com/series/81189",
                proxy=True,
                allowed_redirect_prefixes=("https://api.thetvdb.com/",),
            ),
            NetworkTestRule(
                id="fanart_api",
                name="webservice.fanart.tv",
                icon="fanart",
                url="https://webservice.fanart.tv",
                proxy=True,
                allowed_redirect_prefixes=("https://webservice.fanart.tv/",),
            ),
            NetworkTestRule(
                id="telegram_api",
                name="api.telegram.org",
                icon="telegram",
                url="https://api.telegram.org",
                proxy=True,
                allowed_redirect_prefixes=(
                    "https://api.telegram.org/",
                    "https://core.telegram.org/",
                ),
            ),
            NetworkTestRule(
                id="wechat_api",
                name="qyapi.weixin.qq.com",
                icon="wechat",
                url="https://qyapi.weixin.qq.com/cgi-bin/gettoken",
                proxy=False,
                allowed_redirect_prefixes=("https://qyapi.weixin.qq.com/",),
            ),
            NetworkTestRule(
                id="douban_api",
                name="frodo.douban.com",
                icon="douban",
                url="https://frodo.douban.com",
                proxy=False,
                allowed_redirect_prefixes=(
                    "https://frodo.douban.com/",
                    "https://www.douban.com/doubanapp/frodo",
                ),
            ),
            NetworkTestRule(
                id="slack_api",
                name="slack.com",
                icon="slack",
                url="https://slack.com",
                proxy=False,
                allowed_redirect_prefixes=(
                    "https://slack.com/",
                    "https://www.slack.com/",
                ),
            ),
            NetworkTestRule(
                id="pip_proxy",
                name="pypi.org",
                icon="python",
                url=f"{pip_proxy}rsa/",
                proxy=True,
                allowed_redirect_prefixes=(
                    pip_proxy,
                    "https://pypi.org/simple/",
                ),
                expected_text="pypi:repository-version",
                invalid_message="PIP加速代理已失效，请检查配置",
                proxy_name="PIP加速代理",
            ),
            NetworkTestRule(
                id="github_proxy_web",
                name="github.com",
                icon="github",
                url=(
                    f"{github_proxy}{github_readme_url}"
                    if github_proxy
                    else github_readme_url
                ),
                proxy=True,
                allowed_redirect_prefixes=(
                    "https://github.com/",
                    *(
                        (f"{github_proxy}https://github.com/",)
                        if github_proxy
                        else ()
                    ),
                ),
                expected_text="MoviePilot",
                invalid_message=(
                    "Github加速代理已失效，请检查配置"
                    if github_proxy
                    else "无效响应"
                ),
                proxy_name="Github加速代理" if github_proxy else None,
                headers=github_headers,
            ),
            NetworkTestRule(
                id="github_api",
                name="api.github.com",
                icon="github",
                url="https://api.github.com",
                proxy=True,
                allowed_redirect_prefixes=("https://api.github.com/",),
                headers=github_headers,
            ),
            NetworkTestRule(
                id="github_codeload",
                name="codeload.github.com",
                icon="github",
                url="https://codeload.github.com",
                proxy=True,
                allowed_redirect_prefixes=(
                    "https://codeload.github.com/",
                    "https://github.com/",
                ),
                headers=github_headers,
            ),
            NetworkTestRule(
                id="github_proxy_raw",
                name="raw.githubusercontent.com",
                icon="github",
                url=(
                    f"{github_proxy}{raw_readme_url}"
                    if github_proxy
                    else raw_readme_url
                ),
                proxy=True,
                allowed_redirect_prefixes=(
                    "https://raw.githubusercontent.com/",
                    *(
                        (f"{github_proxy}https://raw.githubusercontent.com/",)
                        if github_proxy
                        else ()
                    ),
                ),
                expected_text="MoviePilot",
                invalid_message=(
                    "Github加速代理已失效，请检查配置"
                    if github_proxy
                    else "无效响应"
                ),
                proxy_name="Github加速代理" if github_proxy else None,
                headers=github_headers,
            ),
        ]
        if tmdb_domain not in {"api.themoviedb.org", "api.tmdb.org"}:
            rules.insert(
                2,
                NetworkTestRule(
                    id="tmdb_api_configured",
                    name=str(tmdb_domain),
                    icon="tmdb",
                    url=f"https://{tmdb_domain}/3/movie/550?api_key={tmdb_key}",
                    proxy=True,
                    allowed_redirect_prefixes=(
                        f"https://{tmdb_domain}/3/",
                    ),
                ),
            )
        return tuple(rules)

    @staticmethod
    def _freeze_headers(value: Any) -> tuple[tuple[str, str], ...]:
        """把动态配置中的请求头复制为不可变、可比较的规则字段。"""
        if not isinstance(value, Mapping):
            return ()
        return tuple(
            (str(key), str(item))
            for key, item in value.items()
            if item is not None
        )


_configured_network_test_service: Optional[NetworkTestService] = None


def configure_network_test_service(service: NetworkTestService) -> None:
    """由启动组合根登记唯一的网络测试应用服务。"""
    global _configured_network_test_service
    _configured_network_test_service = service


def get_configured_network_test_service() -> NetworkTestService:
    """返回启动阶段登记的网络测试应用服务。"""
    if _configured_network_test_service is None:
        raise RuntimeError("网络测试服务尚未装配")
    return _configured_network_test_service
