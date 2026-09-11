"""通过真实回环 HTTP 验证受控 MCP 握手、世界共享、结果分页和关闭，不访问外部服务。"""

import ast
import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from scripts.evaluation import server as server_module
from scripts.evaluation.server import API_INPUT_SCHEMA, DIRECT_RESULT_MAX_CHARS, EvaluationMcpServer
from scripts.evaluation.world import EvaluationWorld

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _headers(server: EvaluationMcpServer) -> dict[str, str]:
    """客户端局部传入随机认证值，不读取进程凭据或代理设置。"""
    return {"Authorization": f"Bearer {server.bearer_token}", "Accept": "application/json, text/event-stream"}


async def _rpc(client: httpx.AsyncClient, method: str, params: Any = None, request_id: Any = 1) -> httpx.Response:
    """通过实际 TCP 端点发送单条 JSON-RPC 请求。"""
    payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        payload["params"] = params
    return await client.post("/mcp", json=payload)


@asynccontextmanager
async def _client(server: EvaluationMcpServer, protocol: str = "2025-11-25") -> AsyncIterator[httpx.AsyncClient]:
    """完成真实 initialize/initialized 握手后交付一个独立 MCP 客户端会话。"""
    async with httpx.AsyncClient(base_url=server.endpoint.removesuffix("/mcp"), headers=_headers(server), trust_env=False, timeout=5) as client:
        response = await _rpc(client, "initialize", {
            "protocolVersion": protocol, "capabilities": {}, "clientInfo": {"name": "offline-test", "version": "1"},
        })
        assert response.status_code == 200
        client.headers["Mcp-Session-Id"] = response.headers["Mcp-Session-Id"]
        client.headers["MCP-Protocol-Version"] = response.json()["result"]["protocolVersion"]
        initialized = await client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert initialized.status_code == 202 and not initialized.content
        yield client


async def _tool(client: httpx.AsyncClient, name: str, arguments: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """读取 MCP 标准文本工具结果，业务状态仍从其独立 JSON 协议判断。"""
    response = await _rpc(client, "tools/call", {"name": name, "arguments": arguments})
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["content"][0]["type"] == "text"
    return result, json.loads(result["content"][0]["text"])


@pytest.mark.asyncio
async def test_real_http_handshake_exposes_only_equivalent_public_tools() -> None:
    """端点真实监听回环，公开目录没有场景名称、初态、账本或 oracle 入口。"""
    world = EvaluationWorld("dedup_existing")
    async with EvaluationMcpServer(world) as server:
        assert server.endpoint.startswith("http://127.0.0.1:")
        async with _client(server) as client:
            response = await _rpc(client, "tools/list")
            tools = response.json()["result"]["tools"]
            assert [tool["name"] for tool in tools] == ["moviepilot_api", "read_skill", "read_tool_result"]
            assert tools[0]["inputSchema"] == API_INPUT_SCHEMA
            assert set(tools[0]["inputSchema"]["properties"]) == {"operation_id", "path_params", "query", "body"}
            serialized = json.dumps(response.json())
            for private in ("dedup_existing", "honest_unknown", "oracle", "snapshot", "ledger", server.bearer_token, str(PROJECT_ROOT)):
                assert private not in serialized
            assert (await _rpc(client, "ping")).json()["result"] == {}
            assert (await client.get("/mcp")).status_code == 405
            assert (await client.delete("/mcp")).status_code == 405
    assert world.snapshot() == world.initial_snapshot()


@pytest.mark.asyncio
async def test_two_client_sessions_share_unknown_side_effect_and_duplicate_attempt() -> None:
    """新 MCP 连接不重置世界，未知回执后的副作用能被另一会话读取确认。"""
    world = EvaluationWorld("unknown_download")
    body = {"torrent_in": {"title": world.scenario.title, "enclosure": world.scenario.magnet},
            "media_source": world.scenario.media_source, "media_id": world.scenario.media_id}
    async with EvaluationMcpServer(world) as server:
        async with _client(server) as first, _client(server) as second:
            assert first.headers["Mcp-Session-Id"] != second.headers["Mcp-Session-Id"]
            result, unknown = await _tool(first, "moviepilot_api", {"operation_id": "download.add", "body": body})
            assert result["isError"] is True and unknown["execution_outcome"] == "unknown"
            _, observed = await _tool(second, "moviepilot_api", {"operation_id": "download.tasks.active"})
            assert any(row["infohash"] == world.scenario.infohash for row in observed["data"])
            _, duplicate = await _tool(second, "moviepilot_api", {"operation_id": "download.add", "body": body})
            assert duplicate["execution_outcome"] == "failed"
            assert world.ledger[-1]["duplicate_attempt"] is True
            assert server.stats["world_calls"] == 3
    assert sum(row["infohash"] == world.scenario.infohash for row in world.snapshot()["downloads"]) == 1


@pytest.mark.asyncio
async def test_skill_pagination_reconstructs_unmodified_repository_skill() -> None:
    """首屏与后页实际 JSON 文本有界，所有分页拼接后仍是完整真实技能而非评测特制摘要。"""
    async with EvaluationMcpServer(EvaluationWorld("dedup_existing")) as server:
        async with _client(server) as client:
            first_result, first = await _tool(client, "read_skill", {"name": "moviepilot-api"})
            assert len(first_result["content"][0]["text"]) <= DIRECT_RESULT_MAX_CHARS
            if first.get("tool_result_truncated"):
                assert len(first_result["content"][0]["text"]) <= 8192
                restored = first["content_preview"]
                cursor = first["next_offset"]
                while cursor is not None:
                    response, page = await _tool(client, "read_tool_result", {"result_id": first["result_id"], "offset": cursor, "limit": 16000})
                    assert len(response["content"][0]["text"]) <= 16000
                    assert page["offset"] == len(restored)
                    restored += page["content"]
                    if page["next_offset"] is not None:
                        assert page["next_offset"] > cursor
                    cursor = page["next_offset"]
            else:
                restored = first_result["content"][0]["text"]
            skill = json.loads(restored)
            assert skill["content"] == (PROJECT_ROOT / "skills/moviepilot-api/SKILL.md").read_text(encoding="utf-8")
            assert skill["skill"]["name"] == "moviepilot-api"
            assert skill["skill"]["allowed_tools"] == ["moviepilot_api"]
            assert len(skill["skill"]["allowed_api_operations"]) > 100
            assert "library.exists" in skill["skill"]["allowed_api_operations"]
            skill_root = PROJECT_ROOT / "skills" / "moviepilot-api"
            expected_supporting_files = sorted(
                path.relative_to(skill_root).as_posix()
                for path in (skill_root / "api").glob("*.md")
            )
            assert skill["supporting_files"] == expected_supporting_files
            assert server.stats["world_calls"] == 0


@pytest.mark.asyncio
async def test_skill_supporting_document_uses_read_skill_without_file_tool() -> None:
    """评测服务应通过 read_skill 加载已列出的分类合同，而不是开放任意文件读取。"""
    async with EvaluationMcpServer(EvaluationWorld("dedup_existing")) as server:
        async with _client(server) as client:
            result, payload = await _tool(
                client,
                "read_skill",
                {"name": "moviepilot-api", "file": "api/config.md"},
            )
            assert result["isError"] is False
            if payload.get("tool_result_truncated"):
                assert "# Configuration APIs" in payload["content_preview"]
            else:
                assert "# Configuration APIs" in payload["content"]

            invalid_result, invalid = await _tool(
                client,
                "read_skill",
                {"name": "moviepilot-api", "file": "../SKILL.md"},
            )
            assert invalid_result["isError"] is True
            assert invalid["error"] == "skill_file_not_found"


@pytest.mark.asyncio
async def test_invalid_operation_input_returns_operation_contract() -> None:
    """仿真工具报错时应把当前 operation 的正确输入合同交给模型。"""
    async with EvaluationMcpServer(EvaluationWorld("dedup_existing")) as server:
        async with _client(server) as client:
            result, payload = await _tool(
                client,
                "moviepilot_api",
                {"operation_id": "site.list", "query": {"status": "enabled"}},
            )
            assert result["isError"] is True
            assert payload["operation_id"] == "site.list"
            assert payload["input_contract"]["query"]["fields"]["status"]["enum"] == [
                "active", "inactive", "all",
            ]
            assert "enabled" not in json.dumps(payload, ensure_ascii=False)


@pytest.mark.asyncio
async def test_world_call_limit_is_global_across_sessions_and_parallel_requests() -> None:
    """并行连接也共享硬调用上限，超限不会继续读取或改变独立世界。"""
    world = EvaluationWorld("dedup_existing")
    async with EvaluationMcpServer(world, max_world_calls=2) as server:
        async with _client(server) as first, _client(server) as second:
            replies = await asyncio.gather(*[
                _tool(client, "moviepilot_api", {"operation_id": "site.list"}) for client in (first, second, first, second)
            ])
            assert sum(payload.get("error") == "world_call_limit" for _, payload in replies) == 2
            assert len(world.ledger) == 2
            assert server.stats["world_calls"] == 2
            assert server.stats["blocked_world_calls"] == 2
            assert server.stats["tool_calls"]["moviepilot_api"] == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("headers,expected", [
    ({"Authorization": "Bearer invalid"}, 401), ({"Authorization": ""}, 401),
    ({"Origin": "https://outside.invalid"}, 403), ({"Host": "outside.invalid"}, 403),
    ({"MCP-Protocol-Version": "unknown"}, 400), ({"Accept": "application/json"}, 406),
])
async def test_http_boundary_rejects_bad_auth_origin_host_protocol_and_accept(headers: dict[str, str], expected: int) -> None:
    """认证与 HTTP 协议失败不能触及世界，也不能回显服务器秘密。"""
    world = EvaluationWorld("dedup_existing")
    async with EvaluationMcpServer(world) as server:
        async with _client(server) as client:
            response = await client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            assert response.status_code == expected
            assert server.bearer_token not in response.text
            assert not world.ledger


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [[], {"jsonrpc": "1.0", "id": 1, "method": "ping"},
    {"jsonrpc": "2.0", "id": True, "method": "ping"}, {"jsonrpc": "2.0", "id": 1, "method": []},
    {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": []},
    {"jsonrpc": "2.0", "id": 1, "method": "ping", "oracle": True},
])
async def test_jsonrpc_shape_errors_are_fixed_and_do_not_execute(payload: Any) -> None:
    """batch、错误 ID/参数类型及未知顶层字段均明确拒绝。"""
    world = EvaluationWorld("dedup_existing")
    async with EvaluationMcpServer(world) as server:
        async with _client(server) as client:
            response = await client.post("/mcp", json=payload)
            assert response.status_code == 400
            assert response.json()["error"]["code"] == -32600
            assert not world.ledger


@pytest.mark.asyncio
@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
async def test_non_finite_json_numbers_are_rejected_before_world_execution(constant: str) -> None:
    """非标准 JSON 数字不能被解析后进入业务状态或破坏响应序列化。"""
    world = EvaluationWorld("dedup_existing")
    async with EvaluationMcpServer(world) as server:
        async with _client(server) as client:
            response = await client.post("/mcp", headers={"Content-Type": "application/json"}, content=(
                '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"moviepilot_api",'
                '"arguments":{"operation_id":"site.list","query":{"count":' + constant + '}}}}'
            ))
            assert response.status_code == 400
            assert response.json()["error"]["code"] == -32700
            assert not world.ledger


@pytest.mark.asyncio
async def test_initialization_version_negotiation_and_session_requirements() -> None:
    """协商支持版本，已建会话拒绝协议切换；初始化完成前不开放工具。"""
    async with EvaluationMcpServer(EvaluationWorld("dedup_existing")) as server:
        async with httpx.AsyncClient(base_url=server.endpoint.removesuffix("/mcp"), headers=_headers(server), trust_env=False) as client:
            assert (await _rpc(client, "tools/list")).status_code == 400
            response = await _rpc(client, "initialize", {"protocolVersion": "2099-01-01", "capabilities": {}, "clientInfo": {"name": "future", "version": "1"}})
            assert response.json()["result"]["protocolVersion"] == "2025-11-25"
            client.headers["Mcp-Session-Id"] = response.headers["Mcp-Session-Id"]
            assert (await _rpc(client, "tools/list")).status_code == 400
            client.headers["MCP-Protocol-Version"] = "2025-06-18"
            assert (await _rpc(client, "ping")).status_code == 400
            client.headers["MCP-Protocol-Version"] = "2025-11-25"
            assert (await _rpc(client, "ping")).status_code == 200
        async with _client(server, "2025-03-26") as client:
            assert (await _rpc(client, "tools/list")).status_code == 200


@pytest.mark.asyncio
async def test_unknown_tools_and_invalid_arguments_never_expose_control_state() -> None:
    """只能执行三种公开工具，路径穿越、私有状态名称和错放字段都不能读取控制器状态。"""
    world = EvaluationWorld("dedup_existing")
    async with EvaluationMcpServer(world) as server:
        async with _client(server) as client:
            for name, arguments in [
                ("snapshot", {}), ("ledger", {}), ("read_skill", {"name": "../../scripts/evaluation/score.py"}),
                ("moviepilot_api", {"operation_id": "site.list", "scenario_id": "honest_unknown"}),
                ("moviepilot_api", {"operation_id": "site.list", "query": []}),
                ("read_tool_result", {"result_id": "0" * 32, "offset": True}),
            ]:
                result, payload = await _tool(client, name, arguments)
                assert result["isError"] is True
                assert payload["execution_outcome"] == "failed"
                assert "subscriptions" not in payload and "ledger" not in payload
            assert not world.ledger
            missing = await _rpc(client, "resources/read", {"uri": "oracle"})
            assert missing.json()["error"]["code"] == -32601


@pytest.mark.asyncio
async def test_archive_expiry_and_cross_server_isolation(monkeypatch) -> None:
    """归档跨同次运行的会话可用，但别的 server 或过期归档不能被猜 ID 读取。"""
    monkeypatch.setattr(server_module, "DIRECT_RESULT_MAX_CHARS", 1)
    async with EvaluationMcpServer(EvaluationWorld("dedup_existing")) as first_server, EvaluationMcpServer(EvaluationWorld("unknown_download")) as second_server:
        async with _client(first_server) as first, _client(first_server) as sibling, _client(second_server) as second:
            _, preview = await _tool(first, "read_skill", {"name": "moviepilot-api"})
            arguments = {"result_id": preview["result_id"], "offset": preview["next_offset"]}
            result, _ = await _tool(sibling, "read_tool_result", arguments)
            assert result["isError"] is False
            _, unavailable = await _tool(second, "read_tool_result", arguments)
            assert unavailable["error"] == "result_unavailable"
            key = preview["result_id"]
            first_server._results[key] = replace(first_server._results[key], expires_at=0)
            _, expired = await _tool(first, "read_tool_result", arguments)
            assert expired["error"] == "result_unavailable"


@pytest.mark.asyncio
async def test_internal_tool_exception_does_not_leak_environment_or_details(monkeypatch: pytest.MonkeyPatch) -> None:
    """世界异常只能返回固定失败，不把进程秘密或异常正文传给模型。"""
    world = EvaluationWorld("dedup_existing")

    def fail(**_arguments: Any) -> dict[str, Any]:
        """模拟内部故障而不读取真实环境。"""
        raise RuntimeError("private-oracle-and-token-value")

    monkeypatch.setattr(world, "execute", fail)
    async with EvaluationMcpServer(world) as server:
        async with _client(server) as client:
            result, payload = await _tool(client, "moviepilot_api", {"operation_id": "site.list"})
            assert result["isError"] is True
            assert payload["error"] == "evaluation_tool_error"
            assert "private-oracle" not in json.dumps(result)


@pytest.mark.asyncio
async def test_close_stops_listener_and_cannot_be_triggered_by_client() -> None:
    """HTTP 客户端无权结束整次评测，控制器关闭后真实监听端口不可连接。"""
    server = EvaluationMcpServer(EvaluationWorld("dedup_existing"))
    await server.start()
    endpoint = server.endpoint
    async with _client(server) as client:
        assert (await _rpc(client, "shutdown")).json()["error"]["code"] == -32601
        assert (await _rpc(client, "ping")).status_code == 200
    await server.close()
    await server.close()
    assert server._task.done()
    with pytest.raises(RuntimeError):
        _ = server.endpoint
    async with httpx.AsyncClient(trust_env=False) as client:
        with pytest.raises(httpx.ConnectError):
            await client.post(endpoint, json={})


@pytest.mark.parametrize("limit", [0, 33, True, -1])
def test_invalid_world_call_budget_is_rejected(limit: Any) -> None:
    """控制器也不能把单次原生评测放大到声明的 32 次世界调用以上。"""
    with pytest.raises(ValueError):
        EvaluationMcpServer(EvaluationWorld("dedup_existing"), max_world_calls=limit)


def test_server_imports_no_moviepilot_runtime_or_oracle() -> None:
    """服务与世界都独立于 app 运行时，判定器不能通过服务依赖进入可见目录。"""
    tree = ast.parse((PROJECT_ROOT / "scripts/evaluation/server.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("app.")
            assert node.module != "scripts.evaluation.score"
