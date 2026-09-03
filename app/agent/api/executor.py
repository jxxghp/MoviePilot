"""通过固定宿主 API 路由执行 Agent 业务操作。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import quote

from app.adapters.network.http import AsyncRequestUtils
from app.agent.policy.api import ApiOperationRoute, resolve_api_route
from app.application.security.token import create_access_token
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting


class ApiExecutionError(RuntimeError):
    """固定 API 路由无法构造或请求失败。"""


@dataclass(frozen=True)
class ApiExecutionContext:
    """API 请求使用的 Agent 身份和会话上下文。"""

    user_id: str
    username: str | None
    is_admin: bool
    session_id: str | None = None
    channel: str | None = None
    source: str | None = None


class MoviePilotApiExecutor:
    """把 operation ID 转换为受控的 MoviePilot REST API 请求。"""

    _ALLOWED_SOURCES = frozenset({"tmdb", "douban", "bangumi", "anilist"})
    _ALLOWED_DOWNLOAD_ACTIONS = frozenset({"start", "stop"})
    _COLLECTION_HEADERS = {
        "x-result-count": "result_count",
        "x-total-count": "total_count",
        "x-page": "page",
        "x-page-size": "count",
    }

    def __init__(
        self,
        *,
        context: ApiExecutionContext,
        request_factory: type[AsyncRequestUtils] = AsyncRequestUtils,
    ) -> None:
        """绑定调用身份和 HTTP 传输工厂。"""
        self._context = context
        self._request_factory = request_factory

    @staticmethod
    def _resolve_base_url() -> str:
        """解析本机 API 基址，避免把任意用户输入当成请求目标。"""
        configured_domain = str(get_runtime_setting("APP_DOMAIN", "") or "").strip()
        if configured_domain.startswith(("http://", "https://")):
            return configured_domain.rstrip("/")
        host = str(get_runtime_setting("HOST", "127.0.0.1") or "127.0.0.1")
        if host in {"0.0.0.0", "::", "[::]"}:
            host = "127.0.0.1"
        port = int(get_runtime_setting("PORT", 3001))
        return f"http://{host}:{port}"

    @classmethod
    def _render_path(
        cls,
        route: ApiOperationRoute,
        path_params: Mapping[str, Any],
    ) -> str:
        """替换固定路由占位符并校验具有枚举约束的路径参数。"""
        rendered = route.path
        required = []
        cursor = 0
        while True:
            start = rendered.find("{", cursor)
            if start < 0:
                break
            end = rendered.find("}", start)
            if end < 0:
                raise ApiExecutionError("API 路由占位符格式无效")
            required.append(rendered[start + 1 : end])
            cursor = end + 1
        for name in required:
            value = path_params.get(name)
            if value in (None, ""):
                raise ApiExecutionError(f"缺少 API 路径参数: {name}")
            if name == "source" and str(value).lower() not in cls._ALLOWED_SOURCES:
                raise ApiExecutionError("媒体来源不在 API 白名单内")
            if name == "action" and str(value).lower() not in cls._ALLOWED_DOWNLOAD_ACTIONS:
                raise ApiExecutionError("下载动作不在 API 白名单内")
            rendered = rendered.replace("{" + name + "}", quote(str(value), safe=""))
        return rendered

    def _build_headers(self) -> dict[str, str]:
        """构造宿主令牌和请求上下文头，并确保头值符合 ASCII 约束。"""
        user_id = str(self._context.user_id or "")
        if not user_id.isdigit():
            raise ApiExecutionError("当前 Agent 身份没有可用于 API 鉴权的用户 ID")
        token = create_access_token(
            userid=int(user_id),
            username=self._context.username or user_id,
            super_user=self._context.is_admin,
        )
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        if self._context.session_id:
            headers["X-MoviePilot-Agent-Session"] = self._context.session_id
        if self._context.channel:
            # 渠道值来自通知渠道枚举，也可能来自插件扩展；统一 URL 编码可兼容两者。
            headers["X-MoviePilot-Agent-Channel"] = quote(self._context.channel, safe="")
        if self._context.source:
            headers["X-MoviePilot-Agent-Source"] = quote(self._context.source, safe="")
        return headers

    @classmethod
    def _attach_collection_metadata(
        cls,
        payload: Any,
        headers: Mapping[str, Any],
    ) -> Any:
        """把 REST 数量响应头投影为 Agent 可直接读取的附加集合元数据。"""
        if not isinstance(payload, Mapping):
            return payload
        normalized_headers = {
            str(name).lower(): value
            for name, value in headers.items()
        }
        collection = {}
        for header_name, field_name in cls._COLLECTION_HEADERS.items():
            raw_value = normalized_headers.get(header_name)
            if raw_value is None:
                continue
            try:
                collection[field_name] = int(raw_value)
            except (TypeError, ValueError):
                continue
        if not collection:
            return payload
        # 集合数据可能触发通用工具结果截断；把元数据放在 data 前面，确保预览仍保留精确总数。
        result = {"collection": collection}
        for key, value in payload.items():
            if key != "collection":
                result[key] = value
        return result

    async def execute(
        self,
        operation_id: str,
        *,
        path_params: Mapping[str, Any] | None = None,
        query: Mapping[str, Any] | None = None,
        body: Any = None,
    ) -> str:
        """执行白名单 operation，并把响应转换为稳定 JSON 文本。"""
        route = resolve_api_route(operation_id)
        if route is None:
            raise ApiExecutionError(f"未注册 API 路由: {operation_id}")
        path = self._render_path(route, path_params or {})
        url = f"{self._resolve_base_url()}{path}"
        query_data = dict(query or {})
        body_data = dict(body) if isinstance(body, Mapping) else body
        if route.method == "GET" and body_data is not None:
            if not isinstance(body_data, Mapping):
                raise ApiExecutionError("GET operation 的 body 必须是 JSON 对象")
            query_data.update(body_data)
            body_data = None
        request = self._request_factory(
            headers=self._build_headers(),
            timeout=30,
            verify=False,
            trust_env=False,
        )
        try:
            response = await request.request(
                method=route.method,
                url=url,
                params=query_data or None,
                json=body_data,
                raise_exception=True,
            )
            if response is None:
                raise ApiExecutionError("MoviePilot API 没有返回响应")
            try:
                payload = response.json()
                status_code = response.status_code
                response_headers = dict(response.headers)
            finally:
                await response.aclose()
        except ApiExecutionError:
            raise
        except Exception as error:
            logger.warning(f"Agent API 请求失败: operation={operation_id} error={error}")
            raise ApiExecutionError(f"MoviePilot API 请求失败: {operation_id}") from error
        if status_code >= 400:
            return json.dumps(
                {"success": False, "error": "api_error", "status_code": status_code, "data": payload},
                ensure_ascii=False,
            )
        payload = self._attach_collection_metadata(payload, response_headers)
        return json.dumps(payload, ensure_ascii=False, default=str)


__all__ = ["ApiExecutionContext", "ApiExecutionError", "MoviePilotApiExecutor"]
