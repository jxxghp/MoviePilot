"""GitHub 单点登录入口类型的实现类。

宿主按 ``GithubSsoEntry(name=实例名, **配置内容)`` 构造本类，构造形状即
`ServiceInstanceDeclaration` 里 ``impl`` 那条路径的契约，构造参数与该类型声明的
``config_schema`` 字段一一对应。

本类只做 OAuth 握手本身：拼授权地址、拿授权码换令牌、读第三方账号标识、按组织名单
判定准入。它不接触本项目的用户、会话与身份绑定——那几样由宿主的
``create_plugin_auth_ticket_for_identity`` 统一处理，入口类型没有理由各自实现一遍。

授权回调地址只取用户配置的那一份，任何一次请求带来的回调地址都不参与拼装：授权码
落到哪个地址由这一个取值决定，让请求参与决定它就等于把授权码交给请求方。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode, urlsplit

from app.sdk.network import RequestUtils

# github.com 的站点地址与 API 地址，自建 GitHub Enterprise Server 由用户各自填写
DEFAULT_BASE_URL = "https://github.com"
DEFAULT_API_BASE_URL = "https://api.github.com"

# 单次 HTTP 往返的默认超时秒数，以及用户可配置的上下界
DEFAULT_TIMEOUT_SECONDS = 15
MIN_TIMEOUT_SECONDS = 3
MAX_TIMEOUT_SECONDS = 120

# 只读取账号身份所需的最小授权范围
_SCOPE_READ_USER = "read:user"

# 判定组织归属所需的授权范围，仅在用户配置了组织名单时附加
_SCOPE_READ_ORG = "read:org"

# 授权地址与令牌兑换地址在站点地址下的路径
_AUTHORIZE_PATH = "/login/oauth/authorize"
_ACCESS_TOKEN_PATH = "/login/oauth/access_token"

# 账号信息与所属组织在 API 地址下的路径
_USER_PATH = "/user"
_USER_ORGS_PATH = "/user/orgs"

# 读取所属组织时单页取回的条目数，GitHub 允许的上限
_ORGS_PAGE_SIZE = 100

# GitHub REST API 的版本协商头，缺省时对端按最新版本响应
_API_VERSION_HEADER = "2022-11-28"


@dataclass(frozen=True, slots=True)
class GithubIdentity:
    """一次握手读到的第三方账号身份。

    :param external_id: GitHub 账号的数字标识，即身份绑定表 external_id 列的取值
    :param login: GitHub 登录名，仅用于展示与日志
    :param display_name: GitHub 侧的显示名
    """

    external_id: str
    login: str
    display_name: str


class GithubSsoAuthError(Exception):
    """握手过程中可直接呈现给用户的失败。"""


class GithubSsoEntry:
    """一条 GitHub 单点登录配置对应的握手实现。

    :param name: 实例名，由宿主按配置填入，即登录页上该入口按钮的名称
    :param client_id: GitHub OAuth App 的 Client ID
    :param client_secret: GitHub OAuth App 的 Client Secret
    :param redirect_uri: 在 GitHub OAuth App 里登记的授权回调地址
    :param base_url: GitHub 站点地址，自建实例填自己的地址
    :param api_base_url: GitHub API 地址，自建实例通常为站点地址加 ``/api/v3``
    :param allowed_organizations: 允许登录的 GitHub 组织登录名，留空表示不限制
    :param request_timeout: 单次 HTTP 往返的超时秒数
    :raises ValueError: 凭据缺失，或回调地址不是绝对的 http(s) 地址
    """

    def __init__(
        self,
        name: str = "",
        client_id: str = "",
        client_secret: str = "",
        redirect_uri: str = "",
        base_url: str = DEFAULT_BASE_URL,
        api_base_url: str = DEFAULT_API_BASE_URL,
        allowed_organizations: Optional[List[str]] = None,
        request_timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        """按一条用户配置建立握手实现，并在构造时判定凭据与回调地址是否可用。"""
        self.name = name
        self.client_id = (client_id or "").strip()
        self.client_secret = (client_secret or "").strip()
        self.redirect_uri = (redirect_uri or "").strip()
        self.base_url = (base_url or DEFAULT_BASE_URL).strip().rstrip("/")
        self.api_base_url = (api_base_url or DEFAULT_API_BASE_URL).strip().rstrip("/")
        self.allowed_organizations = tuple(
            item.strip().lower()
            for item in (allowed_organizations or [])
            if isinstance(item, str) and item.strip()
        )
        self.request_timeout = int(request_timeout or DEFAULT_TIMEOUT_SECONDS)
        if not self.client_id or not self.client_secret:
            raise ValueError("GitHub OAuth App 的 Client ID 与 Client Secret 均不可为空")
        parts = urlsplit(self.redirect_uri)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError("授权回调地址须是完整的 http 或 https 地址")

    @property
    def callback_path(self) -> str:
        """返回配置的回调地址中的路径部分，供调用方核对它指向本插件的回调路由。

        :return: 回调地址的路径
        """
        return urlsplit(self.redirect_uri).path

    @property
    def scope(self) -> str:
        """返回本入口向用户索取的授权范围。

        不配组织名单时只取 ``read:user``：多要一项 ``read:org`` 会在 GitHub 的授权
        页上多列一条用不到的权限，而用户没有办法只同意其中一项。

        :return: 空格分隔的授权范围
        """
        scopes = [_SCOPE_READ_USER]
        if self.allowed_organizations:
            scopes.append(_SCOPE_READ_ORG)
        return " ".join(scopes)

    def authorize_url(self, state: str) -> str:
        """拼出把浏览器送往 GitHub 的授权地址。

        ``allow_signup=false`` 让 GitHub 的授权页不再提供注册入口：本入口只负责把
        已有账号对上已有绑定，引导访客当场注册一个新的 GitHub 账号对登录没有帮助。

        :param state: 本次往返的 state
        :return: GitHub 授权地址
        """
        query = urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "scope": self.scope,
                "state": state,
                "allow_signup": "false",
            }
        )
        return f"{self.base_url}{_AUTHORIZE_PATH}?{query}"

    def exchange_code(self, code: str) -> str:
        """用授权码换取访问令牌。

        兑换时一并回传 ``redirect_uri``：GitHub 据此核对这枚授权码确实是发往同一个
        回调地址的那一枚，缺了它，一枚被截获的授权码可以在任何回调地址上兑现。

        :param code: 回调带回的授权码
        :return: 访问令牌
        :raises GithubSsoAuthError: 对端不可达、拒绝兑换或未返回令牌
        """
        response = RequestUtils(
            timeout=self.request_timeout,
            content_type="application/x-www-form-urlencoded",
            accept_type="application/json",
        ).post_res(
            f"{self.base_url}{_ACCESS_TOKEN_PATH}",
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "code": code,
                "redirect_uri": self.redirect_uri,
            },
        )
        payload = self._json_payload(response, "兑换 GitHub 访问令牌")
        if not isinstance(payload, dict):
            raise GithubSsoAuthError("兑换 GitHub 访问令牌失败：对端返回的不是对象")
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            reason = payload.get("error_description") or payload.get("error") or "对端未返回访问令牌"
            raise GithubSsoAuthError(f"兑换 GitHub 访问令牌失败：{reason}")
        return token

    def fetch_identity(self, access_token: str) -> GithubIdentity:
        """读取访问令牌背后的 GitHub 账号身份。

        身份取数字标识 ``id`` 而不是登录名：登录名可以由账号所有者随时改掉，改完之后
        原来那个名字可以被别人注册走。用登录名做绑定键，等于让一次改名把已绑定的
        本项目账号交给下一个占用该名字的人。

        :param access_token: 访问令牌
        :return: 第三方账号身份
        :raises GithubSsoAuthError: 对端不可达或未返回账号标识
        """
        payload = self._json_payload(
            self._api_request(_USER_PATH, access_token), "读取 GitHub 账号信息"
        )
        if not isinstance(payload, dict):
            raise GithubSsoAuthError("读取 GitHub 账号信息失败：对端返回的不是对象")
        external_id = payload.get("id")
        login = payload.get("login")
        if external_id is None or not isinstance(login, str) or not login:
            raise GithubSsoAuthError("读取 GitHub 账号信息失败：对端未返回账号标识")
        display_name = payload.get("name")
        return GithubIdentity(
            external_id=str(external_id),
            login=login,
            display_name=display_name if isinstance(display_name, str) and display_name else login,
        )

    def organization_allowed(self, access_token: str) -> Tuple[bool, str]:
        """判定该账号是否落在允许登录的组织名单内。

        未配置名单时一律放行，本入口不替用户加一道他没有要求的限制。读不到所属组织时
        判为不放行：名单是一道准入闸，读不到就无从确认闸后的条件成立，此时放行等于
        这道闸在对端抖动时自动打开。

        :param access_token: 访问令牌
        :return: ``(是否放行, 不放行时面向用户的原因)``
        """
        if not self.allowed_organizations:
            return True, ""
        payload = self._json_payload(
            self._api_request(
                _USER_ORGS_PATH, access_token, params={"per_page": _ORGS_PAGE_SIZE}
            ),
            "读取 GitHub 所属组织",
        )
        if not isinstance(payload, list):
            raise GithubSsoAuthError("读取 GitHub 所属组织失败：对端返回的不是组织列表")
        joined = {
            str(item.get("login")).lower()
            for item in payload
            if isinstance(item, dict) and item.get("login")
        }
        if joined & set(self.allowed_organizations):
            return True, ""
        return False, "该 GitHub 账号不属于允许登录的组织"

    def _api_request(
        self, path: str, access_token: str, params: Optional[Dict[str, Any]] = None
    ) -> Any:
        """按访问令牌向 GitHub API 发起一次读取。

        :param path: API 路径
        :param access_token: 访问令牌
        :param params: 查询参数
        :return: HTTP 响应对象；请求未能发出时为 None
        """
        return RequestUtils(
            timeout=self.request_timeout,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": _API_VERSION_HEADER,
            },
        ).get_res(f"{self.api_base_url}{path}", params=params)

    @staticmethod
    def _json_payload(response: Any, action: str) -> Any:
        """把一次 HTTP 往返的结果解成 JSON。

        :param response: HTTP 响应对象，请求未能发出时为 None
        :param action: 本次往返的动作描述，用于组织失败文案
        :return: 解析出的 JSON 数据
        :raises GithubSsoAuthError: 对端不可达、返回非成功状态码或响应体不是 JSON
        """
        if response is None:
            raise GithubSsoAuthError(f"{action}失败：GitHub 服务不可达")
        if response.status_code >= 400:
            raise GithubSsoAuthError(f"{action}失败：GitHub 返回状态码 {response.status_code}")
        try:
            return response.json()
        except Exception as error:
            raise GithubSsoAuthError(f"{action}失败：响应内容不是 JSON（{error}）") from error
