"""
GitHub 授权边界使用的领域协议与不可变数据结构。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

# PR Skill 需要读写仓库并推送工作流文件，反馈 Skill 需要创建 Issue。
GITHUB_DEVICE_AUTH_SCOPES = ("read:user", "repo", "workflow")


class GithubAuthTransportError(RuntimeError):
    """
    表示 GitHub 授权外部传输或响应协议失败。
    """

    def __init__(self, message: str, *, unauthorized: bool = False) -> None:
        """
        保存不含外部响应原文的错误信息及 Token 失效标记。
        """
        super().__init__(message)
        self.unauthorized = unauthorized


@dataclass(frozen=True, slots=True)
class GithubDeviceCode:
    """
    GitHub Device Flow 返回的设备码信息。

    :param device_code: 用于轮询授权结果的设备码，不向用户展示
    :param user_code: 供用户在 GitHub 授权页面输入的验证码
    :param verification_uri: 用户完成设备授权的页面地址
    :param expires_in: 设备码有效时长，单位为秒
    :param interval_seconds: GitHub 要求的最短授权轮询间隔，单位为秒
    """

    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval_seconds: int


@dataclass(frozen=True, slots=True)
class GithubTokenExchange:
    """
    GitHub 设备码或刷新请求的规范化结果。

    :param access_token: 授权成功后的访问令牌；尚未授权或失败时为 None
    :param refresh_token: 用于更新访问令牌的刷新令牌；未返回时为 None
    :param expires_in: 访问令牌有效时长，单位为秒；未返回时为 None
    :param refresh_token_expires_in: 刷新令牌有效时长，单位为秒；未返回时为 None
    :param scope: GitHub 返回的授权范围文本；未返回时为 None
    :param error: GitHub OAuth 错误代码；成功时为 None
    """

    access_token: str | None = None
    refresh_token: str | None = None
    expires_in: int | None = None
    refresh_token_expires_in: int | None = None
    scope: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class GithubUser:
    """
    GitHub 当前用户的最小身份摘要。

    :param login: GitHub 核验后的登录名
    :param user_id: GitHub 分配的数字用户标识；兼容未提供标识时默认为 0
    """

    login: str
    user_id: int = 0


# Wiki 名单的协议版本；结构不兼容时由名单方提升版本，旧实例按拉取失败处理而不是误读字段。
COMMUNITY_MEDAL_SCHEMA = 1


@dataclass(frozen=True, slots=True)
class GithubMedalSnapshot:
    """
    Wiki 勋章名单中某个 GitHub 数字 ID 的记录与名单生成时间。

    ``medals`` 保留名单原始记录（已在协议边界校验），未认识的勋章键与字段原样透传，
    名单中没有该 ID 时为空元组，表示已核对但暂无勋章。

    :param generated_at: 勋章名单的生成时间文本
    :param medals: 已校验的原始勋章记录；名单中无该用户时为空元组
    """

    generated_at: str
    medals: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class GithubCommunityProfile:
    """
    社区勋章的脱敏快照；不可用与确证暂无勋章是不同状态。

    state 取值：unconfigured、ready、unavailable，以及本机或服务端开关未生效时的 disabled。

    :param state: 配置和查询状态：unconfigured 未配置、ready 可用、unavailable 不可用、disabled 已禁用
    :param login: 已核验的 GitHub 登录名；尚未核验时为 None
    :param user_id: 已核验的 GitHub 数字标识；尚未核验时为 None
    :param medals: 当前可用的勋章记录；确证暂无勋章时为空元组
    :param generated_at: 名单生成时间文本；名单不可用时为 None
    :param checked_at: 本机最近检查的时间戳，单位为秒；尚未检查时为 None
    :param stale: 当前结果是否来自已过期但仍允许展示的缓存
    :param refreshing: 后台正在预热或更新，读取接口不会等待其完成
    """

    state: str = "unconfigured"
    login: str | None = None
    user_id: int | None = None
    medals: tuple[dict[str, Any], ...] = ()
    generated_at: str | None = None
    checked_at: int | None = None
    stale: bool = False
    refreshing: bool = False


class GithubCommunityPort(Protocol):
    """
    核验 GitHub 身份并读取 Wiki 社区勋章名单的外部端口。
    """

    async def get_user(self, access_token: str) -> GithubUser:
        """
        使用 Token 校验身份，不接受前端声明的 GitHub 用户名。

        :param access_token: 用于向 GitHub 核验身份的访问令牌
        :return: 核验后的 GitHub 登录名和数字用户标识
        :raises GithubAuthTransportError: 外部传输失败或响应不符合约定协议
        """
        ...

    async def get_medal_snapshot(self, user_id: int) -> GithubMedalSnapshot:
        """
        拉取公开勋章名单并取出指定数字 ID 的记录；名单不可用或格式不符时抛出传输错误。

        :param user_id: 已核验的 GitHub 数字用户标识
        :return: 名单生成时间与该用户的勋章记录；无记录时 medals 为空元组
        :raises GithubAuthTransportError: 外部传输失败或响应不符合约定协议
        """
        ...


class GithubAuthPort(Protocol):
    """
    GitHub OAuth 外部协议所需的最小传输端口。
    """

    async def request_device_code(
        self,
        client_id: str,
        scope: str = "read:user",
    ) -> GithubDeviceCode:
        """
        按调用方 scope 申请设备码，默认保留 Copilot 登录的只读用户权限。

        :param client_id: GitHub OAuth 应用的客户端标识
        :param scope: 空格分隔的权限范围，默认仅申请 read:user
        :return: 设备码、用户验证码、授权地址、有效期及轮询间隔
        :raises GithubAuthTransportError: 外部传输失败或响应不符合约定协议
        """
        ...

    async def exchange_device_code(
        self,
        client_id: str,
        device_code: str,
    ) -> GithubTokenExchange:
        """
        交换设备码并返回授权状态或 Token。

        :param client_id: 申请设备码时使用的 OAuth 客户端标识
        :param device_code: GitHub 签发的设备码
        :return: 令牌交换结果，未完成或被拒绝的授权状态通过 error 字段表达
        :raises GithubAuthTransportError: 外部传输失败或响应不符合约定协议
        """
        ...

    async def refresh_access_token(
        self,
        client_id: str,
        refresh_token: str,
    ) -> GithubTokenExchange:
        """
        使用 GitHub OAuth 刷新 Token。

        :param client_id: GitHub OAuth 应用的客户端标识
        :param refresh_token: 已保存的 OAuth 刷新令牌
        :return: 刷新后的令牌信息，OAuth 错误通过 error 字段表达
        :raises GithubAuthTransportError: 外部传输失败或响应不符合约定协议
        """
        ...

    async def get_user(self, access_token: str) -> GithubUser:
        """
        校验 Token 并读取 GitHub 登录名。

        :param access_token: 待校验的 GitHub 访问令牌
        :return: 令牌所属的 GitHub 登录名和数字用户标识
        :raises GithubAuthTransportError: 外部传输失败或响应不符合约定协议
        """
        ...


class GithubSettingsReader(Protocol):
    """
    为 GitHub 网络适配器提供最小只读部署设置接口。
    """

    def get(self, key: str, default: Any = None) -> Any:
        """
        读取一个部署设置值。

        :param key: 部署设置名称
        :param default: 设置缺失时返回的默认值
        :return: 对应设置的值，缺失时返回 default
        """
        ...
