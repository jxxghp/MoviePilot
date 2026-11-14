"""
QQ 开放平台令牌缓存、消息发送和网关地址查询。

接口参考：https://bot.q.qq.com/wiki/develop/api/
"""

import time
from typing import Any, Literal, Optional

from app.adapters.network.http import RequestUtils
from app.runtime.log import logger

API_BASE = "https://api.sgroup.qq.com"
TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"

# 进程仅保留一个应用的访问令牌与毫秒级过期时间。
_cached_token: Optional[dict[str, Any]] = None


def get_access_token(app_id: str, client_secret: str) -> str:
    """
    获取 QQ 访问令牌，复用尚未进入刷新窗口的缓存。

    进程只保留一个应用的令牌；到期前 5 分钟重新获取，切换应用会清除旧缓存。
    请求或响应解析失败时记录错误并继续抛出异常。

    :param app_id: 机器人应用标识，用于区分缓存归属
    :param client_secret: 机器人应用密钥
    :return: 有效访问令牌字符串
    :raises ValueError: 响应为空、格式无效或缺少有效令牌
    """
    global _cached_token
    now_ms = int(time.time() * 1000)
    if _cached_token and now_ms < _cached_token["expires_at"] - 5 * 60 * 1000 and _cached_token["app_id"] == app_id:
        token = _cached_token.get("token")
        if isinstance(token, str):
            return token
        _cached_token = None

    if _cached_token and _cached_token["app_id"] != app_id:
        _cached_token = None

    try:
        resp = RequestUtils(timeout=30).post_res(
            TOKEN_URL,
            # QQ API 要求此处使用驼峰命名的字段。
            json={"appId": app_id, "clientSecret": client_secret},
            headers={"Content-Type": "application/json"},
        )
        if not resp or not resp.json():
            raise ValueError("Failed to get access_token: empty response")
        data = resp.json()
        if not isinstance(data, dict):
            raise ValueError(f"Failed to get access_token: invalid response {data!r}")
        token = data.get("access_token")
        expires_in = data.get("expires_in", 7200)
        if not token:
            raise ValueError(f"Failed to get access_token: {data}")

        # expires_in 可能为字符串，统一转为 int
        expires_in = int(expires_in) if expires_in is not None else 7200

        _cached_token = {
            "token": token,
            "expires_at": now_ms + expires_in * 1000,
            "app_id": app_id,
        }
        logger.debug(f"QQ API: Token cached for app_id={app_id}")
        if not isinstance(token, str):
            raise ValueError("Invalid access_token response")
        return token
    except Exception as e:
        logger.error(f"QQ API: get_access_token failed: {e}")
        raise


def clear_token_cache() -> None:
    """
    清除 Token 缓存。
    """
    global _cached_token
    _cached_token = None


def _api_request(
    access_token: str,
    method: str,
    path: str,
    body: Optional[dict[str, Any]] = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """
    携带机器人令牌请求 QQ API，并要求返回对象响应。

    :param access_token: 机器人访问令牌
    :param method: GET 使用查询请求，其他值均使用 POST
    :param path: 相对于 QQ API 根地址的接口路径
    :param body: POST JSON 载荷；未提供时发送空对象
    :param timeout: 请求超时时间，单位秒
    :return: 原始响应对象
    :raises ValueError: 响应为空、不是对象或 HTTP 状态表示失败
    """
    url = f"{API_BASE}{path}"
    headers = {
        "Authorization": f"QQBot {access_token}",
        "Content-Type": "application/json",
    }
    try:
        if method.upper() == "GET":
            resp = RequestUtils(timeout=timeout).get_res(url, headers=headers)
        else:
            resp = RequestUtils(timeout=timeout).post_res(
                url, json=body or {}, headers=headers
            )
        if not resp:
            raise ValueError("Empty response")
        data = resp.json()
        if not isinstance(data, dict):
            raise ValueError(f"API Error [{path}]: invalid response {data!r}")
        status = getattr(resp, "status_code", 0)
        if status and status >= 400:
            raise ValueError(f"API Error [{path}]: {data.get('message', data)}")
        return data
    except Exception as e:
        logger.error(f"QQ API: {method} {path} failed: {e}")
        raise


def send_proactive_c2c_message(
    access_token: str,
    openid: str,
    content: str,
    use_markdown: bool = False,
    keyboard: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """
    主动发送单聊消息。

    配额和可发送对象由 QQ 平台校验；本地不记录配额，接口失败时抛出异常。

    :param access_token: 机器人访问令牌
    :param openid: 接收用户 openid
    :param content: 待发送正文；主动发送时会去掉首尾空白
    :param use_markdown: 是否使用已获授权的 Markdown 消息能力
    :param keyboard: 可选按钮配置
    :return: QQ API 返回的原始消息对象
    :raises ValueError: 内容为空或请求响应无效
    """
    if not content or not content.strip():
        raise ValueError("主动消息内容不能为空")
    content = content.strip()
    body = {"markdown": {"content": content}, "msg_type": 2} if use_markdown else {"content": content, "msg_type": 0}
    if keyboard:
        body["keyboard"] = {"content": keyboard}
    return _api_request(
        access_token, "POST", f"/v2/users/{openid}/messages", body
    )


def send_proactive_group_message(
    access_token: str,
    group_openid: str,
    content: str,
    use_markdown: bool = False,
    keyboard: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """
    主动发送群聊消息。

    配额和可发送对象由 QQ 平台校验；本地不记录配额，接口失败时抛出异常。

    :param access_token: 机器人访问令牌
    :param group_openid: 接收群聊 openid
    :param content: 待发送正文；主动发送时会去掉首尾空白
    :param use_markdown: 是否使用已获授权的 Markdown 消息能力
    :param keyboard: 可选按钮配置
    :return: QQ API 返回的原始消息对象
    :raises ValueError: 内容为空或请求响应无效
    """
    if not content or not content.strip():
        raise ValueError("主动消息内容不能为空")
    content = content.strip()
    body = {"markdown": {"content": content}, "msg_type": 2} if use_markdown else {"content": content, "msg_type": 0}
    if keyboard:
        body["keyboard"] = {"content": keyboard}
    return _api_request(
        access_token, "POST", f"/v2/groups/{group_openid}/messages", body
    )


def send_c2c_message(
    access_token: str,
    openid: str,
    content: str,
    msg_id: Optional[str] = None,
    keyboard: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """
    被动回复单聊消息。

    配额和可发送对象由 QQ 平台校验；本地不记录配额，接口失败时抛出异常。
    当前固定使用 msg_seq=1；调用方仍需遵守平台的回复时限和次数限制。

    :param access_token: 机器人访问令牌
    :param openid: 接收用户 openid
    :param content: 待发送正文；主动发送时会去掉首尾空白
    :param msg_id: 原始消息标识；提供时随请求发送以关联被动回复
    :param keyboard: 可选按钮配置
    :return: QQ API 返回的原始消息对象
    """
    body = {"content": content, "msg_type": 0, "msg_seq": 1}
    if msg_id:
        body["msg_id"] = msg_id
    if keyboard:
        body["keyboard"] = {"content": keyboard}
    return _api_request(
        access_token, "POST", f"/v2/users/{openid}/messages", body
    )


def send_group_message(
    access_token: str,
    group_openid: str,
    content: str,
    msg_id: Optional[str] = None,
    keyboard: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """
    被动回复群聊消息。

    配额和可发送对象由 QQ 平台校验；本地不记录配额，接口失败时抛出异常。
    当前固定使用 msg_seq=1；调用方仍需遵守平台的回复时限和次数限制。

    :param access_token: 机器人访问令牌
    :param group_openid: 接收群聊 openid
    :param content: 待发送正文；主动发送时会去掉首尾空白
    :param msg_id: 原始消息标识；提供时随请求发送以关联被动回复
    :param keyboard: 可选按钮配置
    :return: QQ API 返回的原始消息对象
    """
    body = {"content": content, "msg_type": 0, "msg_seq": 1}
    if msg_id:
        body["msg_id"] = msg_id
    if keyboard:
        body["keyboard"] = {"content": keyboard}
    return _api_request(
        access_token, "POST", f"/v2/groups/{group_openid}/messages", body
    )


def get_gateway_url(access_token: str) -> str:
    """
    查询用于接收入站消息的 WebSocket 网关地址。

    :param access_token: 机器人访问令牌
    :return: 服务端返回的非空网关地址
    :raises ValueError: 请求失败或响应缺少有效网关地址
    """
    data = _api_request(access_token, "GET", "/gateway")
    url = data.get("url")
    if not isinstance(url, str) or not url:
        raise ValueError("Gateway URL not found in response")
    return url


def send_message(
    access_token: str,
    target: str,
    content: str,
    msg_type: Literal["c2c", "group"] = "c2c",
    msg_id: Optional[str] = None,
    keyboard: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """
    按是否携带原消息标识选择主动发送或被动回复接口。

    内容、额度、权限或请求失败时由底层接口抛出异常；本函数不进行重试。

    :param access_token: 机器人访问令牌
    :param target: 单聊用户 openid 或群聊 group_openid
    :param content: 待发送消息正文
    :param msg_type: c2c 表示单聊，group 表示群聊
    :param msg_id: 原始消息标识；非空时使用被动回复接口
    :param keyboard: 可选按钮配置
    :return: QQ API 返回的原始消息对象
    """
    if msg_id:
        if msg_type == "c2c":
            return send_c2c_message(access_token, target, content, msg_id, keyboard)
        return send_group_message(access_token, target, content, msg_id, keyboard)
    if msg_type == "c2c":
        return send_proactive_c2c_message(access_token, target, content, keyboard=keyboard)
    return send_proactive_group_message(access_token, target, content, keyboard=keyboard)
