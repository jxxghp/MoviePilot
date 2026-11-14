"""
GitHub OAuth Device Flow 与社区勋章名单的网络适配器。
"""

from __future__ import annotations

import re
from typing import Any

from app.adapters.network.http import AsyncRequestUtils
from app.domain.github import (
    COMMUNITY_MEDAL_SCHEMA,
    GithubAuthPort,
    GithubAuthTransportError,
    GithubDeviceCode,
    GithubMedalSnapshot,
    GithubSettingsReader,
    GithubTokenExchange,
    GithubUser,
)
from app.foundation.url import UrlUtils
from app.runtime.settings import get_runtime_setting

# Device Flow 的 client_id 本身不是秘密；沿用当前内置 Copilot 登录使用的公开应用标识，
# 后续如果注册专用 OAuth App，只需在组合根替换这一常量，不会影响授权协议和存储合同。
GITHUB_DEVICE_CLIENT_ID = "Ov23li8tweQw6odWQebz"

# 社区勋章公开名单，由 MoviePilot-Wiki 定时任务计算与人工登记后发布。
COMMUNITY_MEDALS_URL = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Wiki/main/community/medals.json"

# 已知字段的类型约束；名单记录中这些字段类型不符时整条丢弃，其余字段原样透传。
_MEDAL_DATE = re.compile(r"\d{4}(-\d{2}-\d{2})?")


class _RuntimeGithubSettings:
    """
    把 runtime 设置读取端口适配为 GitHub 客户端所需的 get 接口。
    """

    def get(self, key: str, default: Any = None) -> Any:
        """
        读取一个运行时设置，并在启动早期保留默认值语义。
        """
        return get_runtime_setting(key, default)


class GithubAuthClient(GithubAuthPort):
    """
    通过 MoviePilot 统一异步 HTTP 适配器访问 GitHub OAuth API。
    """

    _DEVICE_CODE_URL = "https://github.com/login/device/code"
    _ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
    _USER_URL = "https://api.github.com/user"

    def __init__(self, settings: GithubSettingsReader | None = None) -> None:
        """
        保存代理、User-Agent 等部署设置提供器。

        :param settings: 代理和请求头配置读取器；省略时读取运行时设置
        """
        self._settings = settings or _RuntimeGithubSettings()

    async def request_device_code(
        self,
        client_id: str,
        scope: str = "read:user",
    ) -> GithubDeviceCode:
        """
        按调用方 scope 请求设备码，默认保留 Copilot 登录的只读用户权限。

        :param client_id: GitHub OAuth 应用的公开客户端标识
        :param scope: 请求的 OAuth 权限范围，默认为只读用户信息
        :return: 设备码、用户授权地址与有效期、轮询间隔
        :raises GithubAuthTransportError: 请求失败、服务端拒绝或设备授权响应无效
        """
        try:
            response = await self._request().post_res(
                self._DEVICE_CODE_URL,
                json={"client_id": client_id, "scope": scope},
                raise_exception=True,
            )
        # 网络异常统一转换为领域传输错误
        except Exception as error:  # noqa: BLE001
            raise GithubAuthTransportError("无法连接 GitHub，请稍后重试。") from error
        payload = self._json_payload(response)
        self._raise_payload_error(payload, "申请 GitHub 设备码失败")
        try:
            return GithubDeviceCode(
                device_code=str(payload["device_code"]),
                user_code=str(payload["user_code"]),
                verification_uri=str(payload.get("verification_uri") or payload.get("verification_url") or ""),
                expires_in=max(int(payload.get("expires_in") or 900), 1),
                interval_seconds=max(int(payload.get("interval") or 5), 1),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise GithubAuthTransportError("GitHub 返回的设备授权信息无效") from error

    async def exchange_device_code(
        self,
        client_id: str,
        device_code: str,
    ) -> GithubTokenExchange:
        """
        轮询 GitHub 设备码，并保留协议层授权状态。

        :param client_id: 申请设备码的 OAuth 应用标识
        :param device_code: GitHub 返回的设备授权码
        :return: 本次轮询的令牌或 OAuth 错误状态；等待授权等状态不作为传输异常
        :raises GithubAuthTransportError: 网络失败或响应不是有效 JSON 对象
        """
        try:
            response = await self._request().post_res(
                self._ACCESS_TOKEN_URL,
                json={
                    "client_id": client_id,
                    "device_code": device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
                raise_exception=True,
            )
        # 网络异常统一转换为领域传输错误
        except Exception as error:  # noqa: BLE001
            raise GithubAuthTransportError("无法连接 GitHub，请稍后重试。") from error
        return self._exchange_payload(self._json_payload(response))

    async def refresh_access_token(
        self,
        client_id: str,
        refresh_token: str,
    ) -> GithubTokenExchange:
        """
        使用 GitHub 返回的 refresh_token 申请新的访问 Token。

        :param client_id: 原授权所属的 OAuth 应用标识
        :param refresh_token: GitHub 颁发的刷新令牌
        :return: 更新后的令牌或服务端返回的 OAuth 错误状态
        :raises GithubAuthTransportError: 网络失败或响应不是有效 JSON 对象
        """
        try:
            response = await self._request().post_res(
                self._ACCESS_TOKEN_URL,
                json={
                    "client_id": client_id,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
                raise_exception=True,
            )
        # 网络异常统一转换为领域传输错误
        except Exception as error:  # noqa: BLE001
            raise GithubAuthTransportError("无法连接 GitHub，请稍后重试。") from error
        return self._exchange_payload(self._json_payload(response))

    async def get_user(self, access_token: str) -> GithubUser:
        """
        调用 GitHub 用户接口验证 Token 并读取登录名。

        :param access_token: 用于查询当前用户的 GitHub 访问令牌
        :return: 经响应校验的数字用户 ID 与登录名
        :raises GithubAuthTransportError: 请求失败、令牌失效或用户信息无效；401 响应会标记 unauthorized
        """
        try:
            response = await self._request(headers={"Authorization": f"Bearer {access_token}"}).get_res(
                self._USER_URL,
                raise_exception=True,
            )
        # 网络异常统一转换为领域传输错误
        except Exception as error:  # noqa: BLE001
            raise GithubAuthTransportError("无法连接 GitHub，请稍后重试。") from error
        if response is None:
            raise GithubAuthTransportError("GitHub 未返回用户校验结果")
        if response.status_code == 401:
            raise GithubAuthTransportError("GitHub Token 已失效", unauthorized=True)
        if response.status_code >= 400:
            raise GithubAuthTransportError("GitHub Token 校验失败")
        payload = self._json_payload(response)
        login = str(payload.get("login") or "").strip()
        if not login:
            raise GithubAuthTransportError("GitHub 未返回用户信息")
        user_id = payload.get("id")
        if not isinstance(user_id, int) or user_id <= 0:
            raise GithubAuthTransportError("GitHub 未返回有效的用户身份")
        return GithubUser(login=login, user_id=user_id)

    async def get_medal_snapshot(self, user_id: int) -> GithubMedalSnapshot:
        """
        拉取 Wiki 勋章名单并只返回该数字 ID 的记录。

        名单是公开文件，请求不携带实例 Token，避免凭据泄露给 GitHub 镜像站。
        配置 ``GITHUB_PROXY`` 时先走镜像站，失败再经 ``PROXY``（未配置时直连）访问原地址，
        与插件市场读取 Wiki 的出口顺序一致。

        :param user_id: 已验证的 GitHub 数字用户 ID
        :return: 名单生成时间与该用户的有效勋章记录；没有记录时列表为空
        :raises GithubAuthTransportError: 所有出口均未取得有效名单
        """
        cause: Exception | None = None
        for url, direct in self._medal_routes():
            try:
                response = await self._request(direct=direct).get_res(url, raise_exception=True)
                if response is None or response.status_code >= 400:
                    raise GithubAuthTransportError("社区勋章名单请求失败")
                return self._medal_snapshot(self._json_payload(response), user_id)
            # 镜像站可能以 200 返回错误页，继续尝试下一出口
            except Exception as error:  # noqa: BLE001
                cause = error
        raise GithubAuthTransportError("暂时无法获取社区勋章名单") from cause

    def _medal_routes(self) -> list[tuple[str, bool]]:
        """
        返回勋章名单的请求地址及是否绕过部署代理；镜像站优先且不叠加代理。

        :return: 按尝试顺序排列的地址与直连标志；镜像优先，原始地址兜底
        """
        routes: list[tuple[str, bool]] = []
        mirror = str(self._settings.get("GITHUB_PROXY") or "").strip()
        if mirror:
            routes.append((f"{UrlUtils.standardize_base_url(mirror)}{COMMUNITY_MEDALS_URL}", True))
        routes.append((COMMUNITY_MEDALS_URL, False))
        return routes

    @classmethod
    def _medal_snapshot(cls, payload: dict[str, Any], user_id: int) -> GithubMedalSnapshot:
        """
        校验名单顶层协议并取出目标 ID 的记录；其他持有人的记录不在此处展开校验。

        名单形如 ``holders["<数字 ID>"] = {"login": ..., "medals": [...]}``；持有人对象中的
        ``login`` 等其他字段与顶层 ``unlinked``（无 GitHub 账号的共建者）都不参与实例查表。

        :param payload: 勋章名单 JSON 对象
        :param user_id: 要提取勋章的 GitHub 数字用户 ID
        :return: 过滤无效条目后的勋章快照；用户不存在时勋章为空
        :raises GithubAuthTransportError: 名单版本或目标持有人结构无效
        """
        schema = payload.get("schema")
        generated_at = payload.get("generated_at")
        holders = payload.get("holders")
        if type(schema) is not int or schema != COMMUNITY_MEDAL_SCHEMA:
            raise GithubAuthTransportError("社区勋章名单版本不受支持")
        if not isinstance(generated_at, str) or not generated_at.strip() or not isinstance(holders, dict):
            raise GithubAuthTransportError("社区勋章名单格式无效")
        holder = holders.get(str(user_id))
        if holder is None:
            records: list[Any] = []
        elif isinstance(holder, dict) and isinstance(holder.get("medals"), list):
            records = holder["medals"]
        else:
            raise GithubAuthTransportError("社区勋章名单格式无效")
        return GithubMedalSnapshot(
            generated_at=generated_at.strip(),
            medals=tuple(record for record in records if cls._valid_medal(record)),
        )

    @staticmethod
    def _valid_medal(record: Any) -> bool:
        """
        判断单条勋章记录可否发布。

        记录必须是带非空字符串 ``key`` 的对象；未认识的勋章键和字段为向前兼容原样透传，
        已知字段类型不符时整条丢弃，避免前端渲染出半残勋章，也不让一条坏记录拖垮整份名单。

        :param record: 待检查的单条勋章数据
        :return: 满足键名和已知字段约束时返回 True，否则返回 False
        """
        if not isinstance(record, dict) or not isinstance(record.get("key"), str) or not record["key"]:
            return False
        tier = record.get("tier")
        years = record.get("years")
        checks = (
            tier is None or (type(tier) is int and 1 <= tier <= 4),
            "at" not in record or (isinstance(record["at"], str) and _MEDAL_DATE.fullmatch(record["at"]) is not None),
            years is None or (isinstance(years, list) and all(type(year) is int for year in years)),
            "facts" not in record or isinstance(record["facts"], dict),
            "serial" not in record or record["serial"] is None or type(record["serial"]) in (int, str),
            all(field not in record or isinstance(record[field], str) for field in ("specialty", "basis", "note")),
        )
        return all(checks)

    def _request(self, *, headers: dict[str, str] | None = None, direct: bool = False) -> AsyncRequestUtils:
        """
        构造 GitHub 请求客户端，不复用当前业务 Token 请求头；访问镜像站时不叠加部署代理。

        :param headers: 合并到默认 JSON 请求头的附加字段，可显式提供授权信息
        :param direct: 是否绕过部署代理；访问镜像站时为 True
        :return: 超时 20 秒、验证证书且不读取环境代理的异步请求适配器
        """
        request_headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": str(self._settings.get("USER_AGENT") or "MoviePilot"),
        }
        request_headers.update(headers or {})
        return AsyncRequestUtils(
            headers=request_headers,
            proxies=None if direct else self._settings.get("PROXY"),
            timeout=20,
            verify=True,
            trust_env=False,
        )

    @staticmethod
    def _json_payload(response: Any) -> dict[str, Any]:
        """
        读取 GitHub JSON 响应，避免把原始响应文本写入业务错误。

        :param response: 提供 json 方法的 HTTP 响应
        :return: 解析后的 JSON 对象
        :raises GithubAuthTransportError: 解析失败或响应顶层不是对象
        """
        try:
            payload = response.json()
        # 外部 JSON 异常统一收敛
        except Exception as error:  # noqa: BLE001
            raise GithubAuthTransportError("GitHub 返回了无效响应") from error
        if not isinstance(payload, dict):
            raise GithubAuthTransportError("GitHub 返回了无效响应")
        return payload

    @staticmethod
    def _raise_payload_error(payload: dict[str, Any], message: str) -> None:
        """
        将 GitHub OAuth 错误字段转换为不泄露响应原文的业务异常。

        :param payload: 已解析的 OAuth 响应对象
        :param message: 发现错误时使用的安全错误说明
        :raises GithubAuthTransportError: 响应带有非空 error 字段
        """
        if payload.get("error"):
            raise GithubAuthTransportError(message)

    @classmethod
    def _exchange_payload(cls, payload: dict[str, Any]) -> GithubTokenExchange:
        """
        把设备码或刷新响应转换为统一 Token 交换结果。

        :param payload: 设备授权或刷新令牌的响应对象
        :return: 规范化后的令牌和错误状态；缺失或无法转换的可选值为 None
        """
        return GithubTokenExchange(
            access_token=cls._optional_text(payload.get("access_token")),
            refresh_token=cls._optional_text(payload.get("refresh_token")),
            expires_in=cls._optional_int(payload.get("expires_in")),
            refresh_token_expires_in=cls._optional_int(payload.get("refresh_token_expires_in")),
            scope=cls._optional_text(payload.get("scope")),
            error=cls._optional_text(payload.get("error")),
        )

    @staticmethod
    def _optional_text(value: Any) -> str | None:
        """
        规范化 GitHub 响应中的可选文本字段。

        :param value: 服务端返回的可选文本值
        :return: 转为文本并去除首尾空白后的值；空值或假值返回 None
        """
        text = str(value or "").strip()
        return text or None

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        """
        规范化 GitHub 响应中的可选整数时间字段。

        :param value: 服务端返回的秒数或其他可转整数值
        :return: 转换后的整数；缺失或不可转换时返回 None
        """
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None
