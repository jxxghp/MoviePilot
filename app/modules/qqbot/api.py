"""
QQ Bot API - Python 实现
参考 QQ 开放平台官方 API: https://bot.q.qq.com/wiki/develop/api/
"""

import time
from typing import Optional, Literal

from app.runtime.log import logger
from app.adapters.network.http import RequestUtils

API_BASE = "https://api.sgroup.qq.com"
TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"

# Token 缓存
_cached_token: Optional[dict] = None


def get_access_token(app_id: str, client_secret: str) -> str:
    """
    获取 AccessToken（带缓存，提前 5 分钟刷新）
    """
    global _cached_token
    now_ms = int(time.time() * 1000)
    if _cached_token and now_ms < _cached_token["expires_at"] - 5 * 60 * 1000 and _cached_token["app_id"] == app_id:
        return _cached_token["token"]

    if _cached_token and _cached_token["app_id"] != app_id:
        _cached_token = None

    try:
        resp = RequestUtils(timeout=30).post_res(
            TOKEN_URL,
            json={"appId": app_id, "clientSecret": client_secret},  # QQ API 使用 camelCase
            headers={"Content-Type": "application/json"},
        )
        if not resp or not resp.json():
            raise ValueError("Failed to get access_token: empty response")
        data = resp.json()
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
        return token
    except Exception as e:
        logger.error(f"QQ API: get_access_token failed: {e}")
        raise


def clear_token_cache() -> None:
    """清除 Token 缓存"""
    global _cached_token
    _cached_token = None


def _api_request(
    access_token: str,
    method: str,
    path: str,
    body: Optional[dict] = None,
    timeout: int = 30,
) -> dict:
    """通用 API 请求"""
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
    keyboard: Optional[dict] = None,
) -> dict:
    """
    主动发送 C2C 单聊消息（不需要 msg_id）
    注意：每月限 4 条/用户，且用户必须曾与机器人交互过
    :param access_token: 访问令牌
    :param openid: 用户 openid
    :param content: 消息内容
    :param use_markdown: 是否使用 Markdown 格式（需机器人开通 Markdown 能力）
    :param keyboard: 键盘按钮配置
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
    keyboard: Optional[dict] = None,
) -> dict:
    """
    主动发送群聊消息（不需要 msg_id）
    注意：每月限 4 条/群，且群必须曾与机器人交互过
    :param access_token: 访问令牌
    :param group_openid: 群聊 openid
    :param content: 消息内容
    :param use_markdown: 是否使用 Markdown 格式（需机器人开通 Markdown 能力）
    :param keyboard: 键盘按钮配置
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
    keyboard: Optional[dict] = None,
) -> dict:
    """被动回复 C2C 单聊消息（1 小时内最多 4 次）"""
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
    keyboard: Optional[dict] = None,
) -> dict:
    """被动回复群聊消息（1 小时内最多 4 次）"""
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
    获取 WebSocket Gateway URL
    """
    data = _api_request(access_token, "GET", "/gateway")
    url = data.get("url")
    if not url:
        raise ValueError("Gateway URL not found in response")
    return url


def send_message(
    access_token: str,
    target: str,
    content: str,
    msg_type: Literal["c2c", "group"] = "c2c",
    msg_id: Optional[str] = None,
    keyboard: Optional[dict] = None,
) -> dict:
    """
    统一发送接口
    :param access_token: 访问令牌
    :param target: openid（c2c）或 group_openid（group）
    :param content: 消息内容
    :param msg_type: c2c 单聊 / group 群聊
    :param msg_id: 可选，被动回复时传入原消息 id
    :param keyboard: 可选，键盘按钮配置
    """
    if msg_id:
        if msg_type == "c2c":
            return send_c2c_message(access_token, target, content, msg_id, keyboard)
        return send_group_message(access_token, target, content, msg_id, keyboard)
    if msg_type == "c2c":
        return send_proactive_c2c_message(access_token, target, content, keyboard=keyboard)
    return send_proactive_group_message(access_token, target, content, keyboard=keyboard)
