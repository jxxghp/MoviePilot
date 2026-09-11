"""给原生客户端提供有调用上限的模型连接，凭据和业务答案留在控制器。"""

import asyncio
import copy
import hashlib
import json
import re
import secrets
import socket
from collections.abc import AsyncGenerator, Iterator
from contextlib import contextmanager, suppress
from typing import Any, Optional

import anyio
import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from scripts.evaluation.models import ModelSettings

MAX_REQUEST_BYTES = 4 * 1024 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_EVENT_BYTES = 1024 * 1024
TOKEN_KEYS = ("input_tokens", "output_tokens", "total_tokens")
NATIVE_CONTROL_TOOLS = frozenset({
    "update_plan", "spawn_agent", "send_input", "resume_agent", "close_agent", "wait", "list_agents",
    "request_user_input", "search_tools", "tool_search", "search_tool_bm25",
})
NATIVE_COLLABORATION_TOOLS = frozenset({
    "spawn_agent", "followup_task", "interrupt_agent", "list_agents", "send_message", "wait_agent",
})
NATIVE_MULTI_AGENT_TOOLS = NATIVE_CONTROL_TOOLS | NATIVE_COLLABORATION_TOOLS
KNOWN_NAMESPACES = frozenset({"functions", "clock", "collaboration", "mcp__evaluation", "multi_agent_v1"})
FIXTURE_TOOLS = frozenset({"moviepilot_api", "read_skill", "read_tool_result"})


def allowed_tool(name: str) -> bool:
    """只允许原生计划/协作控制与本地固定 MCP 目录，不接入真实系统工具。"""
    normalized = name.removeprefix("functions.").replace(".", "__")
    return (normalized in NATIVE_CONTROL_TOOLS
            or normalized in {f"collaboration__{tool}" for tool in NATIVE_COLLABORATION_TOOLS}
            or normalized in {f"mcp__evaluation__{tool}" for tool in FIXTURE_TOOLS}
            or (normalized.startswith("multi_agent_v1__")
                and normalized.removeprefix("multi_agent_v1__") in NATIVE_MULTI_AGENT_TOOLS))


def project_tools(tools: Any, prefix: str = "") -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """保留允许工具的原始 schema；外部 MCP 配置污染必须中止，不能静默投影成合格评测。"""
    if not isinstance(tools, list) or prefix.count(".") > 8:
        raise ValueError("工具目录结构无效")
    projected, retained, removed = [], [], []
    for tool in tools:
        if not isinstance(tool, dict) or not isinstance(tool.get("type"), str):
            raise ValueError("工具定义无效")
        if tool["type"] in {"function", "custom", "namespace"} and "name" not in tool:
            raise ValueError("工具缺少名称")
        if "parameters" in tool and not isinstance(tool["parameters"], dict):
            raise ValueError("工具参数 schema 无效")
        name = tool.get("name", tool["type"])
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,255}", name):
            raise ValueError("工具名称无效")
        qualified = f"{prefix}.{name}" if prefix else name
        normalized = qualified.removeprefix("functions.").replace(".", "__")
        if tool["type"] == "mcp" or (normalized.startswith("mcp__") and not (
            normalized == "mcp__evaluation" or normalized.startswith("mcp__evaluation__")
        )):
            raise ValueError(f"评测目录包含外部 MCP：{qualified}")
        if tool["type"] == "namespace":
            if qualified not in KNOWN_NAMESPACES:
                raise ValueError(f"评测目录包含未知命名空间：{qualified}")
            if "tools" not in tool:
                raise ValueError("命名空间没有工具目录")
            children, kept, dropped = project_tools(tool["tools"], qualified)
            if children:
                projected.append({**tool, "tools": children})
            retained.extend(kept)
            removed.extend(dropped)
        elif tool["type"] == "tool_search":
            description = tool.get("description")
            sources = re.findall(r"(?m)^- ([^:\n]+):", description) if isinstance(description, str) else []
            allowed_sources = {"Multi-agent tools", "evaluation"}
            if (prefix or tool.get("execution") != "client" or not sources
                    or "evaluation" not in sources or any(source not in allowed_sources for source in sources)):
                raise ValueError("原生工具搜索不是固定评测目录")
            projected.append(copy.deepcopy(tool))
            retained.append(qualified)
        elif allowed_tool(qualified) and tool["type"] in {"function", "custom"}:
            projected.append(copy.deepcopy(tool))
            retained.append(qualified)
        else:
            removed.append(qualified)
    return projected, retained, removed


def _project_catalogs(payload: dict[str, Any]) -> tuple[list[str], list[str], list[dict[str, Any]]]:
    """同时约束传统顶层目录和原生 additional_tools 输入目录，保持各自 wire 位置。"""
    retained, removed, catalogs = [], [], []

    def project(value: dict[str, Any], location: str) -> None:
        """替换已存在的目录并记录位置，不能凭空创建第二份工具定义。"""
        projected, kept, dropped = project_tools(value["tools"])
        value["tools"] = projected
        retained.extend(kept)
        removed.extend(dropped)
        catalogs.append({"location": location, "retained_tools": kept, "removed_tools": dropped})

    def visit(value: Any, location: str) -> None:
        """递归检查输入及子代理历史，未知额外目录封装必须拒绝。"""
        if isinstance(value, dict):
            kind = value.get("type")
            if isinstance(kind, str) and kind.startswith("additional_tools"):
                if (kind != "additional_tools" or value.get("role") != "developer" or "tools" not in value
                        or set(value) - {"type", "id", "role", "tools"}):
                    raise ValueError("原生额外工具目录结构无效")
                project(value, f"{location}.tools")
                return
            if kind == "tool_search_output":
                _validate_search_output(value)
                project(value, f"{location}.tools")
                return
            for key, child in value.items():
                visit(child, f"{location}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{location}[{index}]")

    if "tools" in payload:
        project(payload, "tools")
    visit(payload.get("input", []), "input")
    return retained, removed, catalogs


def project_host_instructions(payload: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """只去除原生独立 AGENTS 用户说明块，保留系统提示及同消息内的业务文本。"""
    removed: list[dict[str, Any]] = []
    envelope = re.compile(r"\A# AGENTS\.md instructions(?: for [^\r\n]+)?\n\n<INSTRUCTIONS>\n[\s\S]*\n</INSTRUCTIONS>\Z")
    marker = "# AGENTS.md instructions"
    omitted = object()

    def visit(value: Any, *, top_level: bool = False) -> Any:
        """递归处理子代理历史，未知封装不能靠关键词删除后继续发送。"""
        if isinstance(value, dict):
            result = {}
            removed_content = False
            for key, child in value.items():
                if top_level and key == "instructions":
                    result[key] = copy.deepcopy(child)
                elif key == "content" and value.get("role") == "user" and isinstance(child, list):
                    blocks = []
                    for block in child:
                        content = block.get("text") if isinstance(block, dict) else None
                        kind = block.get("kind") if isinstance(block, dict) else None
                        if isinstance(content, str) and (marker in content or kind == "agents_md.instructions"):
                            if (block.get("type") not in {"input_text", "text"}
                                    or kind not in {None, "agents_md.instructions"} or not envelope.fullmatch(content)):
                                raise ValueError("无法确认宿主说明块边界")
                            encoded = content.encode("utf-8")
                            removed.append({"sha256": hashlib.sha256(encoded).hexdigest(), "bytes": len(encoded)})
                            removed_content = True
                        else:
                            blocks.append(visit(block))
                    result[key] = blocks
                else:
                    result[key] = visit(child)
            return omitted if removed_content and not result.get("content") else result
        if isinstance(value, list):
            values = [visit(child) for child in value]
            return [child for child in values if child is not omitted]
        if isinstance(value, str) and any(boundary in value for boundary in (
            marker, "<skills_instructions>", "</skills_instructions>", "<INSTRUCTIONS>", "</INSTRUCTIONS>",
        )):
            raise ValueError("宿主说明出现在未知位置")
        return value

    return visit(payload, top_level=True), removed


def _input_metadata(payload: Any) -> list[dict[str, Any]]:
    """仅审计主输入文本块的角色、类型和指纹，不保存宿主偏好或用户任务正文。"""
    if not isinstance(payload, dict):
        return []
    result = []
    for item in payload.get("input", []) if isinstance(payload.get("input"), list) else []:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
        for block in blocks:
            text = block.get("text") if isinstance(block, dict) else None
            if isinstance(text, str):
                encoded = text.encode("utf-8")
                result.append({"role": role if isinstance(role, str) and role in {"user", "assistant", "developer", "system", "tool"} else None,
                               "type": block.get("type") if isinstance(block.get("type"), str) and block["type"] in {"text", "input_text", "output_text"} else None,
                               "bytes": len(encoded), "sha256": hashlib.sha256(encoded).hexdigest()})
    return result


def _strings(payload: Any) -> Iterator[str]:
    """遍历解析后的字段和值，转义后的凭据也不能从响应或审计元数据泄露。"""
    if isinstance(payload, str):
        yield payload
    elif isinstance(payload, dict):
        for key, value in payload.items():
            yield from _strings(key)
            yield from _strings(value)
    elif isinstance(payload, list):
        for value in payload:
            yield from _strings(value)


def _validate_search_call(payload: dict[str, Any], *, partial: bool = False) -> None:
    """只接受原生客户端的查询参数，服务端搜索或自选来源不能进入受控运行。"""
    if payload.get("execution") != "client" or not isinstance(payload.get("call_id"), str) or not payload["call_id"]:
        raise ValueError("原生工具搜索调用边界无效")
    arguments = payload.get("arguments")
    if partial and arguments in (None, "", {}):
        return
    if not isinstance(arguments, dict) or set(arguments) - {"query", "limit"}:
        raise ValueError("原生工具搜索参数无效")
    if not isinstance(arguments.get("query"), str) or not arguments["query"].strip():
        raise ValueError("原生工具搜索缺少查询")
    limit = arguments.get("limit", 8)
    if limit is not None and (type(limit) is not int or limit <= 0):
        raise ValueError("原生工具搜索数量无效")


def _validate_search_output(payload: dict[str, Any]) -> None:
    """延迟发现返回的定义只能属于三个评测工具，不能借搜索结果再引入原生危险工具。"""
    if (payload.get("execution") != "client" or payload.get("status") != "completed"
            or not isinstance(payload.get("call_id"), str) or not payload["call_id"]):
        raise ValueError("原生工具搜索结果边界无效")
    _, retained, removed = project_tools(payload.get("tools"))
    if removed or any(not allowed_tool(name) for name in retained):
        names = ", ".join(removed[:8]) or ", ".join(retained[:8])
        raise ValueError(f"搜索结果包含评测目录以外的定义：{names}")


def _reject_unsafe_calls(payload: Any, *, partial: bool = False) -> None:
    """校验实际调用；猜出被隐藏的函数名或内置调用类型也不能交给原生执行器。"""
    if isinstance(payload, dict):
        kind = payload.get("type")
        if kind == "tool_search_call":
            _validate_search_call(payload, partial=partial)
        elif kind == "tool_search_output":
            _validate_search_output(payload)
        elif isinstance(kind, str) and kind.endswith("_call"):
            name = payload.get("name")
            namespace = payload.get("namespace")
            if namespace:
                name = f"{namespace}.{name}"
            if kind not in {"function_call", "custom_tool_call"} or not isinstance(name, str) or not allowed_tool(name):
                raise ValueError("模型返回了受控目录以外的工具调用")
        for value in payload.values():
            _reject_unsafe_calls(value, partial=partial or kind == "response.output_item.added")
    elif isinstance(payload, list):
        for value in payload:
            _reject_unsafe_calls(value, partial=partial)


class _LocalServer(uvicorn.Server):
    """回环服务的取消由评测控制器统一管理，不占用宿主进程的信号处理器。"""

    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        """控制器并行管理 MCP 和模型连接，服务不得互相覆盖全局信号处理。"""
        yield


class _ModelStreamingResponse(StreamingResponse):
    """客户端断开时显式等待生成器收尾，确保连接和审计结果不依赖垃圾回收。"""

    def __init__(self, body: AsyncGenerator[str, None]) -> None:
        """保留本次响应独占的生成器，后续关闭不会影响其他模型请求。"""
        super().__init__(body, media_type="text/event-stream")
        self._body = body

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """流式发送被取消也要完成局部清理，之后才让控制器读取终态。"""
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await self._body.aclose()


class EvaluationModelProxy:
    """原生 Codex 使用本地随机令牌，真正供应商凭据只由此固定目标代理持有。"""

    def __init__(self, settings: ModelSettings, *, probe_only: bool = False,
                 transport: Optional[httpx.AsyncBaseTransport] = None) -> None:
        """创建独立计数和目录记录；probe_only 只记录目录，绝不真正出站。"""
        self.settings = settings
        self.probe_only = probe_only
        self.bearer_token = secrets.token_urlsafe(32)
        self.endpoint = ""
        self.requests: list[dict[str, Any]] = []
        self.blocked_calls = 0
        self._rejected_requests: list[dict[str, Any]] = []
        self._client = httpx.AsyncClient(transport=transport, trust_env=False, timeout=min(120, settings.timeout_seconds))
        self._socket: Optional[socket.socket] = None
        self._server: Optional[uvicorn.Server] = None
        self._task: Optional[asyncio.Task[Any]] = None
        self._closed = False
        self._server_error: Optional[str] = None
        self.app = Starlette(routes=[Route("/v1/responses", self._respond, methods=["POST"])])

    async def __aenter__(self) -> "EvaluationModelProxy":
        """显式启动本地 HTTP 服务，不访问真实模型。"""
        await self.start()
        return self

    async def __aexit__(self, *_args: Any) -> None:
        """退出时回收服务和连接，不把运行任务交给解释器退出。"""
        await self.close()

    async def start(self) -> None:
        """绑定随机回环端口；启动失败或取消也必须关闭连接、监听和子任务。"""
        if self._task is not None or self._closed:
            raise RuntimeError("模型代理已经启动或关闭")
        try:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._socket.bind(("127.0.0.1", 0))
            self.endpoint = f"http://127.0.0.1:{self._socket.getsockname()[1]}/v1"
            self._server = _LocalServer(uvicorn.Config(self.app, log_config=None, access_log=False, log_level="error"))
            self._task = asyncio.create_task(self._server.serve(sockets=[self._socket]))
            async with asyncio.timeout(10):
                while not self._server.started:
                    if self._task.done():
                        await self._task
                        raise RuntimeError("模型代理未能启动")
                    await asyncio.sleep(0.01)
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        """有界等待退出，幂等回收；子任务异常也不能跳过后续资源关闭。"""
        if self._server is not None:
            self._server.should_exit = True
        try:
            if self._task is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(self._task), 10)
                except asyncio.TimeoutError:
                    self._task.cancel()
                except Exception as error:
                    self._server_error = type(error).__name__
                finally:
                    if not self._task.done():
                        self._task.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await self._task
        finally:
            self._closed = True
            try:
                await self._client.aclose()
            finally:
                if self._socket is not None:
                    self._socket.close()

    @staticmethod
    def _error(message: str, status: int = 400) -> JSONResponse:
        """只返回固定控制错误，不回显原始供应商响应、密钥或提示词。"""
        return JSONResponse({"error": {"message": message, "type": "evaluation_error"}}, status_code=status)

    def _validate_payload(self, payload: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        """请求边界同时约束模型、预算和目录，子模型不能静默替换对照条件。"""
        if not isinstance(payload, dict) or payload.get("model") != self.settings.model:
            raise ValueError("模型与评测配置不符")
        if self.settings.api_key in "".join(_strings(payload)):
            raise ValueError("请求包含私有凭据")
        payload, host_instructions = project_host_instructions(payload)
        reasoning = payload.get("reasoning")
        if reasoning is None:
            reasoning = {}
        if not isinstance(reasoning, dict) or reasoning.get("effort") not in {None, self.settings.reasoning_effort}:
            raise ValueError("推理预算与评测配置不符")
        tokens = payload.get("max_output_tokens", self.settings.max_output_tokens)
        if type(tokens) is not int or tokens <= 0 or type(payload.get("stream", False)) is not bool:
            raise ValueError("模型请求预算无效")
        retained, removed, catalogs = _project_catalogs(payload)
        _reject_unsafe_calls(payload.get("input", []))
        choice = payload.get("tool_choice")
        if isinstance(choice, dict) and not (choice.get("type") in {"function", "custom"}
                                             and allowed_tool(str(choice.get("name", "")))):
            raise ValueError("强制工具不在受控目录")
        record = {"requested_model": payload["model"], "reasoning_effort": reasoning.get("effort"),
                  "retained_tools": retained, "removed_tools": removed, "tool_catalogs": catalogs, "forwarded": False,
                  "removed_host_instructions": host_instructions, "input_blocks": _input_metadata(payload),
                  "request_field_types": {key: {"type": type(value).__name__,
                                                "length": len(value) if isinstance(value, (str, list, dict)) else None}
                                          for key, value in payload.items()},
                  "completed": False, "usage": None, "response_model": None, "error_type": None}
        if self.settings.auth_mode == "codex_oauth":
            # ChatGPT Codex Responses 不接受 max_output_tokens；原生 Codex
            # 客户端本来也不发送该字段，调用预算由代理计数和超时控制。
            payload.pop("max_output_tokens", None)
        else:
            payload["max_output_tokens"] = min(tokens, self.settings.max_output_tokens)
        return payload, record

    async def _respond(self, request: Request) -> Response:
        """认证与原子预算判断先于真正出站，探测请求单独计数。"""
        if request.headers.get("authorization") != f"Bearer {self.bearer_token}":
            return self._error("Local evaluation authentication failed", 401)
        if self._closed:
            return self._error("Local evaluation proxy is closed", 503)
        origin = request.headers.get("origin")
        if origin is not None and origin != self.endpoint.removesuffix("/v1"):
            return self._error("Unexpected request origin", 403)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_REQUEST_BYTES:
                return self._error("Evaluation request is too large", 413)
        parsed = None
        try:
            parsed = json.loads(body)
            payload, record = self._validate_payload(parsed)
        except (ValueError, TypeError, RecursionError) as error:
            # 校验异常来自本文件的固定合同文本，可安全反馈给模型以便纠正
            # 输入；请求正文、密钥和供应商响应仍不会回显。
            message = str(error) or "请求字段不符合评测合同"
            self._rejected_requests.append({"error_type": type(error).__name__, "message": message,
                                             "input_blocks": _input_metadata(parsed)})
            return self._error(f"Invalid evaluation model request: {message}")
        if self.probe_only:
            self.requests.append(record)
            return self._error("Evaluation probe complete; no model request was forwarded")
        if len(self.requests) >= self.settings.max_model_calls:
            self.blocked_calls += 1
            return self._error("Evaluation model call budget exhausted")
        record["forwarded"] = True
        self.requests.append(record)
        try:
            headers = {"Authorization": f"Bearer {self.settings.api_key}", "Content-Type": "application/json"}
            if self.settings.auth_mode == "codex_oauth":
                headers["originator"] = "moviepilot"
                if self.settings.account_id:
                    headers["ChatGPT-Account-Id"] = self.settings.account_id
            upstream = await self._client.send(self._client.build_request(
                "POST", f"{self.settings.base_url.rstrip('/')}/responses", json=payload,
                headers=headers,
            ), stream=True)
        except httpx.HTTPError as error:
            record["error_type"] = type(error).__name__
            return self._error("Configured evaluation model provider could not be reached", 502)
        if upstream.status_code != 200:
            record["http_status"] = upstream.status_code
            record["error_type"] = "upstream_rejected"
            await upstream.aclose()
            return self._error("Configured evaluation model provider rejected the request")
        if payload.get("stream"):
            return _ModelStreamingResponse(self._stream(upstream, record))
        return await self._response(upstream, record)

    async def _response(self, upstream: httpx.Response, record: dict[str, Any]) -> Response:
        """非流式响应同样有大小和工具边界，错误正文始终留在代理内部。"""
        body = bytearray()
        try:
            async for chunk in upstream.aiter_bytes():
                body.extend(chunk)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise ValueError("响应超限")
            response = json.loads(body)
            self._validate_response(response)
            self._capture(response, record)
            return JSONResponse(response)
        except (ValueError, TypeError, RecursionError, httpx.HTTPError):
            record["error_type"] = "invalid_response"
            return self._error("Model response violated the evaluation contract")
        finally:
            await upstream.aclose()

    def _validate_response(self, value: Any) -> None:
        """在任何正文交给原生客户端之前拒绝凭据、非法调用与非对象响应。"""
        if not isinstance(value, dict) or self.settings.api_key in "".join(_strings(value)):
            raise ValueError("响应格式或凭据边界无效")
        _reject_unsafe_calls(value)

    @staticmethod
    def _capture(response: dict[str, Any], record: dict[str, Any]) -> None:
        """只记录完整非负用量；响应声明完成与流后续失败分别保留。"""
        if not isinstance(response, dict):
            raise ValueError("响应完成对象无效")
        record["completed"] = response.get("status") == "completed"
        usage = response.get("usage")
        record["usage"] = ({key: usage[key] for key in TOKEN_KEYS} if isinstance(usage, dict) and all(
            type(usage.get(key)) is int and usage[key] >= 0 for key in TOKEN_KEYS
        ) else None)
        model = response.get("model")
        record["response_model"] = model if isinstance(model, str) else None

    def _event(self, raw: bytes, record: dict[str, Any], tails: dict[str, str]) -> str:
        """校验完整 SSE 帧以及跨帧文本片段，非法调用所在帧不向客户端发送。"""
        text = raw.decode("utf-8")
        data = "\n".join(line[5:].removeprefix(" ") for line in text.splitlines() if line.startswith("data:"))
        if self.settings.api_key in text:
            raise ValueError("事件包含私有凭据")
        if data and data != "[DONE]":
            value = json.loads(data)
            self._validate_response(value)
            delta = value.get("delta")
            if isinstance(delta, str):
                identity = str(value.get("item_id", ""))
                combined = tails.get(identity, "") + delta
                if self.settings.api_key in combined:
                    raise ValueError("跨事件包含私有凭据")
                tails[identity] = combined[-max(1, len(self.settings.api_key) - 1):]
            kind = value.get("type")
            if kind in {"response.completed", "response.incomplete", "response.failed"}:
                self._capture(value.get("response"), record)
                record["terminal_event"] = kind
            elif kind == "error":
                record["error_type"] = "upstream_stream_error"
        return text + "\n\n"

    async def _stream(self, upstream: httpx.Response, record: dict[str, Any]) -> AsyncGenerator[str, None]:
        """完整事件校验后转发，终态交付后即关闭上游，不再等待客户端通常忽略的尾帧。"""
        pending = bytearray()
        tails: dict[str, str] = {}
        received = 0
        try:
            async for chunk in upstream.aiter_bytes():
                received += len(chunk)
                if received > MAX_RESPONSE_BYTES:
                    raise ValueError("事件流总量超限")
                pending.extend(chunk)
                while True:
                    match = re.search(rb"\r?\n\r?\n", pending)
                    if match is None:
                        break
                    if match.start() > MAX_EVENT_BYTES:
                        raise ValueError("事件超限")
                    raw = bytes(pending[:match.start()])
                    del pending[:match.end()]
                    yield self._event(raw, record, tails)
                    if "terminal_event" in record:
                        return
                if len(pending) > MAX_EVENT_BYTES:
                    raise ValueError("事件超限")
            if pending:
                yield self._event(bytes(pending), record, tails)
                if "terminal_event" in record:
                    return
            if "terminal_event" not in record and record["error_type"] is None:
                raise ValueError("事件流缺少终态")
        except (ValueError, TypeError, RecursionError, httpx.HTTPError):
            record["error_type"] = "invalid_stream"
            record["completed"] = False
            yield 'event: error\ndata: {"error":{"type":"evaluation_error","message":"Model stream violated the evaluation contract"}}\n\n'
        except (asyncio.CancelledError, GeneratorExit):
            self._record_disconnect(record)
            raise
        finally:
            try:
                await upstream.aclose()
            except asyncio.CancelledError:
                self._record_disconnect(record)
                raise

    @staticmethod
    def _record_disconnect(record: dict[str, Any]) -> None:
        """客户端收到终态后关闭属于正常完成，终态前断开则保留未完成事实。"""
        if "terminal_event" in record:
            record["client_closed_after_terminal"] = True
        else:
            record["error_type"] = "stream_cancelled"
            record["completed"] = False

    def snapshot(self) -> dict[str, Any]:
        """探测不计真实消耗；缺失用量保持未知，已知总量是可审计下界。"""
        records = copy.deepcopy(self.requests)
        forwarded = [record for record in records if record["forwarded"]]
        known = [record["usage"] for record in forwarded if isinstance(record["usage"], dict)]
        return {"model_calls": len(forwarded), "probe_requests": len(records) - len(forwarded),
                "completed_model_calls": sum(bool(record["completed"]) for record in forwarded),
                "blocked_model_calls": self.blocked_calls, "usage_complete": len(known) == len(forwarded),
                "reported_models": sorted({record["response_model"] for record in forwarded
                                           if isinstance(record["response_model"], str)}),
                "tokens": {key: sum(usage[key] for usage in known) for key in TOKEN_KEYS} if known else None,
                "model_requests": records, "server_error": self._server_error,
                "rejected_requests": copy.deepcopy(self._rejected_requests)}
