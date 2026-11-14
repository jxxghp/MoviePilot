"""
命令行 Agent 正式组合根、持久化和资源收口的隔离进程回归。
"""

import os
import subprocess
import sys
from pathlib import Path

import coverage
import pytest


@pytest.mark.parametrize("failure", ["", "model", "configuration", "cancel"])
def test_cli_agent_owns_real_runtime_and_closes_resources(tmp_path, failure):
    """
    临时 SQLite 和真实 Agent 入口只替换模型图，成功及失败后均释放运行时 owner。
    """
    code = r'''
import asyncio
import json
import os
from types import SimpleNamespace

if os.environ.get("CLI_REVIEW_COVERAGE_FILE"):
    import atexit
    import coverage
    measurement = coverage.Coverage(
        data_file=os.environ["CLI_REVIEW_COVERAGE_FILE"],
        data_suffix=True,
        config_file=os.environ["CLI_REVIEW_COVERAGE_CONFIG"],
        source=["app"],
        branch=True,
    )
    measurement.start()

    def save_coverage():
        """
        子进程退出时保存真实执行轨迹，唯一后缀由父pytest-cov统一合并。
        """
        measurement.stop()
        measurement.save()

    atexit.register(save_coverage)

from app.testing.bootstrap import ensure_sites_stub, isolate_config_dir

isolate_config_dir()
ensure_sites_stub()
import pytest
from app.testing.network import block_real_network
network_patch = pytest.MonkeyPatch()
network_guard = block_real_network.__wrapped__(network_patch)
next(network_guard)
from app.startup.initializers.cache import configure_cache_dependencies
configure_cache_dependencies()

from app.runtime.config import settings
settings.AI_AGENT_ENABLE = True
settings.LLM_PROVIDER = "openai"
settings.LLM_MODEL = "review"
settings.LLM_API_KEY = "review-not-a-secret"

from langchain_core.messages import AIMessage
from app.agent.loader import get_agent_manager, get_running_agent_manager
from app.agent.orchestrator import MoviePilotAgent
from app.application.configuration import get_api_runtime_config_snapshot
from app.application.chain.context import get_chain_runtime_context
from app.application.messaging.chat import get_configured_agent_chat_persistence
from app.db.oper.agentchat import AgentChatOper
from app.db import session as database_session
from app.db.session import SessionFactory
from app.runtime.loop import main_loop_registry
from app.startup.composition import configuration
from app.startup.composition.database import database_runtime_active
from scripts.local_setup import run_agent_request

seen = []
closed = []
failure = os.environ["CLI_REVIEW_FAILURE"]
original_close_database = database_session.close_database

async def observe_database_close():
    """
    在真实引擎释放前读取持久消息，观察后继续调用实际关闭 owner。
    """
    with SessionFactory() as session:
        record = AgentChatOper(session).get("cli-review", "review")
        closed.append(bool(record and record.agent_messages))
    await original_close_database()

database_session.close_database = observe_database_close

async def graph(agent, streaming=False):
    """
    模型边界核对真实组合根身份，并返回确定的 LangChain 消息。
    """
    del streaming
    manager = get_running_agent_manager()
    seen.append({
        "data": agent._data is not None,
        "canonical": manager is get_agent_manager(),
        "memory": manager is not None and agent._memory is manager._memory,
        "worker": database_runtime_active(),
        "configuration": get_api_runtime_config_snapshot().ai_agent_enable,
        "persistence": agent._data is not None and agent._data.chat_persistence is get_configured_agent_chat_persistence(),
        "loop": main_loop_registry.require() is asyncio.get_running_loop(),
        "chain": get_chain_runtime_context().configuration.ai_agent_enable,
    })
    agent._last_agent_cache_hit = False
    state = {}

    async def invoke(payload, config):
        del config
        if failure == "model":
            raise RuntimeError("review model failure")
        if failure == "cancel":
            raise asyncio.CancelledError()
        if os.environ.get("CLI_REVIEW_HISTORY"):
            assert len(payload["messages"]) >= 3, payload
        state["messages"] = [*payload["messages"], AIMessage(content="review answer")]
        return state

    return SimpleNamespace(ainvoke=invoke, get_state=lambda _config: SimpleNamespace(values=state))

async def no_title(_self, _message):
    """
    标题生成同样属于模型边界，本回归只验证主对话。
    """

MoviePilotAgent._create_agent = graph
MoviePilotAgent.prepare_chat_title = no_title
MoviePilotAgent._should_stream = lambda _self: False
MoviePilotAgent._learning_enabled = lambda _self: False
if failure == "configuration":
    async def fail_configuration(**_kwargs):
        raise RuntimeError("review configuration failure")
    configuration.compose_configuration = fail_configuration

try:
    result = run_agent_request(message=" review prompt ", session_id="cli-review", new_session=False, user_id="review")
except RuntimeError as error:
    assert failure == "configuration" and "review configuration failure" in str(error), str(error)
except asyncio.CancelledError:
    assert failure == "cancel"
else:
    assert failure != "configuration", "正式配置组合根没有执行"
    assert len(seen) == 1 and all(seen[0].values()), seen
    if not failure:
        assert result == {"session_id":"cli-review", "result":"review answer"}, result
        assert closed == [True], "CLI 对话未提交到临时 SQLite"
    else:
        assert result["result"], result

assert len(closed) == 1, "引擎关闭 owner 没有执行"
assert not database_runtime_active(), "数据库 worker 未关闭"
assert get_running_agent_manager() is None, "Agent 能力仍处于运行态"
assert main_loop_registry.current is None, "CLI 主循环登记未释放"
if seen:
    from app.agent.manager import agent_manager
    assert not agent_manager._accepting_tasks and not agent_manager.active_agents
    assert agent_manager._idle_cleanup_task is None
    assert agent_manager._memory.cleanup_task is None
try:
    next(network_guard)
except StopIteration:
    pass
network_patch.undo()
print("CLI_REVIEW_OK")
'''
    environment = {**os.environ, "CONFIG_DIR": str(tmp_path / "config"), "CLI_REVIEW_FAILURE": failure}
    environment.pop("DEBUG", None)
    active_coverage = coverage.Coverage.current()
    if active_coverage is not None:
        environment["CLI_REVIEW_COVERAGE_FILE"] = active_coverage.get_data().data_filename()
        environment["CLI_REVIEW_COVERAGE_CONFIG"] = str(Path(__file__).parents[1] / ".coveragerc")
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parents[1],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "CLI_REVIEW_OK" in completed.stdout
    if not failure:
        replay = subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path(__file__).parents[1],
            env={**environment, "CLI_REVIEW_HISTORY": "1"},
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert replay.returncode == 0, replay.stdout + replay.stderr
        assert "CLI_REVIEW_OK" in replay.stdout
