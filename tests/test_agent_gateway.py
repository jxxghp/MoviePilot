"""
S4 / P2 回归:agent_gateway seam 打断 chain↔agent import-time 循环。

本地 venv 中 `import app.agent` 会拉起 langchain 并触发预存的 jieba_next 缺失,
故 lazy-default 路径(真实单例)无法在此直接 import 验证;改以
(a) provider 注入路径 + (b) 源码结构契约 两类断言覆盖。
"""
from pathlib import Path

import pytest

from app.core import agent_gateway

APP_DIR = Path(__file__).resolve().parent.parent / "app"


@pytest.fixture(autouse=True)
def _reset_providers():
    """每个用例后清空所有 provider,避免全局态泄漏到其它测试。"""
    yield
    agent_gateway.set_agent_manager_provider(None)
    agent_gateway.set_prompt_manager_provider(None)
    agent_gateway.set_agent_llm_provider(None)
    agent_gateway.set_agent_capability_provider(None)


# ---- (a) provider 注入路径:有 provider 时返回注入对象,不触发真实 agent import ----

def test_get_agent_manager_returns_injected_provider():
    sentinel = object()
    agent_gateway.set_agent_manager_provider(lambda: sentinel)
    assert agent_gateway.get_agent_manager() is sentinel


def test_get_prompt_manager_returns_injected_provider():
    sentinel = object()
    agent_gateway.set_prompt_manager_provider(lambda: sentinel)
    assert agent_gateway.get_prompt_manager() is sentinel


def test_get_agent_llm_returns_injected_provider():
    sentinel = object()
    agent_gateway.set_agent_llm_provider(lambda: sentinel)
    assert agent_gateway.get_agent_llm() is sentinel


def test_get_agent_capability_returns_injected_provider():
    sentinel = object()
    agent_gateway.set_agent_capability_provider(lambda: sentinel)
    assert agent_gateway.get_agent_capability() is sentinel


def test_providers_independent():
    """注入其一不影响其它访问器(仍为各自默认/None 判定)。"""
    sentinel = object()
    agent_gateway.set_prompt_manager_provider(lambda: sentinel)
    assert agent_gateway.get_prompt_manager() is sentinel
    # 其它三个未注入 —— 仍为 None provider(此处只断言访问器互不串扰)
    assert agent_gateway._agent_manager_provider is None
    assert agent_gateway._agent_llm_provider is None
    assert agent_gateway._agent_capability_provider is None


# ---- (b) 源码结构契约:import-time 循环已断 ----

def test_gateway_has_no_top_level_agent_import():
    """网关自身对 app.agent 的引用必须全部在函数体内(惰性),顶层零引用。"""
    src = (APP_DIR / "core" / "agent_gateway.py").read_text(encoding="utf-8")
    for line in src.splitlines():
        # 顶层 import 必然顶格;函数内惰性 import 有缩进
        assert not line.startswith("from app.agent"), f"网关顶层不应直接 import agent: {line}"
        assert not line.startswith("import app.agent"), f"网关顶层不应直接 import agent: {line}"


@pytest.mark.parametrize("rel", ["chain/transfer.py", "chain/message.py", "chain/search.py"])
def test_chain_has_no_agent_import(rel):
    """chain 三个回边文件不得再以任何形式 import app.agent(顶层或惰性均已改走网关)。"""
    src = (APP_DIR / rel).read_text(encoding="utf-8")
    offenders = [ln.strip() for ln in src.splitlines() if "from app.agent" in ln]
    assert not offenders, f"{rel} 仍残留 chain→agent 直连: {offenders}"


def test_replymode_importable_from_schemas_types():
    from app.schemas.types import ReplyMode
    assert issubclass(ReplyMode, str)
    assert ReplyMode.DISPATCH.value == "dispatch"
    assert ReplyMode.CAPTURE_ONLY.value == "capture_only"


def test_agent_init_reexports_replymode():
    """agent/__init__.py 仍以 shim 顶层 re-export ReplyMode,保 `from app.agent import ReplyMode` 兼容。"""
    src = (APP_DIR / "agent" / "__init__.py").read_text(encoding="utf-8")
    assert "from app.schemas.types import" in src and "ReplyMode" in src
    # 旧的本地定义必须已移除,避免双源
    assert "class ReplyMode" not in src
