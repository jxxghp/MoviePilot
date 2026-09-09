"""真实模型评测的凭据边界、调用限额及用量可信度。"""

import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from pydantic import Field

from scripts.evaluation.models import ModelCallLimitError, ModelSettings, ModelUsageTracker, load_codex_model_settings


def _config(tmp_path, credential='experimental_bearer_token = "private-test-key"'):
    """创建仅用于测试的用户明确配置，不读取任何真实凭据。"""
    path = tmp_path / "config.toml"
    path.write_text('model = "test-model"\nmodel_provider = "selected"\nmodel_reasoning_effort = "high"\n'
                    '[model_providers.selected]\nbase_url = "https://model.invalid/v1"\nwire_api = "responses"\n'
                    + credential + '\n', encoding="utf-8")
    return path


def test_model_settings_do_not_expose_explicit_credential(tmp_path):
    """凭据只用于连接，repr 和用户可见元数据不得包含它。"""
    settings = load_codex_model_settings(_config(tmp_path))
    assert settings.api_key == "private-test-key"
    assert "private-test-key" not in repr(settings)
    assert "private-test-key" not in json.dumps(settings.public_metadata())
    assert settings.model == "test-model"


def test_only_named_provider_environment_key_is_used(tmp_path, monkeypatch):
    """仅使用配置明确指定的环境变量，不回退到其他账户或服务的凭据。"""
    path = _config(tmp_path, 'env_key = "MOVIEPILOT_EVALUATION_TEST_KEY"')
    monkeypatch.setenv("MOVIEPILOT_EVALUATION_TEST_KEY", "named-key")
    assert load_codex_model_settings(path).api_key == "named-key"
    monkeypatch.delenv("MOVIEPILOT_EVALUATION_TEST_KEY")
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-key")
    with pytest.raises(ValueError, match="显式评测凭据"):
        load_codex_model_settings(path)


@pytest.mark.parametrize("budget", [0, -1, 65])
def test_invalid_call_budget_is_rejected_before_live_request(tmp_path, budget):
    """无效预算不能退化成无限调用。"""
    with pytest.raises(ValueError, match="预算"):
        load_codex_model_settings(_config(tmp_path), max_model_calls=budget)


def test_parallel_model_calls_cannot_exceed_shared_limit():
    """父模型和多个子模型并发开始时，也必须共享同一个硬限额。"""
    tracker = ModelUsageTracker(3)

    def start():
        """模拟一次尚未发出 HTTP 的模型调用起点。"""
        try:
            tracker.on_chat_model_start({}, [], run_id=uuid4())
        except ModelCallLimitError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: start(), range(8)))
    assert sum(results) == 3
    assert tracker.snapshot()["model_calls"] == 3
    assert tracker.snapshot()["blocked_model_calls"] == 5
    assert tracker.raise_error is True


def test_usage_includes_all_calls_and_marks_unknown_usage():
    """失败或没有 usage 的请求不能被报告成完整的零成本运行。"""
    tracker = ModelUsageTracker(4)
    first, second = uuid4(), uuid4()
    tracker.on_chat_model_start({}, [], run_id=first)
    tracker.on_chat_model_start({}, [], run_id=second)
    usage = {"input_tokens": 12, "output_tokens": 8, "total_tokens": 20}
    response = SimpleNamespace(generations=[[SimpleNamespace(message=SimpleNamespace(
        usage_metadata=usage, response_metadata={"model_name": "reported-model"},
    ))]])
    tracker.on_llm_end(response, run_id=first)
    usage["total_tokens"] = 999
    tracker.on_llm_error(RuntimeError("private error text"), run_id=second)
    snapshot = tracker.snapshot()
    assert snapshot["model_calls"] == 2
    assert snapshot["tokens"]["total_tokens"] == 20
    assert snapshot["usage_complete"] is False
    assert snapshot["reported_models"] == ["reported-model"]
    assert "private error text" not in json.dumps(snapshot)


def test_empty_or_partial_usage_does_not_break_response_handling():
    """统计器不能因不完整响应而打断本来可继续处理的模型结果。"""
    tracker = ModelUsageTracker(2)
    run_id = uuid4()
    tracker.on_chat_model_start({}, [], run_id=run_id)
    tracker.on_llm_end(SimpleNamespace(generations=[]), run_id=run_id)
    snapshot = tracker.snapshot()
    assert snapshot["completed_model_calls"] == 1
    assert snapshot["tokens"] is None
    assert snapshot["usage_complete"] is False


@pytest.mark.parametrize(("field", "value"), [
    ("model", ""), ("model", "  "), ("model", 42),
    ("base_url", "file:///tmp/model"), ("base_url", "https:///missing-host"),
    ("base_url", "https://user:password@model.invalid/v1"),
    ("base_url", "https://model.invalid/v1?api_key=private-test-key"),
    ("base_url", "https://model.invalid/v1#private-test-key"),
    ("api_key", ""), ("api_key", "  "), ("api_key", "private\ntest"), ("api_key", False),
    ("reasoning_effort", "unbounded"), ("max_model_calls", 0), ("max_model_calls", 65),
    ("max_model_calls", True), ("max_model_calls", 2.5),
    ("max_output_tokens", 255), ("max_output_tokens", 32769), ("max_output_tokens", True),
    ("timeout_seconds", 29), ("timeout_seconds", 901), ("timeout_seconds", False),
    ("context_window", 0), ("context_window", -1), ("context_window", True),
])
def test_direct_settings_reject_invalid_worker_input(field, value):
    """stdin 直接构造设置也必须执行同一校验，布尔值不能冒充整数预算。"""
    values = {"model": "test-model", "base_url": "https://model.invalid/v1", "api_key": "private-test-key"}
    values[field] = value
    with pytest.raises(ValueError):
        ModelSettings(**values)


@pytest.mark.parametrize("credential", [
    'experimental_bearer_token = ""', 'experimental_bearer_token = "  "',
    'experimental_bearer_token = "line\\nfeed"', 'experimental_bearer_token = false',
])
def test_invalid_explicit_credential_does_not_fallback_to_other_provider(tmp_path, monkeypatch, credential):
    """无效的选中凭据应明确失败，不能改用环境内其他账户的密钥。"""
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-account-secret")
    with pytest.raises(ValueError) as failure:
        load_codex_model_settings(_config(tmp_path, credential))
    assert "unrelated-account-secret" not in str(failure.value)


def test_duplicate_callbacks_preserve_single_usage_record():
    """相同 run_id 的重复开始与完成通知不能重复耗用预算或累计 token。"""
    tracker = ModelUsageTracker(1)
    run_id = uuid4()
    tracker.on_chat_model_start({}, [], run_id=run_id)
    tracker.on_chat_model_start({}, [], run_id=run_id)
    response = SimpleNamespace(generations=[[SimpleNamespace(message=SimpleNamespace(
        usage_metadata={"input_tokens": 12, "output_tokens": 8, "total_tokens": 20}, response_metadata={},
    ))]])
    tracker.on_llm_end(response, run_id=run_id)
    tracker.on_llm_end(response, run_id=run_id)
    snapshot = tracker.snapshot()
    assert snapshot["model_calls"] == snapshot["completed_model_calls"] == 1
    assert snapshot["tokens"]["total_tokens"] == 20
    assert snapshot["blocked_model_calls"] == 0


@pytest.mark.parametrize("usage", [
    {}, {"input_tokens": 10}, {"input_tokens": True, "output_tokens": 2, "total_tokens": 3},
    {"input_tokens": -1, "output_tokens": 2, "total_tokens": 1},
    {"input_tokens": 1.5, "output_tokens": 2, "total_tokens": 3.5},
])
def test_invalid_usage_remains_unknown(usage):
    """不完整或类型错误的 usage 不能出现在完整成本报告里。"""
    tracker = ModelUsageTracker(1)
    run_id = uuid4()
    tracker.on_chat_model_start({}, [], run_id=run_id)
    tracker.on_llm_end(SimpleNamespace(generations=[[SimpleNamespace(message=SimpleNamespace(
        usage_metadata=usage, response_metadata={},
    ))]]), run_id=run_id)
    assert tracker.snapshot()["tokens"] is None
    assert tracker.snapshot()["usage_complete"] is False


class _CountingModel(FakeMessagesListChatModel):
    """在实际 LangChain 调用中验证 callback 限额确实早于模型执行。"""

    executed: int = Field(default=0)

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        """记录真正到达模型实现的请求数，不模拟 callback 调度。"""
        self.executed += 1
        return await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)


@pytest.mark.asyncio
async def test_sdk_call_limit_prevents_next_model_execution():
    """SDK 必须传播限额异常，第二次调用不能到达模型实现。"""
    tracker = ModelUsageTracker(1)
    model = _CountingModel(responses=[AIMessage(content="离线结果")], callbacks=[tracker])
    assert (await model.ainvoke("第一次请求")).content == "离线结果"
    with pytest.raises(ModelCallLimitError, match="上限"):
        await model.ainvoke("第二次请求")
    assert model.executed == 1
    assert tracker.snapshot()["model_calls"] == 1
    assert tracker.snapshot()["completed_model_calls"] == 1
    assert tracker.snapshot()["blocked_model_calls"] == 1


def test_failed_request_records_type_status_without_private_error():
    """HTTP 失败可诊断为类型和状态码，私有请求内容不能进入公开统计。"""
    tracker = ModelUsageTracker(1)
    run_id = uuid4()
    tracker.on_chat_model_start({}, [], run_id=run_id)
    error = RuntimeError("Bearer private-test-key; https://private-host.invalid/secret-path")
    error.status_code = 400
    tracker.on_llm_error(error, run_id=run_id)
    snapshot = tracker.snapshot()
    assert snapshot["completed_model_calls"] == 0
    assert snapshot["errors"] == [{"error_type": "RuntimeError", "http_status": 400}]
    assert snapshot["tokens"] is None
    assert "private" not in json.dumps(snapshot)
