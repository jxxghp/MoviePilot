"""LLM Provider 持久鉴权和外部授权协议的唯一 owner。"""

from __future__ import annotations

import base64
import copy
import hashlib
import html
import json
import secrets
import time
from typing import Any, Optional
from urllib.parse import urlencode

import jwt

from app.agent.llm.runtime import LLMProviderAuthError
from app.agent.llm.session import PendingAuthSession
from app.application.configuration import get_configured_system_config
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.schemas.types import SystemConfigKey


def render_auth_result_html(success: bool, message: str) -> str:
    """生成 OAuth 回调落地页，并转义所有外部可控文本。"""
    title = "授权成功" if success else "授权失败"
    accent = "#3aa675" if success else "#e24b4b"
    safe_title = html.escape(title, quote=True)
    safe_message = html.escape(str(message or ""), quote=True)
    event_payload = (
        json.dumps(
            {"type": "moviepilot-llm-auth", "success": success},
            ensure_ascii=False,
        )
        .replace("<", "\u003c")
        .replace(">", "\u003e")
        .replace("&", "\u0026")
    )
    return f"""<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{safe_title}</title>
    <style>
      body {{
        margin: 0;
        min-height: 100vh;
        display: flex;
        align-items: center;
        justify-content: center;
        background: #101418;
        color: #f3f5f7;
        font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      }}
      .card {{
        width: min(480px, calc(100vw - 32px));
        padding: 28px 24px;
        border-radius: 18px;
        background: rgba(20, 28, 36, 0.92);
        box-shadow: 0 18px 48px rgba(0, 0, 0, 0.28);
      }}
      h1 {{
        margin: 0 0 12px;
        font-size: 24px;
        color: {accent};
      }}
      p {{
        margin: 0;
        line-height: 1.7;
        color: #d4dbe3;
      }}
    </style>
  </head>
  <body>
    <div class="card">
      <h1>{safe_title}</h1>
      <p>{safe_message}</p>
    </div>
    <script>
      if (window.opener) {{
        try {{
          window.opener.postMessage({event_payload}, "*");
        }} catch (err) {{}}
      }}
      setTimeout(() => window.close(), 1800);
    </script>
  </body>
</html>"""


class _ProviderAuth:
    """LLM Provider 持久鉴权和外部授权协议的唯一 owner。"""

    _lock: Any
    _pending_sessions: dict[str, PendingAuthSession]
    _oauth_state_index: dict[str, str]

    def __getattr__(self, name: str) -> Any:
        """将跨 owner 调用交给最终 Facade 的 MRO 解析。"""
        raise AttributeError(name)

    _CHATGPT_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"

    _CHATGPT_ISSUER = "https://auth.openai.com"

    _CHATGPT_CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"

    _COPILOT_CLIENT_ID = "Ov23li8tweQw6odWQebz"

    @staticmethod
    def _read_agent_config() -> dict[str, Any]:
        """读取 AI Agent 配置信息。"""
        config = get_configured_system_config().get(SystemConfigKey.AIAgentConfig)
        if isinstance(config, dict):
            return config
        return {}

    @staticmethod
    async def _write_agent_config(value: dict[str, Any]) -> None:
        """
        使用异步持久化写回 provider 鉴权配置。

        `get_configured_system_config().get()` 读取的是内存缓存，这里保留同步调用；
        但写入需要落库，因此统一走 `async_set()`。
        """
        await get_configured_system_config().async_set(
            SystemConfigKey.AIAgentConfig,
            copy.deepcopy(value) or None,
        )

    def _get_auth_store(self) -> dict[str, Any]:
        """获取所有鉴权数据。"""
        config = self._read_agent_config()
        auth_store = config.get("provider_auth")
        if isinstance(auth_store, dict):
            return auth_store
        return {}

    def get_saved_auth(self, provider_id: str) -> dict[str, Any] | None:
        """读取持久化 provider 鉴权信息。"""
        return copy.deepcopy(self._get_auth_store().get(provider_id))

    async def save_auth(self, provider_id: str, auth_data: dict[str, Any]) -> None:
        """写入 provider 鉴权信息。"""
        config = self._read_agent_config()
        auth_store = config.get("provider_auth")
        if not isinstance(auth_store, dict):
            auth_store = {}
        auth_store[provider_id] = copy.deepcopy(auth_data)
        config["provider_auth"] = auth_store
        await self._write_agent_config(config)

    async def clear_auth(self, provider_id: str) -> None:
        """移除 provider 鉴权信息。"""
        config = self._read_agent_config()
        auth_store = config.get("provider_auth")
        if not isinstance(auth_store, dict):
            return
        auth_store.pop(provider_id, None)
        if auth_store:
            config["provider_auth"] = auth_store
        else:
            config.pop("provider_auth", None)
        await self._write_agent_config(config)

    def get_auth_status(self, provider_id: str) -> dict[str, Any]:
        """返回前端展示用的 provider 鉴权摘要。"""
        auth = self.get_saved_auth(provider_id)
        if not auth:
            return {"connected": False}
        return {
            "connected": True,
            "type": auth.get("type"),
            "label": auth.get("label") or auth.get("email") or auth.get("account_id") or "已授权",
            "expires_at": auth.get("expires_at"),
            "updated_at": auth.get("updated_at"),
        }

    @staticmethod
    def _jwt_claims(token: str) -> dict[str, Any]:
        """解析 JWT token 内容（不验证签名）。"""
        try:
            payload = jwt.decode(token, options={"verify_signature": False})
            return payload if isinstance(payload, dict) else {}
        except Exception as err:
            logger.debug(f"解析 JWT token 内容失败: {err}")
            return {}

    @staticmethod
    def _extract_chatgpt_account_id(token_payload: dict[str, Any]) -> Optional[str]:
        """从 ChatGPT 的 Token payload 中提取 account id。"""
        if token_payload.get("chatgpt_account_id"):
            return str(token_payload["chatgpt_account_id"])
        auth_payload = token_payload.get("https://api.openai.com/auth") or {}
        if isinstance(auth_payload, dict) and auth_payload.get("chatgpt_account_id"):
            return str(auth_payload["chatgpt_account_id"])
        organizations = token_payload.get("organizations") or []
        if organizations and isinstance(organizations[0], dict):
            return organizations[0].get("id")
        return None

    def _chatgpt_authorize_url(self, redirect_uri: str, challenge: str, state: str) -> str:
        """构建 ChatGPT OAuth 授权链接。"""
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self._CHATGPT_CLIENT_ID,
                "redirect_uri": redirect_uri,
                "scope": "openid profile email offline_access",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "id_token_add_organizations": "true",
                "codex_cli_simplified_flow": "true",
                "state": state,
                "originator": "moviepilot",
            }
        )
        return f"{self._CHATGPT_ISSUER}/oauth/authorize?{query}"

    @staticmethod
    def _pkce_pair() -> tuple[str, str]:
        """生成 PKCE verifier 和 challenge。"""
        verifier = secrets.token_urlsafe(64).replace("=", "")
        digest = hashlib.sha256(verifier.encode("utf-8")).digest()
        challenge = base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")
        return verifier, challenge

    async def start_auth(
        self,
        provider_id: str,
        method_id: str,
        callback_url: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        启动 OAuth / device code 会话。

        API Key 方式已经由普通设置表单覆盖，这里只处理需要交互式授权的 provider。
        """
        provider = await self._get_provider_async(provider_id)
        method = next(
            (item for item in provider.oauth_methods if item.id == method_id),
            None,
        )
        if not method:
            raise LLMProviderAuthError(f"{provider.name} 不支持授权方式：{method_id}")

        session = PendingAuthSession(
            session_id=secrets.token_urlsafe(18),
            provider_id=provider_id,
            method_id=method_id,
            flow_type=method.type,
            expires_at=time.time() + 600,
        )

        if provider_id == "chatgpt" and method_id == "browser_oauth":
            if not callback_url:
                raise LLMProviderAuthError("ChatGPT 浏览器授权缺少回调地址")
            verifier, challenge = self._pkce_pair()
            state = secrets.token_urlsafe(24)
            session.authorize_url = self._chatgpt_authorize_url(
                redirect_uri=callback_url,
                challenge=challenge,
                state=state,
            )
            session.instructions = "请在浏览器中完成 ChatGPT Plus/Pro 登录授权。"
            session.context.update(
                {
                    "code_verifier": verifier,
                    "state": state,
                    "redirect_uri": callback_url,
                }
            )
            with self._lock:
                self._cleanup_auth_sessions_locked()
                self._pending_sessions[session.session_id] = session
                self._oauth_state_index[state] = session.session_id
            return {
                "session_id": session.session_id,
                "flow_type": "oauth_browser",
                "authorize_url": session.authorize_url,
                "instructions": session.instructions,
                "expires_at": session.expires_at,
            }

        if provider_id == "chatgpt" and method_id == "device_code":
            response = await self._build_async_request(
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": get_runtime_setting("USER_AGENT"),
                }
            ).post_res(
                f"{self._CHATGPT_ISSUER}/api/accounts/deviceauth/usercode",
                json={"client_id": self._CHATGPT_CLIENT_ID},
                raise_exception=True,
            )
            response.raise_for_status()
            payload = response.json()

            session.verification_url = f"{self._CHATGPT_ISSUER}/codex/device"
            session.user_code = payload.get("user_code")
            session.interval_seconds = max(int(payload.get("interval") or 5), 1)
            session.instructions = f"请在浏览器输入设备码：{session.user_code}"
            session.context.update(
                {
                    "device_auth_id": payload.get("device_auth_id"),
                    "user_code": payload.get("user_code"),
                }
            )
            with self._lock:
                self._cleanup_auth_sessions_locked()
                self._pending_sessions[session.session_id] = session
            return {
                "session_id": session.session_id,
                "flow_type": "device_code",
                "verification_url": session.verification_url,
                "user_code": session.user_code,
                "interval_seconds": session.interval_seconds,
                "instructions": session.instructions,
                "expires_at": session.expires_at,
            }

        if provider_id == "github-copilot" and method_id == "device_code":
            response = await self._build_async_request(
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "User-Agent": get_runtime_setting("USER_AGENT"),
                }
            ).post_res(
                "https://github.com/login/device/code",
                json={
                    "client_id": self._COPILOT_CLIENT_ID,
                    "scope": "read:user",
                },
                raise_exception=True,
            )
            response.raise_for_status()
            payload = response.json()

            session.verification_url = payload.get("verification_uri")
            session.user_code = payload.get("user_code")
            session.interval_seconds = max(int(payload.get("interval") or 5), 1)
            session.instructions = f"请在 GitHub 页面输入设备码：{session.user_code}"
            session.context.update(
                {
                    "device_code": payload.get("device_code"),
                }
            )
            with self._lock:
                self._cleanup_auth_sessions_locked()
                self._pending_sessions[session.session_id] = session
            return {
                "session_id": session.session_id,
                "flow_type": "device_code",
                "verification_url": session.verification_url,
                "user_code": session.user_code,
                "interval_seconds": session.interval_seconds,
                "instructions": session.instructions,
                "expires_at": session.expires_at,
            }

        raise LLMProviderAuthError(f"暂未实现 {provider.name} 的授权方式：{method.label}")

    async def handle_chatgpt_callback(
        self,
        provider_id: str,
        code: Optional[str],
        state: Optional[str],
        error: Optional[str],
        error_description: Optional[str],
    ) -> tuple[bool, str]:
        """处理 ChatGPT 浏览器 OAuth 回调。"""
        if provider_id != "chatgpt":
            return False, "当前 provider 不支持浏览器 OAuth 回调"

        if error:
            message = error_description or error
            with self._lock:
                self._cleanup_auth_sessions_locked()
                session_id = self._oauth_state_index.pop(state or "", None)
                if session_id and session_id in self._pending_sessions:
                    self._mark_session_error(self._pending_sessions[session_id], message)
            return False, message

        if not code or not state:
            return False, "缺少授权码或 state 参数"

        with self._lock:
            self._cleanup_auth_sessions_locked()
            session_id = self._oauth_state_index.pop(state, None)
            session = self._pending_sessions.get(session_id or "")

        if not session:
            return False, "授权会话不存在或已失效"

        if state != session.context.get("state"):
            self._mark_session_error(session, "state 校验失败")
            return False, "state 校验失败"

        try:
            payload = await self._exchange_chatgpt_code_for_tokens(
                code=code,
                redirect_uri=session.context["redirect_uri"],
                code_verifier=session.context["code_verifier"],
            )
            claims = self._jwt_claims(payload.get("id_token") or payload["access_token"])
            account_id = self._extract_chatgpt_account_id(claims)
            auth_data = {
                "type": "oauth",
                "provider": "chatgpt",
                "access_token": payload["access_token"],
                "refresh_token": payload["refresh_token"],
                "expires_at": int(time.time() + int(payload.get("expires_in") or 3600)),
                "account_id": account_id,
                "email": claims.get("email"),
                "label": claims.get("email") or account_id or "ChatGPT Plus/Pro",
            }
            await self._mark_session_success(session, auth_data)
            return True, "ChatGPT 授权成功"
        except Exception as err:
            message = f"ChatGPT 授权失败: {err}"
            self._mark_session_error(session, message)
            return False, message

    async def _exchange_chatgpt_code_for_tokens(
        self, code: str, redirect_uri: str, code_verifier: str
    ) -> dict[str, Any]:
        """使用 authorization code 交换 ChatGPT 令牌。"""
        response = await self._build_async_request(
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        ).post_res(
            f"{self._CHATGPT_ISSUER}/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": self._CHATGPT_CLIENT_ID,
                "code_verifier": code_verifier,
            },
            raise_exception=True,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise LLMProviderAuthError("ChatGPT 授权服务返回了无效响应")
        return payload

    async def _refresh_chatgpt_tokens(self, refresh_token: str) -> dict[str, Any]:
        """刷新 ChatGPT 的 access_token。"""
        response = await self._build_async_request(
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        ).post_res(
            f"{self._CHATGPT_ISSUER}/oauth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self._CHATGPT_CLIENT_ID,
            },
            raise_exception=True,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise LLMProviderAuthError("ChatGPT 刷新服务返回了无效响应")
        return payload

    async def _poll_chatgpt_device_auth(self, session: PendingAuthSession) -> None:
        """轮询 ChatGPT Device Auth 状态。"""
        response = await self._build_async_request(
            headers={
                "Content-Type": "application/json",
                "User-Agent": get_runtime_setting("USER_AGENT"),
            }
        ).post_res(
            f"{self._CHATGPT_ISSUER}/api/accounts/deviceauth/token",
            json={
                "device_auth_id": session.context["device_auth_id"],
                "user_code": session.context["user_code"],
            },
            raise_exception=True,
        )

        if response.status_code in {403, 404}:
            session.message = "等待用户在浏览器完成授权"
            return

        response.raise_for_status()
        payload = response.json()
        token_payload = await self._exchange_chatgpt_code_for_tokens(
            code=payload["authorization_code"],
            redirect_uri=f"{self._CHATGPT_ISSUER}/deviceauth/callback",
            code_verifier=payload["code_verifier"],
        )
        claims = self._jwt_claims(token_payload.get("id_token") or token_payload["access_token"])
        account_id = self._extract_chatgpt_account_id(claims)
        await self._mark_session_success(
            session,
            {
                "type": "oauth",
                "provider": "chatgpt",
                "access_token": token_payload["access_token"],
                "refresh_token": token_payload["refresh_token"],
                "expires_at": int(time.time() + int(token_payload.get("expires_in") or 3600)),
                "account_id": account_id,
                "email": claims.get("email"),
                "label": claims.get("email") or account_id or "ChatGPT Plus/Pro",
            },
        )

    async def _poll_copilot_device_auth(self, session: PendingAuthSession) -> None:
        """轮询 GitHub Copilot Device Auth 状态。"""
        response = await self._build_async_request(
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": get_runtime_setting("USER_AGENT"),
            }
        ).post_res(
            "https://github.com/login/oauth/access_token",
            json={
                "client_id": self._COPILOT_CLIENT_ID,
                "device_code": session.context["device_code"],
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
            raise_exception=True,
        )
        response.raise_for_status()
        payload = response.json()

        access_token = payload.get("access_token")
        if access_token:
            await self._mark_session_success(
                session,
                {
                    "type": "oauth",
                    "provider": "github-copilot",
                    "access_token": access_token,
                    # Copilot 设备码授权返回的是长期可复用 token，这里复用 access 字段即可。
                    "refresh_token": access_token,
                    "expires_at": None,
                    "label": "GitHub Copilot",
                },
            )
            return

        error = payload.get("error")
        if error == "authorization_pending":
            session.message = "等待用户在 GitHub 页面完成授权"
            return
        if error == "slow_down":
            session.interval_seconds = max(session.interval_seconds + 5, 10)
            session.message = "GitHub 要求降低轮询频率，稍后继续。"
            return
        if error:
            raise LLMProviderAuthError(f"GitHub Copilot 授权失败: {error}")

    async def _resolve_chatgpt_oauth(self) -> dict[str, Any]:
        """解析并返回 ChatGPT OAuth 鉴权，支持自动刷新 Token。"""
        auth = self.get_saved_auth("chatgpt")
        if not auth or auth.get("type") != "oauth":
            raise LLMProviderAuthError("尚未完成 ChatGPT Plus/Pro 授权")

        expires_at = auth.get("expires_at")
        refresh_token = auth.get("refresh_token")
        # 预留 60 秒刷新缓冲，避免刚发起请求就遇到过期。
        if expires_at and refresh_token and int(expires_at) <= int(time.time()) + 60:
            payload = await self._refresh_chatgpt_tokens(refresh_token)
            claims = self._jwt_claims(payload.get("id_token") or payload["access_token"])
            auth.update(
                {
                    "access_token": payload["access_token"],
                    "refresh_token": payload.get("refresh_token") or refresh_token,
                    "expires_at": int(time.time() + int(payload.get("expires_in") or 3600)),
                    "account_id": auth.get("account_id") or self._extract_chatgpt_account_id(claims),
                    "email": auth.get("email") or claims.get("email"),
                    "label": auth.get("label") or claims.get("email") or auth.get("account_id") or "ChatGPT Plus/Pro",
                }
            )
            await self.save_auth("chatgpt", auth)
        return auth
