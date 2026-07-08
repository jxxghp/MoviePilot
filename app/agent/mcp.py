"""Agent 外部 MCP 客户端与配置管理。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urljoin

from app.db.systemconfig_oper import SystemConfigOper
from app.log import logger
from app.schemas.agent import (
    AgentMcpServerConfig,
    AgentMcpServerTestResult,
    AgentMcpServerToolInfo,
)
from app.schemas.types import SystemConfigKey
from app.utils.http import AsyncRequestUtils

MCP_PROTOCOL_VERSION = "2025-11-25"
MCP_CLIENT_NAME = "MoviePilot Agent"
DEFAULT_MCP_TIMEOUT = 30
MCP_TOOL_NAME_PATTERN = re.compile(r"[^a-zA-Z0-9_]+")


@dataclass(frozen=True)
class AgentMcpToolSpec:
    """已发现的外部 MCP 工具定义。"""

    server: AgentMcpServerConfig
    name: str
    agent_tool_name: str
    description: str
    input_schema: dict[str, Any]


def _normalize_identifier(value: str, fallback: str = "mcp") -> str:
    """把服务器或工具名称转换为 Agent 工具可用的标识片段。"""
    normalized = MCP_TOOL_NAME_PATTERN.sub("_", str(value or "").strip())
    normalized = re.sub(r"_+", "_", normalized).strip("_").lower()
    if not normalized:
        normalized = fallback
    if normalized[0].isdigit():
        normalized = f"{fallback}_{normalized}"
    return normalized[:64]


def _normalize_timeout(value: Any) -> int:
    """规范化 MCP 连接和调用超时时间。"""
    try:
        timeout = int(value or DEFAULT_MCP_TIMEOUT)
    except (TypeError, ValueError):
        timeout = DEFAULT_MCP_TIMEOUT
    return min(max(timeout, 1), 600)


def _normalize_string_dict(value: Any) -> dict[str, str]:
    """规范化请求头和环境变量字典，移除空键。"""
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, str] = {}
    for key, item in value.items():
        normalized_key = str(key or "").strip()
        if not normalized_key:
            continue
        normalized[normalized_key] = str(item or "")
    return normalized


def _normalize_input_schema(value: Any) -> dict[str, Any]:
    """规范化 MCP 工具参数 Schema，保证至少是 object schema。"""
    if not isinstance(value, dict):
        return {"type": "object", "properties": {}, "required": []}
    schema = dict(value)
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    schema.setdefault("required", [])
    return schema


def _build_agent_tool_name(server: AgentMcpServerConfig, tool_name: str) -> str:
    """构造注入 Agent 的外部 MCP 工具名。"""
    prefix = server.tool_prefix or f"mcp_{server.name or server.id}"
    normalized_prefix = _normalize_identifier(prefix, fallback="mcp")
    normalized_tool_name = _normalize_identifier(tool_name, fallback="tool")
    if normalized_tool_name.startswith(f"{normalized_prefix}_"):
        return normalized_tool_name
    return f"{normalized_prefix}_{normalized_tool_name}"[:128]


def _jsonrpc_message(method: str, params: Optional[dict[str, Any]] = None, *, request_id: Optional[str] = None) -> dict:
    """构造 JSON-RPC 2.0 消息。"""
    payload = {"jsonrpc": "2.0", "method": method}
    if request_id is not None:
        payload["id"] = request_id
    if params is not None:
        payload["params"] = params
    return payload


def _raise_for_jsonrpc_error(payload: Any) -> None:
    """检查 JSON-RPC 响应错误并转换为运行时异常。"""
    if isinstance(payload, dict) and payload.get("error"):
        error = payload["error"]
        if isinstance(error, dict):
            message = error.get("message") or error
        else:
            message = error
        raise RuntimeError(f"MCP JSON-RPC 错误: {message}")


def _extract_jsonrpc_result(payload: Any, request_id: str) -> Any:
    """从 JSON-RPC 响应中提取 result 字段。"""
    if not isinstance(payload, dict):
        raise RuntimeError("MCP 响应不是有效 JSON 对象")
    if payload.get("id") != request_id:
        raise RuntimeError("MCP 响应 ID 与请求不匹配")
    _raise_for_jsonrpc_error(payload)
    return payload.get("result")


async def _iter_sse_events(response) -> Any:
    """按 SSE 事件格式迭代响应流。"""
    event_name = "message"
    data_lines: list[str] = []
    async for raw_line in response.aiter_lines():
        line = raw_line.rstrip("\r")
        if not line:
            if data_lines:
                yield {"event": event_name, "data": "\n".join(data_lines)}
            event_name = "message"
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            event_name = value or "message"
        elif field == "data":
            data_lines.append(value)
    if data_lines:
        yield {"event": event_name, "data": "\n".join(data_lines)}


def _parse_sse_text_response(text: str, request_id: str) -> Any:
    """从非流式 SSE 文本响应中提取匹配请求的 JSON-RPC 结果。"""
    event_name = "message"
    data_lines: list[str] = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.rstrip("\r")
        if not line:
            if data_lines:
                payload = _load_sse_json_payload(event_name, "\n".join(data_lines))
                if isinstance(payload, dict) and payload.get("id") == request_id:
                    return _extract_jsonrpc_result(payload, request_id)
            event_name = "message"
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            event_name = value or "message"
        elif field == "data":
            data_lines.append(value)
    if data_lines:
        payload = _load_sse_json_payload(event_name, "\n".join(data_lines))
        if isinstance(payload, dict) and payload.get("id") == request_id:
            return _extract_jsonrpc_result(payload, request_id)
    raise RuntimeError("MCP SSE 响应中未找到匹配请求")


def _load_sse_json_payload(event_name: str, data: str) -> Optional[dict]:
    """解析 SSE data 中的 JSON-RPC 消息。"""
    if event_name not in {"message", "messages"}:
        return None
    try:
        payload = json.loads(data)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


class _StdioMcpSession:
    """stdio MCP 会话，按一次操作生命周期启动外部进程。"""

    def __init__(self, server: AgentMcpServerConfig) -> None:
        self.server = server
        self.process: Optional[asyncio.subprocess.Process] = None
        self.stderr_task: Optional[asyncio.Task] = None

    async def __aenter__(self) -> "_StdioMcpSession":
        """启动 stdio MCP 子进程。"""
        if not self.server.command:
            raise RuntimeError("stdio MCP 服务器缺少启动命令")
        env = os.environ.copy()
        env.update(self.server.env or {})
        self.process = await asyncio.create_subprocess_exec(
            self.server.command,
            *(self.server.args or []),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        self.stderr_task = asyncio.create_task(self._drain_stderr())
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        """结束 stdio MCP 子进程。"""
        if self.stderr_task:
            self.stderr_task.cancel()
        if not self.process:
            return
        if self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=2)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()

    async def _drain_stderr(self) -> None:
        """持续读取子进程 stderr，避免缓冲区阻塞。"""
        if not self.process or not self.process.stderr:
            return
        try:
            while True:
                line = await self.process.stderr.readline()
                if not line:
                    break
                logger.debug(f"MCP stdio[{self.server.name}] stderr: {line.decode(errors='replace').strip()}")
        except asyncio.CancelledError:
            return

    async def notify(self, method: str, params: Optional[dict[str, Any]] = None) -> None:
        """发送不需要响应的 JSON-RPC 通知。"""
        await self._write_json(_jsonrpc_message(method, params))

    async def request(self, method: str, params: Optional[dict[str, Any]] = None) -> Any:
        """发送 JSON-RPC 请求并等待响应。"""
        request_id = uuid.uuid4().hex
        await self._write_json(_jsonrpc_message(method, params, request_id=request_id))
        while True:
            payload = await self._read_json()
            if payload.get("id") == request_id:
                return _extract_jsonrpc_result(payload, request_id)

    async def _write_json(self, payload: dict) -> None:
        """写入一行 JSON-RPC 消息。"""
        if not self.process or not self.process.stdin:
            raise RuntimeError("stdio MCP 进程未启动")
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        self.process.stdin.write(data.encode("utf-8"))
        await self.process.stdin.drain()

    async def _read_json(self) -> dict:
        """从 stdout 读取一行 JSON-RPC 消息。"""
        if not self.process or not self.process.stdout:
            raise RuntimeError("stdio MCP 进程未启动")
        timeout = _normalize_timeout(self.server.timeout)
        while True:
            line = await asyncio.wait_for(self.process.stdout.readline(), timeout=timeout)
            if not line:
                raise RuntimeError("stdio MCP 进程已退出")
            try:
                payload = json.loads(line.decode("utf-8"))
            except ValueError:
                logger.debug(f"忽略非 JSON MCP stdout 行: {line!r}")
                continue
            if isinstance(payload, dict):
                return payload


class _HttpMcpSession:
    """Streamable HTTP MCP 会话。"""

    def __init__(self, server: AgentMcpServerConfig) -> None:
        self.server = server
        self.session_id: Optional[str] = None

    async def __aenter__(self) -> "_HttpMcpSession":
        """进入 HTTP MCP 会话。"""
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        """退出 HTTP MCP 会话。"""
        return None

    async def notify(self, method: str, params: Optional[dict[str, Any]] = None) -> None:
        """发送不需要响应的 JSON-RPC 通知。"""
        await self._post(_jsonrpc_message(method, params), expect_response=False)

    async def request(self, method: str, params: Optional[dict[str, Any]] = None) -> Any:
        """发送 JSON-RPC 请求并等待响应。"""
        request_id = uuid.uuid4().hex
        return await self._post(
            _jsonrpc_message(method, params, request_id=request_id),
            expect_response=True,
            request_id=request_id,
        )

    async def _post(
        self,
        payload: dict,
        *,
        expect_response: bool,
        request_id: Optional[str] = None,
    ) -> Any:
        """向 Streamable HTTP MCP 服务发送一条 JSON-RPC 消息。"""
        if not self.server.url:
            raise RuntimeError("HTTP MCP 服务器缺少 URL")
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            **(self.server.headers or {}),
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        response = await AsyncRequestUtils(
            headers=headers,
            timeout=_normalize_timeout(self.server.timeout),
            content_type="application/json",
            accept_type="application/json, text/event-stream",
            http2=False,
        ).post_res(self.server.url, json=payload, raise_exception=True)
        try:
            if not response:
                raise RuntimeError("HTTP MCP 请求无响应")
            response.raise_for_status()
            session_id = response.headers.get("Mcp-Session-Id")
            if session_id:
                self.session_id = session_id
            if not expect_response:
                return None
            content_type = response.headers.get("content-type", "").lower()
            if "text/event-stream" in content_type:
                return _parse_sse_text_response(response.text, request_id or "")
            data = response.json()
            return _extract_jsonrpc_result(data, request_id or "")
        finally:
            if response is not None:
                await response.aclose()


class _SseMcpSession:
    """旧版 HTTP+SSE MCP 会话。"""

    def __init__(self, server: AgentMcpServerConfig) -> None:
        self.server = server
        self.response = None
        self.endpoint: Optional[str] = None
        self._stream_manager = None
        self._event_iterator = None

    async def __aenter__(self) -> "_SseMcpSession":
        """打开 SSE 流并读取服务端回传的 POST endpoint。"""
        if not self.server.url:
            raise RuntimeError("SSE MCP 服务器缺少 URL")
        self._stream_manager = AsyncRequestUtils(
            headers={"Accept": "text/event-stream", **(self.server.headers or {})},
            timeout=_normalize_timeout(self.server.timeout),
            accept_type="text/event-stream",
            http2=False,
        ).get_stream(self.server.url, raise_exception=True)
        self.response = await self._stream_manager.__aenter__()
        if not self.response:
            raise RuntimeError("SSE MCP 连接无响应")
        self.response.raise_for_status()
        self._event_iterator = _iter_sse_events(self.response).__aiter__()
        self.endpoint = await self._read_endpoint()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        """关闭 SSE 流。"""
        if self._stream_manager:
            await self._stream_manager.__aexit__(exc_type, exc, tb)

    async def notify(self, method: str, params: Optional[dict[str, Any]] = None) -> None:
        """发送不需要响应的 JSON-RPC 通知。"""
        await self._post(_jsonrpc_message(method, params))

    async def request(self, method: str, params: Optional[dict[str, Any]] = None) -> Any:
        """发送 JSON-RPC 请求并等待 SSE 流上的响应。"""
        request_id = uuid.uuid4().hex
        await self._post(_jsonrpc_message(method, params, request_id=request_id))
        timeout = _normalize_timeout(self.server.timeout)
        while True:
            event = await asyncio.wait_for(self._event_iterator.__anext__(), timeout=timeout)
            payload = _load_sse_json_payload(event.get("event", ""), event.get("data", ""))
            if isinstance(payload, dict) and payload.get("id") == request_id:
                return _extract_jsonrpc_result(payload, request_id)

    async def _read_endpoint(self) -> str:
        """读取 SSE endpoint 事件中的 POST 地址。"""
        timeout = _normalize_timeout(self.server.timeout)
        while True:
            event = await asyncio.wait_for(self._event_iterator.__anext__(), timeout=timeout)
            if event.get("event") != "endpoint":
                continue
            endpoint = str(event.get("data") or "").strip()
            if not endpoint:
                continue
            return urljoin(self.server.url, endpoint)

    async def _post(self, payload: dict) -> None:
        """向 SSE 握手返回的 endpoint 发送 JSON-RPC 消息。"""
        if not self.endpoint:
            raise RuntimeError("SSE MCP endpoint 未初始化")
        response = await AsyncRequestUtils(
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                **(self.server.headers or {}),
            },
            timeout=_normalize_timeout(self.server.timeout),
            content_type="application/json",
            accept_type="application/json",
            http2=False,
        ).post_res(self.endpoint, json=payload, raise_exception=True)
        try:
            if not response:
                raise RuntimeError("SSE MCP POST 请求无响应")
            response.raise_for_status()
        finally:
            if response is not None:
                await response.aclose()


async def _open_mcp_session(server: AgentMcpServerConfig):
    """根据配置创建对应的 MCP 传输会话。"""
    transport = "http" if server.transport == "streamable_http" else server.transport
    if transport == "stdio":
        return _StdioMcpSession(server)
    if transport == "sse":
        return _SseMcpSession(server)
    if transport == "http":
        return _HttpMcpSession(server)
    raise RuntimeError(f"不支持的 MCP 传输协议: {server.transport}")


class AgentMcpManager:
    """管理 Agent 外部 MCP 服务器配置、工具发现和工具调用。"""

    def get_servers(self) -> list[AgentMcpServerConfig]:
        """读取已保存的外部 MCP 服务器配置。"""
        raw_servers = SystemConfigOper().get(SystemConfigKey.AIAgentMcpServers) or []
        if not isinstance(raw_servers, list):
            return []
        servers: list[AgentMcpServerConfig] = []
        for raw_server in raw_servers:
            try:
                servers.append(self.normalize_server(raw_server))
            except Exception as err:
                logger.warning(f"忽略无效的 Agent MCP 配置: {err}")
        return servers

    async def save_servers(self, servers: list[AgentMcpServerConfig]) -> bool:
        """保存外部 MCP 服务器配置。"""
        normalized_servers = [self.normalize_server(server).model_dump() for server in servers]
        return await SystemConfigOper().async_set(
            SystemConfigKey.AIAgentMcpServers,
            normalized_servers or None,
        )

    def normalize_server(self, value: Any) -> AgentMcpServerConfig:
        """规范化单个 MCP 服务器配置。"""
        if isinstance(value, AgentMcpServerConfig):
            raw_server = value.model_dump()
        elif isinstance(value, dict):
            raw_server = dict(value)
        else:
            raise ValueError("MCP 服务器配置必须是对象")

        raw_server["id"] = str(raw_server.get("id") or uuid.uuid4().hex[:12]).strip()
        raw_server["name"] = str(raw_server.get("name") or raw_server["id"]).strip()
        raw_server["transport"] = str(raw_server.get("transport") or "stdio").strip()
        raw_server["description"] = str(raw_server.get("description") or "").strip() or None
        raw_server["command"] = str(raw_server.get("command") or "").strip() or None
        raw_server["args"] = [str(item) for item in raw_server.get("args") or []]
        raw_server["env"] = _normalize_string_dict(raw_server.get("env"))
        raw_server["url"] = str(raw_server.get("url") or "").strip() or None
        raw_server["headers"] = _normalize_string_dict(raw_server.get("headers"))
        raw_server["timeout"] = _normalize_timeout(raw_server.get("timeout"))
        raw_server["tool_prefix"] = str(raw_server.get("tool_prefix") or "").strip() or None
        raw_server["require_admin"] = bool(raw_server.get("require_admin", True))
        return AgentMcpServerConfig.model_validate(raw_server)

    def config_signature(self) -> str:
        """生成外部 MCP 配置签名，用于 Agent 图缓存失效。"""
        payload = [server.model_dump() for server in self.get_servers()]
        raw_text = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(raw_text.encode("utf-8")).hexdigest()

    async def initialize_session(self, session) -> None:
        """完成 MCP initialize 和 initialized 通知流程。"""
        await session.request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {
                    "name": MCP_CLIENT_NAME,
                    "version": "1.0.0",
                },
            },
        )
        await session.notify("notifications/initialized")

    async def list_server_tools(self, server: AgentMcpServerConfig) -> list[AgentMcpToolSpec]:
        """连接单个 MCP 服务器并读取工具列表。"""
        normalized_server = self.normalize_server(server)
        session_manager = await _open_mcp_session(normalized_server)
        async with session_manager as session:
            await self.initialize_session(session)
            result = await session.request("tools/list")
        tools_payload = result.get("tools", []) if isinstance(result, dict) else []
        tool_specs: list[AgentMcpToolSpec] = []
        for item in tools_payload:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            tool_name = str(item["name"])
            tool_specs.append(
                AgentMcpToolSpec(
                    server=normalized_server,
                    name=tool_name,
                    agent_tool_name=_build_agent_tool_name(normalized_server, tool_name),
                    description=str(item.get("description") or ""),
                    input_schema=_normalize_input_schema(item.get("inputSchema")),
                )
            )
        return tool_specs

    async def list_enabled_tool_specs(self) -> list[AgentMcpToolSpec]:
        """读取所有启用 MCP 服务器暴露的工具定义。"""
        tool_specs: list[AgentMcpToolSpec] = []
        seen_names: set[str] = set()
        for server in self.get_servers():
            if not server.enabled:
                continue
            try:
                for spec in await self.list_server_tools(server):
                    if spec.agent_tool_name in seen_names:
                        logger.warning(f"跳过重复的 MCP Agent 工具名: {spec.agent_tool_name}")
                        continue
                    tool_specs.append(spec)
                    seen_names.add(spec.agent_tool_name)
            except Exception as err:
                logger.warning(f"读取 MCP 服务器 {server.name} 工具失败: {err}")
        return tool_specs

    async def call_server_tool(
        self,
        server: AgentMcpServerConfig,
        tool_name: str,
        arguments: Optional[dict[str, Any]] = None,
    ) -> Any:
        """调用单个 MCP 服务器上的指定工具。"""
        normalized_server = self.normalize_server(server)
        session_manager = await _open_mcp_session(normalized_server)
        async with session_manager as session:
            await self.initialize_session(session)
            return await session.request(
                "tools/call",
                {
                    "name": tool_name,
                    "arguments": arguments or {},
                },
            )

    async def test_server(self, server: AgentMcpServerConfig) -> AgentMcpServerTestResult:
        """测试 MCP 服务器连接并返回工具列表。"""
        tool_specs = await self.list_server_tools(server)
        tools = [
            AgentMcpServerToolInfo(
                name=spec.name,
                agent_tool_name=spec.agent_tool_name,
                description=spec.description,
                input_schema=spec.input_schema,
            )
            for spec in tool_specs
        ]
        return AgentMcpServerTestResult(
            success=True,
            message=f"连接成功，发现 {len(tools)} 个工具",
            tools=tools,
            tool_count=len(tools),
        )


agent_mcp_manager = AgentMcpManager()
