"""
覆盖类型收敛过程中明确化的外部响应、可选资源及动态模块边界。
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from app.adapters.network import doh
from app.adapters.system.fsproxy import FileSystemProxy
from app.agent.llm.capability import OpenAIChatAudioProvider
from app.agent.llm.helper import LLMHelper
from app.agent.mcp import _SseMcpSession
from app.agent.tools.impl.apply_patch import ApplyPatchTool
from app.agent.tools.impl.edit_file import EditFileTool
from app.agent.tools.impl.write_file import WriteFileTool
from app.chain.anilist import AniListChain
from app.modules.bangumi import BangumiModule
from app.schemas.agent import AgentMcpServerConfig


@pytest.mark.parametrize(("answer", "expected"), [
    (None, None), ([], None), ([None], None), ([{"type": 1, "data": 123}], None),
    ([{"data": "203.0.113.7"}], None),
    ([{"type": 1, "data": "203.0.113.7"}], "203.0.113.7"),
])
def test_doh_json_only_accepts_text_address(answer, expected, monkeypatch):
    """
    DNS JSON 缺少答案、没有 A 记录或返回错误类型时视为未解析，合法 A 记录地址原样返回。
    """
    response = MagicMock(status=200)
    response.read.return_value = json.dumps({"Answer": answer}).encode()
    response.__enter__.return_value = response
    request = Mock(return_value=response)
    monkeypatch.setattr(doh.urllib.request, "urlopen", request)
    assert doh.doh_query_json("resolver.test", "example.test") == expected
    assert request.call_count == 1


@pytest.mark.parametrize("operation", ["request", "stream", "read"])
def test_fsproxy_without_worker_fails_before_pipe_access(operation, monkeypatch):
    """
    代理没有进程或标准管道时报告通信错误，不能把缺失资源当作成功结果。
    """
    proxy = FileSystemProxy()
    monkeypatch.setattr(proxy, "_ensure_worker", Mock())
    with pytest.raises(BrokenPipeError, match="进程未启动"):
        if operation == "request":
            proxy._request("stat", {"path": "/unused"})
        elif operation == "stream":
            proxy._request_stream({}, None, None)
        else:
            proxy._read_line()
    proxy.close()


def test_fsproxy_worker_without_pipe_is_closed(monkeypatch):
    """
    子进程创建成功但管道缺失时立即收尾，不留下不可用的代理进程。
    """
    process = Mock(stdin=None, stdout=None)
    proxy = FileSystemProxy()
    shutdown = Mock()
    monkeypatch.setattr(proxy, "_shutdown", shutdown)
    monkeypatch.setattr("app.adapters.system.fsproxy.subprocess.Popen", Mock(return_value=process))
    with pytest.raises(BrokenPipeError, match="管道创建失败"):
        proxy._ensure_worker()
    assert shutdown.call_count == 2


@pytest.mark.parametrize(("audio", "expected"), [
    (None, None), ({"data": None}, None), ({"data": 123}, None), ({"data": "encoded-audio"}, "encoded-audio"),
])
def test_audio_extra_requires_string_payload(audio, expected):
    """
    供应方扩展字段中的音频仅接受编码字符串，避免非文本载荷流入文件解码。
    """
    message = SimpleNamespace(model_extra={"audio": audio})
    assert OpenAIChatAudioProvider._extract_audio_data(message) == expected


@pytest.mark.parametrize(("payload", "expected"), [
    ({"type": "text", "text": 12}, ""),
    ({"type": "text", "text": "正文"}, "正文"),
    ({"text": "普通正文"}, "普通正文"),
    ([{"type": "text", "text": 12}, {"type": "text", "text": "正文"}], "正文"),
])
def test_llm_text_projection_discards_non_text_content(payload, expected):
    """
    混合响应块中的非字符串 text 不会泄漏为正文或导致拼接失败。
    """
    assert LLMHelper.extract_text_content(payload) == expected


def test_sse_mcp_uninitialized_stream_is_an_explicit_session_error(monkeypatch):
    """
    SSE 会话尚无事件流时不能发起初始化或请求，并保留明确可诊断的错误。
    """
    session = _SseMcpSession(AgentMcpServerConfig(id="sse", name="SSE", transport="sse", url="https://mcp.test"))
    monkeypatch.setattr(session, "_post", AsyncMock())
    with pytest.raises(RuntimeError, match="事件流未初始化"):
        asyncio.run(session._read_endpoint())
    with pytest.raises(RuntimeError, match="事件流未初始化"):
        asyncio.run(session.request("tools/list"))


@pytest.mark.parametrize(("server_url", "expected"), [(None, None), ("https://mcp.test/sse", "https://mcp.test/messages")])
def test_sse_mcp_endpoint_requires_server_url(server_url, expected):
    """
    相对 SSE 投递地址必须绑定配置的服务 URL，缺失时拒绝生成错误地址。
    """
    session = _SseMcpSession(AgentMcpServerConfig(id="sse", name="SSE", transport="sse", url=server_url))

    async def events():
        """
        提供服务端声明的相对消息投递地址。
        """
        yield {"event": "endpoint", "data": "/messages"}

    session._event_iterator = events()
    if expected is None:
        with pytest.raises(RuntimeError, match="缺少 URL"):
            asyncio.run(session._read_endpoint())
    else:
        assert asyncio.run(session._read_endpoint()) == expected


@pytest.mark.parametrize("tool_class", [EditFileTool, WriteFileTool, ApplyPatchTool])
def test_file_tools_reject_missing_resolved_path(tool_class, monkeypatch, tmp_path):
    """
    权限边界未解析出路径时拒绝编辑、写入及补丁，不能落到当前目录。
    """
    tool = tool_class(session_id="missing-path", user_id="admin")
    tool.set_agent_context({"is_admin": True})
    monkeypatch.setattr(tool_class, "_check_local_file_access", AsyncMock(return_value=(None, None)))
    path = tmp_path / "safe.txt"
    if tool_class is EditFileTool:
        result = asyncio.run(tool.run(str(path), "before", "after"))
    elif tool_class is WriteFileTool:
        result = asyncio.run(tool.run(str(path), "after"))
    else:
        result = asyncio.run(tool.run(f"*** Begin Patch\n*** Add File: {path}\n+after\n*** End Patch"))
    assert "无法解析" in result
    assert not path.exists()


def test_apply_patch_without_plan_never_writes(monkeypatch, tmp_path):
    """
    补丁规划未产生结果时整体失败，不尝试执行部分文件操作。
    """
    tool = ApplyPatchTool(session_id="missing-plan", user_id="admin")
    monkeypatch.setattr(ApplyPatchTool, "_plan_operations", AsyncMock(return_value=(None, None)))
    path = tmp_path / "safe.txt"
    result = asyncio.run(tool.run(f"*** Begin Patch\n*** Add File: {path}\n+after\n*** End Patch"))
    assert result == "错误：补丁规划失败"
    assert not path.exists()


@pytest.mark.parametrize("result", [None, [], {"id": 1}])
def test_anilist_detail_requires_object_response(result, monkeypatch):
    """
    动态模块详情只接受字典，错误列表与空响应不会被投影成媒体详情。
    """
    chain = AniListChain()
    monkeypatch.setattr(chain, "run_module", Mock(return_value=result))
    monkeypatch.setattr(chain, "async_run_module", AsyncMock(return_value=result))
    expected = result if isinstance(result, dict) else None
    assert chain.info(1) == expected
    assert asyncio.run(chain.async_info(1)) == expected


def test_bangumi_uninitialized_dependencies_report_lifecycle_error(monkeypatch):
    """
    未激活的 Bangumi 客户端与刮削器返回生命周期错误，不触发外部调用。
    """
    module = object.__new__(BangumiModule)
    monkeypatch.setattr(module, "bangumiapi", None)
    monkeypatch.setattr(module, "scraper", None)
    with pytest.raises(RuntimeError, match="尚未初始化"):
        module._require_api()
    with pytest.raises(RuntimeError, match="尚未初始化"):
        module._require_scraper()
