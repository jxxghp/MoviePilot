"""仅绑定回环地址的受控 MCP JSON-RPC 子集，不加载 MoviePilot 运行时或真实业务服务。"""

import asyncio
import hashlib
import json
import re
import secrets
import socket
import time
from collections import Counter, OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import uvicorn
import yaml  # type: ignore[import-untyped]  # 锁定的 PyYAML 不提供类型声明。
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from scripts.evaluation.world import EvaluationWorld

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
MAX_REQUEST_BYTES = 1024 * 1024
MAX_SKILL_BYTES = 512 * 1024
MAX_RESULT_BYTES = 1024 * 1024
MAX_TOTAL_RESULT_BYTES = 4 * MAX_RESULT_BYTES
MAX_RESULTS = 8
RESULT_TTL_SECONDS = 900
FIRST_PAGE_CHARS = 8192
NEXT_PAGE_CHARS = 16000

# 与 Agent 的 MoviePilotApiInput 保持相同字段，不提前暴露场景支持操作或 oracle 判据。
API_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object", "required": ["operation_id"], "additionalProperties": False,
    "properties": {
        "operation_id": {"type": "string", "description": (
            "Exact allowlisted MoviePilot operation ID selected from the loaded domain Skill. "
            "Never supply a URL, authentication header, or API token."
        )},
        "path_params": {"type": "object", "additionalProperties": True, "description": "Route placeholder values declared by the selected operation."},
        "query": {"type": "object", "additionalProperties": True, "description": "Query-string fields declared by the selected operation."},
        "body": {"default": None, "description": (
            "JSON request value declared by the selected operation and its loaded Skill contract. "
            "Most operations use an object; a oneOf branch may require an exact scalar."
        )},
    },
}


@dataclass(frozen=True)
class _StoredResult:
    """本次评测独占的有界结果归档，MCP 客户端会话共享但其他服务实例不可读取。"""

    text: str
    tool_name: str
    byte_size: int
    expires_at: float


@dataclass
class _Session:
    """只保存 MCP 握手状态，不拥有或重置业务世界。"""

    protocol: str
    initialized: bool = False


class _LocalServer(uvicorn.Server):  # type: ignore[misc]  # follow_imports=skip 不展开外部 Server 基类。
    """让控制器拥有服务启停，嵌入运行时不替换进程信号处理器。"""

    def __init__(self, config: uvicorn.Config, ready: asyncio.Event) -> None:
        """通过事件通知实际套接字监听已就绪。"""
        super().__init__(config)
        self.ready = ready

    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        """主控制器处理取消和信号，MCP 服务不接管全局状态。"""
        yield

    async def startup(self, sockets: Optional[list[socket.socket]] = None) -> None:
        """完成 uvicorn 的真实监听后才允许模型开始握手。"""
        await super().startup(sockets=sockets)
        self.ready.set()


class EvaluationMcpServer:
    """为一个独立世界提供受认证的本地 Streamable HTTP 必需子集。"""

    def __init__(self, world: EvaluationWorld, *, root_dir: Optional[Path] = None, max_world_calls: int = 32) -> None:
        """固定世界与可信源码目录；模型不能通过请求选择场景、文件或真实后端。"""
        if type(max_world_calls) is not int or not 1 <= max_world_calls <= 32:
            raise ValueError("max_world_calls 必须为 1 到 32 的整数")
        self._world = world
        self._max_world_calls = max_world_calls
        self._world_calls = 0
        self._blocked_world_calls = 0
        self._tool_calls: Counter[str] = Counter()
        self._bearer_token = secrets.token_urlsafe(32)
        self._endpoint: Optional[str] = None
        self._listener: Optional[socket.socket] = None
        self._server: Optional[_LocalServer] = None
        self._task: Optional[asyncio.Task[None]] = None
        self._ready = asyncio.Event()
        self._lifecycle_lock = asyncio.Lock()
        self._closed = False
        self._sessions: dict[str, _Session] = {}
        self._results: OrderedDict[str, _StoredResult] = OrderedDict()
        self._skill = self._load_skill(Path(root_dir) if root_dir is not None else Path(__file__).resolve().parents[2])

    @property
    def endpoint(self) -> str:
        """控制器在 start 完成后取得回环 MCP 地址，不包含认证秘密。"""
        if self._endpoint is None or self._closed:
            raise RuntimeError("MCP 评测服务尚未运行")
        return self._endpoint

    @property
    def bearer_token(self) -> str:
        """仅供控制器放入客户端局部环境，不会出现在任何工具或协议响应中。"""
        return self._bearer_token

    @property
    def stats(self) -> dict[str, Any]:
        """提供无场景答案、无参数正文的控制器计数快照。"""
        return {"world_calls": self._world_calls, "blocked_world_calls": self._blocked_world_calls,
                "tool_calls": dict(self._tool_calls), "max_world_calls": self._max_world_calls}

    @property
    def skill_sha256(self) -> str:
        """控制器可记录实际公开技能正文的指纹，模型工具不暴露额外上下文。"""
        return hashlib.sha256(self._skill["content"].encode("utf-8")).hexdigest()

    async def __aenter__(self) -> "EvaluationMcpServer":
        """异步上下文进入时等待真实端点就绪。"""
        return await self.start()

    async def __aexit__(self, *_args: Any) -> None:
        """控制器正常退出、失败或取消时均关闭端点。"""
        await self.close()

    async def start(self) -> "EvaluationMcpServer":
        """绑定系统分配的 127.0.0.1 端口，不读取任何业务运行配置。"""
        async with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("已关闭的评测服务不能重新启动")
            if self._task is not None:
                return self
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._listener = listener
            try:
                listener.bind(("127.0.0.1", 0))
                listener.setblocking(False)
                self._endpoint = f"http://127.0.0.1:{listener.getsockname()[1]}/mcp"
                app = Starlette(routes=[Route("/mcp", self._handle, methods=["POST", "GET", "DELETE"])])
                config = uvicorn.Config(app, host="127.0.0.1", log_config=None, log_level=None, access_log=False,
                                        lifespan="off", proxy_headers=False, ws="none", timeout_graceful_shutdown=2)
                self._server = _LocalServer(config, self._ready)
                self._task = asyncio.create_task(self._server.serve(sockets=[listener]))
                self._task.add_done_callback(lambda _task: self._ready.set())
                await asyncio.wait_for(self._ready.wait(), timeout=10)
                if self._task.done() or not self._server.started:
                    raise RuntimeError("本地 MCP 服务启动失败")
            except BaseException:
                await self._stop()
                raise
        return self

    async def close(self) -> None:
        """显式、幂等关闭 HTTP 服务，保留业务世界供控制器独立评分。"""
        async with self._lifecycle_lock:
            await self._stop()

    async def _stop(self) -> None:
        """有限等待服务退出，异常路径也回收监听套接字与归档。"""
        self._closed = True
        try:
            if self._server is not None:
                self._server.should_exit = True
            if self._task is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(self._task), timeout=5)
                except TimeoutError:
                    self._task.cancel()
                    await asyncio.gather(self._task, return_exceptions=True)
                except asyncio.CancelledError:
                    self._task.cancel()
                    await asyncio.gather(self._task, return_exceptions=True)
                    raise
        finally:
            if self._listener is not None:
                self._listener.close()
            self._sessions.clear()
            self._results.clear()

    @staticmethod
    def _error(request_id: Any, code: int, message: str, status: int = 400) -> JSONResponse:
        """错误只使用固定协议文案，不回显异常、环境、参数或私有文件路径。"""
        return JSONResponse({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}, status_code=status)

    def _http_error(self, request: Request) -> Optional[Response]:
        """先校验认证、Host 与 Origin，防止回环服务被浏览器或代理借用。"""
        expected = f"Bearer {self._bearer_token}"
        if len(request.headers.getlist("authorization")) != 1 or not secrets.compare_digest(
            request.headers.get("authorization", "").encode("utf-8"), expected.encode("ascii"),
        ):
            return self._error(None, -32001, "Unauthorized", 401)
        origin = self.endpoint.removesuffix("/mcp")
        if request.headers.get("host") != origin.removeprefix("http://") or request.headers.get("origin", origin) != origin:
            return self._error(None, -32001, "Forbidden", 403)
        if request.client is None or request.client.host != "127.0.0.1":
            return self._error(None, -32001, "Forbidden", 403)
        version = request.headers.get("mcp-protocol-version")
        if version is not None and version not in PROTOCOL_VERSIONS:
            return self._error(None, -32600, "Unsupported protocol version")
        return None

    async def _handle(self, request: Request) -> Response:
        """处理 JSON-RPC POST；本服务不提供服务器推送 SSE 或远程关闭能力。"""
        rejected = self._http_error(request)
        if rejected is not None:
            return rejected
        if request.method != "POST":
            return Response(status_code=405, headers={"Allow": "POST"})
        accepted = {item.split(";", 1)[0].strip().lower() for item in request.headers.get("accept", "").split(",")}
        if not {"application/json", "text/event-stream"} <= accepted:
            return self._error(None, -32600, "Accept must include application/json and text/event-stream", 406)
        if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
            return self._error(None, -32600, "Content-Type must be application/json", 415)
        try:
            data = bytearray()
            async for chunk in request.stream():
                data.extend(chunk)
                if len(data) > MAX_REQUEST_BYTES:
                    return self._error(None, -32600, "Request too large", 413)
            payload = json.loads(data.decode("utf-8"), parse_constant=self._invalid_json_constant)
        except (ValueError, UnicodeError):
            return self._error(None, -32700, "Parse error")
        if not self._valid_request(payload):
            return self._error(None, -32600, "Invalid Request")
        try:
            return self._dispatch(request, payload)
        except Exception:
            return self._error(payload.get("id"), -32603, "Internal error", 500)

    @staticmethod
    def _invalid_json_constant(_value: str) -> None:
        """JSON-RPC 仅接受标准 JSON，拒绝 Python 默认允许的 NaN/Infinity 扩展。"""
        raise ValueError("Non-finite JSON number")

    @staticmethod
    def _valid_request(payload: Any) -> bool:
        """仅接受单条 JSON-RPC 请求或通知，拒绝 batch 和可混淆的标量类型。"""
        return (type(payload) is dict and payload.get("jsonrpc") == "2.0"
                and not set(payload) - {"jsonrpc", "id", "method", "params"}
                and type(payload.get("method")) is str and bool(payload["method"])
                and type(payload.get("params", {})) is dict
                and ("_meta" not in payload.get("params", {}) or type(payload["params"]["_meta"]) is dict)
                and ("id" not in payload or type(payload["id"]) in (str, int)))

    def _dispatch(self, request: Request, payload: dict[str, Any]) -> Response:
        """握手会话只管理协议状态，所有合法工具调用始终进入同一评测世界。"""
        method, params, request_id = payload["method"], payload.get("params", {}), payload.get("id")
        if method == "initialize":
            return self._initialize(params, request_id) if "id" in payload else self._error(None, -32600, "initialize requires an id")
        session = self._sessions.get(request.headers.get("mcp-session-id", ""))
        if session is None:
            return self._error(request_id, -32000, "Unknown MCP session", 404 if request.headers.get("mcp-session-id") else 400)
        if request.headers.get("mcp-protocol-version", session.protocol) != session.protocol:
            return self._error(request_id, -32600, "Protocol version differs from initialized session")
        if method == "notifications/initialized":
            if "id" in payload or set(params) - {"_meta"}:
                return self._error(request_id, -32600, "Invalid initialized notification")
            session.initialized = True
            return Response(status_code=202)
        if method == "notifications/cancelled" and "id" not in payload:
            if (set(params) - {"requestId", "reason", "_meta"} or type(params.get("requestId")) not in (str, int)
                    or ("reason" in params and type(params["reason"]) is not str)):
                return self._error(None, -32600, "Invalid cancellation notification")
            return Response(status_code=202)
        if "id" not in payload:
            return self._error(None, -32600, "Unsupported notification")
        if not session.initialized and method != "ping":
            return self._error(request_id, -32000, "MCP session is not initialized")
        if method in {"ping", "tools/list"}:
            allowed = {"_meta", "cursor"} if method == "tools/list" else {"_meta"}
            if set(params) - allowed or params.get("cursor") not in (None, ""):
                return self._error(request_id, -32602, "Invalid params")
            result: Any = {} if method == "ping" else {"tools": self._tool_catalog()}
        elif method == "tools/call":
            if set(params) - {"name", "arguments", "_meta"} or type(params.get("name")) is not str or type(params.get("arguments", {})) is not dict:
                return self._error(request_id, -32602, "Invalid tools/call params")
            result = self._call_tool(params["name"], params.get("arguments", {}))
        else:
            return self._error(request_id, -32601, "Method not found", 200)
        return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _initialize(self, params: dict[str, Any], request_id: Any) -> Response:
        """按正式握手版本协商，新客户端会话不会重置业务状态或调用预算。"""
        client = params.get("clientInfo")
        if (set(params) - {"protocolVersion", "capabilities", "clientInfo", "_meta"}
                or type(params.get("protocolVersion")) is not str
                or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", params["protocolVersion"])
                or type(params.get("capabilities")) is not dict or type(client) is not dict
                or any(type(client.get(key)) is not str or not client[key] for key in ("name", "version"))):
            return self._error(request_id, -32602, "Invalid initialize params")
        if len(self._sessions) >= 64:
            return self._error(request_id, -32000, "Session limit reached", 429)
        protocol = params["protocolVersion"] if params["protocolVersion"] in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        session_id = secrets.token_hex(24)
        self._sessions[session_id] = _Session(protocol)
        result = {"protocolVersion": protocol, "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": {"name": "MoviePilot Evaluation", "version": "1"},
                  "instructions": "Use the MoviePilot domain Skill to select API operations and interpret results."}
        return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result}, headers={"Mcp-Session-Id": session_id})

    @staticmethod
    def _tool_catalog() -> list[dict[str, Any]]:
        """只公开与 MoviePilot 任务输入等价的工具合同，不注入场景答案。"""
        return [
            {"name": "moviepilot_api", "description": (
                "Call allowlisted MoviePilot business APIs. Use the domain Skill to select operation_id, parameters, and failure handling. "
                "For collection counts, use the smallest documented page and read collection.total_count instead of querying the database after item truncation. "
                "Arbitrary URLs, commands, and authentication endpoints are forbidden."
            ), "inputSchema": deepcopy(API_INPUT_SCHEMA)},
            {"name": "read_skill", "description": "Read the named MoviePilot Skill including its full body and supporting file paths. Available skill: moviepilot-api.", "inputSchema": {
                "type": "object", "required": ["name"], "additionalProperties": False, "properties": {"name": {"type": "string"}},
            }},
            {"name": "read_tool_result", "description": "Read an archived tool result using result_id and next_offset. Offsets count Unicode characters. Results expire after 15 minutes or eviction; never guess result IDs.", "inputSchema": {
                "type": "object", "required": ["result_id"], "additionalProperties": False, "properties": {
                    "result_id": {"type": "string", "minLength": 32, "maxLength": 32},
                    "offset": {"type": "integer", "minimum": 0, "default": 0},
                    "limit": {"type": "integer", "minimum": 1, "maximum": NEXT_PAGE_CHARS, "default": 4000},
                },
            }},
        ]

    @staticmethod
    def _failure(error: str, message: str) -> dict[str, Any]:
        """工具失败使用固定四态字段，异常细节不交给模型。"""
        return {"success": False, "execution_outcome": "failed", "error": error, "message": message}

    def _call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """精确工具白名单与全局预算均在进入业务世界前检查。"""
        self._tool_calls[name if name in {"moviepilot_api", "read_skill", "read_tool_result"} else "unknown"] += 1
        try:
            if name == "moviepilot_api":
                if set(arguments) - set(API_INPUT_SCHEMA["properties"]) or type(arguments.get("operation_id")) is not str:
                    result: Any = self._failure("invalid_arguments", "Invalid moviepilot_api arguments")
                elif any(key in arguments and type(arguments[key]) is not dict for key in ("path_params", "query")):
                    result = self._failure("invalid_arguments", "path_params and query must be objects")
                elif self._world_calls >= self._max_world_calls:
                    self._blocked_world_calls += 1
                    result = self._failure("world_call_limit", "World tool call budget exhausted")
                else:
                    self._world_calls += 1
                    result = self._world.execute(**arguments)
            elif name == "read_skill":
                result = self._read_skill(arguments)
            elif name == "read_tool_result":
                result = self._read_result(arguments)
            else:
                result = self._failure("tool_not_found", "Unknown tool")
        except Exception:
            result = self._failure("evaluation_tool_error", "Controlled tool execution failed")
        text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=2)
        outcome = json.loads(text).get("execution_outcome")
        return {"content": [{"type": "text", "text": text}], "isError": outcome in {"failed", "unknown"}}

    @staticmethod
    def _load_skill(root: Path) -> dict[str, Any]:
        """读取唯一可信技能文件，不从请求中接受路径，也不加载宿主元数据解析器。"""
        skill_path = root / "skills/moviepilot-api/SKILL.md"
        with skill_path.open("rb") as handle:
            data = handle.read(MAX_SKILL_BYTES + 1)
        truncated = len(data) > MAX_SKILL_BYTES
        content = data[:MAX_SKILL_BYTES].decode("utf-8", errors="ignore" if truncated else "replace")
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
        metadata = yaml.safe_load(match.group(1)) if match else None
        if not isinstance(metadata, dict) or metadata.get("name") != "moviepilot-api":
            raise ValueError("moviepilot-api Skill 元数据无效")
        return {"success": True, "skill": {
            "id": "moviepilot-api", "name": "moviepilot-api", "description": str(metadata.get("description", ""))[:1024],
            "path": "skills/moviepilot-api/SKILL.md", "allowed_tools": str(metadata.get("allowed-tools", "")).split(),
            "allowed_api_operations": str(metadata.get("allowed-api-operations", "")).split(),
        }, "content": content, "content_limit_bytes": MAX_SKILL_BYTES, "supporting_files": sorted(
            str(path.relative_to(skill_path.parent)) for path in skill_path.parent.rglob("*") if path.is_file() and path != skill_path
        ), "truncated": truncated,
            "truncation_message": "SKILL.md exceeds 512 KiB; content contains only the first 512 KiB." if truncated else None}

    def _prune_results(self) -> None:
        """匹配 MoviePilot 的 15 分钟、8 项、4MiB 会话归档约束。"""
        now = time.monotonic()
        for key in list(self._results):
            if self._results[key].expires_at <= now:
                del self._results[key]
        while self._results and (len(self._results) > MAX_RESULTS or sum(value.byte_size for value in self._results.values()) > MAX_TOTAL_RESULT_BYTES):
            self._results.popitem(last=False)

    @staticmethod
    def _bounded_page(build: Any, limit: int, length: int) -> str:
        """按最终 JSON 文本长度二分页边界，游标只覆盖实际交付的字符。"""
        low, high = 0, length
        best = json.dumps(build(0), ensure_ascii=False, indent=2)
        while low <= high:
            middle = (low + high) // 2
            candidate = json.dumps(build(middle), ensure_ascii=False, indent=2)
            if len(candidate) <= limit:
                best, low = candidate, middle + 1
            else:
                high = middle - 1
        return best

    def _read_skill(self, arguments: dict[str, Any]) -> Any:
        """返回完整真实技能的首屏或可续读归档，不提供精简场景专用说明。"""
        if set(arguments) != {"name"} or arguments.get("name") != "moviepilot-api":
            return self._failure("skill_not_found", "Unknown Skill")
        text = json.dumps(self._skill, ensure_ascii=False, indent=2)
        if len(text) <= FIRST_PAGE_CHARS:
            return text
        if len(text.encode("utf-8")) > MAX_RESULT_BYTES:
            return self._failure("result_too_large", "Skill result exceeds archive limit")
        result_id = secrets.token_hex(16)
        self._results[result_id] = _StoredResult(text, "read_skill", len(text.encode("utf-8")), time.monotonic() + RESULT_TTL_SECONDS)
        self._prune_results()

        def page(length: int) -> dict[str, Any]:
            """首屏与 MoviePilot 归档预览使用相同的定位字段。"""
            return {"result_id": result_id, "read_tool": "read_tool_result", "expires_in_seconds": RESULT_TTL_SECONDS,
                    "offset_unit": "unicode_characters", "next_offset": length, "execution_outcome": "succeeded",
                    "tool_result_truncated": True, "tool_name": "read_skill", "total_chars": len(text),
                    "returned_chars": length, "content_preview": text[:length],
                    "message": "完整结果已在当前会话临时保存；使用 read_tool_result 的 result_id 和 next_offset 继续读取，无需重复执行原工具。"}
        return self._bounded_page(page, FIRST_PAGE_CHARS, len(text))

    def _read_result(self, arguments: dict[str, Any]) -> Any:
        """按 Unicode 字符续读本次服务归档，非法位置不得生成伪观察内容。"""
        offset, limit, result_id = arguments.get("offset", 0), arguments.get("limit", 4000), arguments.get("result_id")
        if (set(arguments) - {"result_id", "offset", "limit"} or type(result_id) is not str or len(result_id) != 32
                or type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= NEXT_PAGE_CHARS):
            return self._failure("invalid_arguments", "Invalid result cursor or limit")
        self._prune_results()
        result = self._results.get(result_id)
        if result is None:
            return self._failure("result_unavailable", "Result unavailable")
        if offset > len(result.text):
            return self._failure("offset_out_of_range", "Result offset out of range")

        def page(length: int) -> dict[str, Any]:
            """最终响应预算包含转义开销，next_offset 始终对应实际返回的正文。"""
            end = offset + length
            return {"success": True, "result_id": result_id, "tool_name": result.tool_name, "offset": offset,
                    "next_offset": end if end < len(result.text) else None, "total_chars": len(result.text), "content": result.text[offset:end]}
        return self._bounded_page(page, NEXT_PAGE_CHARS, min(limit, len(result.text) - offset))
