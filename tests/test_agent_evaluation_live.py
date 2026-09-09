"""离线验证真实模型评测入口的证据标记、进程隔离与报告保密边界。"""

import asyncio
import io
import json
import os
import socket
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import langchain_openai
import pytest

from app.testing import bootstrap
from scripts.evaluation import __main__ as evaluation_cli
from scripts.evaluation import live, runtime
from scripts.evaluation.models import ModelSettings
from scripts.evaluation.world import EvaluationWorld


@pytest.fixture(autouse=True)
def no_external_requests(monkeypatch):
    """本文件只测试模型入口边界，禁止任何测试意外访问真实供应商。"""
    def refuse(*_args, **_kwargs):
        """网络解析发生即说明离线边界被绕过。"""
        raise AssertionError("评测入口单测禁止真实网络")

    monkeypatch.setattr(socket, "getaddrinfo", refuse)


@pytest.fixture
def model_settings():
    """提供公开虚构主机和一次性测试密钥，不读取用户文件。"""
    return ModelSettings(model="test-model", base_url="https://model.invalid/private-path", api_key="private-test-key")


def test_run_live_passes_credentials_only_on_stdin(monkeypatch, model_settings):
    """模型连接不出现在命令行或继承环境里，业务设置也不能传入新 worker。"""
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-openai-key")
    monkeypatch.setenv("LLM_API_KEY", "unrelated-moviepilot-key")
    monkeypatch.setenv("TMDB_API_KEY", "unrelated-business-key")
    monkeypatch.setenv("HTTP_PROXY", "http://user:proxy-secret@proxy.invalid")
    called = {}

    def start_worker(command, **kwargs):
        """代替进程启动并检查控制器传递的精确私有输入。"""
        called.update(command=command, **kwargs)
        return SimpleNamespace(returncode=0, stdout='{"passed": false}', stderr="private-worker-log")

    monkeypatch.setattr(live.subprocess, "run", start_worker)
    assert live.run_live("unknown_download", model_settings) == {"passed": False}
    assert called["command"] == [sys.executable, "-m", "scripts.evaluation.live", "--worker"]
    assert called["timeout"] == model_settings.timeout_seconds + 30
    assert called["capture_output"] is True
    assert called["check"] is False
    payload = json.loads(called["input"])
    assert payload["settings"]["api_key"] == model_settings.api_key
    assert payload["scenario_id"] == "unknown_download"
    assert model_settings.api_key not in repr(called["command"])
    assert called["env"].get("PATH") == os.environ.get("PATH")
    assert not {"OPENAI_API_KEY", "LLM_API_KEY", "TMDB_API_KEY", "HTTP_PROXY", "CONFIG_DIR"} & set(called["env"])
    assert "secret" not in json.dumps(called["env"])


def test_worker_timeout_has_safe_public_error(monkeypatch, model_settings):
    """子进程超时包装为固定错误，不暴露已捕获的 stdout、stderr 或凭据。"""
    def time_out(command, **kwargs):
        """模拟 subprocess 已完成终止回收但携带私有输出的超时异常。"""
        raise subprocess.TimeoutExpired(command, kwargs["timeout"], output=model_settings.api_key, stderr="private-stderr")

    monkeypatch.setattr(live.subprocess, "run", time_out)
    with pytest.raises(RuntimeError) as failure:
        live.run_live("unknown_download", model_settings)
    assert model_settings.api_key not in str(failure.value)
    assert "private-stderr" not in str(failure.value)


def test_worker_nonzero_exit_does_not_publish_private_logs(monkeypatch, model_settings):
    """SDK 初始化报错可含私有响应，父进程只报告退出状态。"""
    monkeypatch.setattr(live.subprocess, "run", Mock(return_value=SimpleNamespace(
        returncode=7, stdout=model_settings.api_key, stderr=f"Bearer {model_settings.api_key}",
    )))
    with pytest.raises(RuntimeError, match="7") as failure:
        live.run_live("unknown_download", model_settings)
    assert model_settings.api_key not in str(failure.value)


@pytest.mark.parametrize(("text", "expected"), [
    ('{"status":"completed"}', {"status": "completed"}),
    ('```json\n{"status":"completed"}\n```', {"status": "completed"}),
    ('```\n{"status":"blocked"}\n```', {"status": "blocked"}),
    ('```xml\n{"status":"completed"}\n```', None),
    ('已完成。\n{"status":"completed"}', None),
    ('```json\n{}\n```\n```json\n{}\n```', None),
    ('{"status":"completed"}\n还可以继续', None),
])
def test_final_report_parser_requires_plain_or_single_json_fence(text, expected):
    """语言标签、附加话术和多个结果不能被悄悄解释成一个完成报告。"""
    assert live._parse_final(text) == expected


def test_trace_metrics_include_rejected_calls_outside_business_ledger():
    """被 transport 拒绝的工具及技能读取都应出现于有明确范围的父图统计。"""
    messages = [
        {"type": "ai", "data": {"tool_calls": [{"name": "read_skill"}, {"name": "moviepilot_api"}]}},
        {"type": "tool", "data": {"name": "read_skill", "status": "success"}},
        {"type": "tool", "data": {"name": "moviepilot_api", "status": "error"}},
    ]
    assert live._trace_metrics(messages) == {"scope": "retained_parent_graph", "requested": 2, "results": 2, "errors": 1}


@pytest.fixture
def worker_boundary(monkeypatch):
    """仅模拟独立 worker 的初始化和模型边界，其世界与判定器使用真实实现。"""
    original_config = os.environ["CONFIG_DIR"]
    monkeypatch.setenv("CONFIG_DIR", original_config)
    monkeypatch.setattr(live, "sys", SimpleNamespace(modules={}, version=sys.version))
    observed = SimpleNamespace(config_dirs=[], model=None, runner=None)

    def prepare():
        """验证隔离必须早于后端初始化，不再次改变当前 pytest 已装配的全局设置。"""
        configured = Path(os.environ["CONFIG_DIR"])
        assert str(configured) != original_config
        assert configured.is_dir()
        observed.config_dirs.append(configured)

    def create_model(**kwargs):
        """保存明确的模型预算并提供可核验的客户端关闭边界。"""
        assert observed.config_dirs
        observed.model = SimpleNamespace(
            parameters=kwargs, root_async_client=SimpleNamespace(close=AsyncMock()),
            root_client=SimpleNamespace(close=Mock()),
        )
        return observed.model

    async def run(world, model, **kwargs):
        """不执行真实模型，由各用例注入回调结果和业务轨迹。"""
        assert observed.runner is not None
        return await observed.runner(world, model, **kwargs)

    monkeypatch.setattr(bootstrap, "prepare_backend", prepare)
    monkeypatch.setattr(langchain_openai, "ChatOpenAI", create_model)
    monkeypatch.setattr(runtime, "run_moviepilot", run)
    return observed


def _record_model_reply(model, *, successful):
    """模拟一次合法回调生命周期，保留真实计数器和成功、失败差别。"""
    tracker = model.parameters["callbacks"][0]
    identifier = uuid4()
    tracker.on_chat_model_start({}, [], run_id=identifier)
    if successful:
        tracker.on_llm_end(SimpleNamespace(generations=[[SimpleNamespace(message=SimpleNamespace(
            usage_metadata={"input_tokens": 30, "output_tokens": 10, "total_tokens": 40},
            response_metadata={"model_name": "provider-reported-model"},
        ))]]), run_id=identifier)
    else:
        error = RuntimeError("Bearer private-test-key")
        error.status_code = 400
        tracker.on_llm_error(error, run_id=identifier)


@pytest.mark.asyncio
async def test_request_rejected_before_reply_is_not_intelligence_evidence(worker_boundary, model_settings):
    """HTTP 400 只证明尝试调用过模型，不能声称已评测模型智能。"""
    async def run(_world, model, **_kwargs):
        """重现真实 pilot 已启动一次请求但未得到任何模型回复的状态。"""
        _record_model_reply(model, successful=False)
        return {"execution_success": False, "final_text": "智能助手执行失败，请稍后重试"}

    worker_boundary.runner = run
    report = await live._run_worker("unknown_download", model_settings)
    assert report["model_calls"] == 1
    assert report["completed_model_calls"] == 0
    assert report["intelligence_evaluated"] is False
    assert report["agent_execution_success"] is False
    assert report["passed"] is False
    assert report["tokens"] is None and report["usage_complete"] is False
    assert report["errors"] == [{"error_type": "RuntimeError", "http_status": 400}]
    assert model_settings.api_key not in json.dumps(report)
    assert not any(path.exists() for path in worker_boundary.config_dirs)
    worker_boundary.model.root_async_client.close.assert_awaited_once()
    worker_boundary.model.root_client.close.assert_called_once()


@pytest.mark.asyncio
async def test_successful_model_reply_still_requires_independent_business_evidence(worker_boundary, model_settings):
    """得到模型答复可以形成模型行为证据，但不能替代世界终态和读取核验。"""
    async def run(_world, model, **_kwargs):
        """没有调用业务工具却虚报已完成，判定器必须拒绝该结果。"""
        _record_model_reply(model, successful=True)
        return {"execution_success": True, "final_text": json.dumps({
            "status": "completed", "completed": ["download"], "unresolved": [],
            "download_ids": ["invented-id"], "subscription_ids": [], "enabled_site_ids": [],
        })}

    worker_boundary.runner = run
    report = await live._run_worker("unknown_download", model_settings)
    assert report["completed_model_calls"] == 1
    assert report["intelligence_evaluated"] is True
    assert report["passed"] is False
    assert "download_not_verified" in report["violations"]
    assert report["codex_comparison"] is False
    assert report["reported_models"] == ["provider-reported-model"]
    assert report["tokens"]["total_tokens"] == 40
    assert report["usage_complete"] is True
    assert worker_boundary.model.parameters["max_retries"] == 0
    assert worker_boundary.model.parameters["max_tokens"] == model_settings.max_output_tokens


@pytest.mark.asyncio
async def test_worker_deadline_closes_clients_and_returns_failure_evidence(monkeypatch, worker_boundary, model_settings):
    """整体超时应记录失败并关闭两个 SDK 客户端，而不是静默变成零调用成功。"""
    observed_timeout = []

    async def time_out(awaitable, *, timeout):
        """验证传递的真实墙钟上限，但不让离线单测实际等待数分钟。"""
        observed_timeout.append(timeout)
        awaitable.close()
        raise asyncio.TimeoutError()

    monkeypatch.setattr(live.asyncio, "wait_for", time_out)
    report = await live._run_worker("unknown_download", model_settings)
    assert observed_timeout == [model_settings.timeout_seconds]
    assert report["runner_error_type"] == "TimeoutError"
    assert report["passed"] is False
    assert report["intelligence_evaluated"] is False
    worker_boundary.model.root_async_client.close.assert_awaited_once()
    worker_boundary.model.root_client.close.assert_called_once()


@pytest.mark.asyncio
async def test_client_cleanup_failure_preserves_report_and_closes_other_client(worker_boundary, model_settings):
    """异步客户端关闭失败不能跳过同步客户端，清理错误也不能抹掉模型失败证据。"""
    async def run(_world, model, **_kwargs):
        """在获得失败回调后触发 SDK 清理故障。"""
        _record_model_reply(model, successful=False)
        model.root_async_client.close.side_effect = RuntimeError("private-test-key")
        return {"execution_success": False, "final_text": "执行失败"}

    worker_boundary.runner = run
    report = await live._run_worker("unknown_download", model_settings)
    assert report["cleanup_error_types"] == ["RuntimeError"]
    assert report["model_calls"] == 1 and report["completed_model_calls"] == 0
    assert report["intelligence_evaluated"] is False
    assert model_settings.api_key not in json.dumps(report)
    worker_boundary.model.root_async_client.close.assert_awaited_once()
    worker_boundary.model.root_client.close.assert_called_once()


@pytest.mark.asyncio
async def test_worker_rejects_preloaded_backend(model_settings):
    """pytest 已加载后端；真正 worker 必须在这种状态下拒绝事后隔离配置。"""
    with pytest.raises(RuntimeError, match="导入前隔离"):
        await live._run_worker("unknown_download", model_settings)


def _worker_stdin(monkeypatch, model_settings):
    """构造私有 stdin 输入，不把测试密钥传入命令行参数。"""
    monkeypatch.setattr(sys, "argv", ["evaluation-worker", "--worker"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
        "scenario_id": "unknown_download", "settings": asdict(model_settings),
    })))


def test_worker_stdout_contains_only_json(monkeypatch, capsys, model_settings):
    """依赖库的普通 stdout 被转移到私有 stderr，公开 stdout 只包含 JSON。"""
    async def run(_scenario_id, _settings):
        """模拟依赖在执行期间打印日志。"""
        print("library diagnostic")
        return {"passed": False, "intelligence_evaluated": False}

    _worker_stdin(monkeypatch, model_settings)
    monkeypatch.setattr(live, "_run_worker", run)
    assert live.main() == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"passed": False, "intelligence_evaluated": False}
    assert "library diagnostic" in captured.err
    assert model_settings.api_key not in captured.out


def test_worker_refuses_report_containing_credential(monkeypatch, capsys, model_settings):
    """模型或异常若把连接密钥带入报告，最后输出边界必须拒绝整份结果。"""
    async def run(_scenario_id, settings):
        """注入带密钥的模型最终文本，验证主进程输出前的最后一道检查。"""
        return {"passed": False, "final_text": f"Bearer {settings.api_key}"}

    _worker_stdin(monkeypatch, model_settings)
    monkeypatch.setattr(live, "_run_worker", run)
    with pytest.raises(RuntimeError, match="凭据"):
        live.main()
    assert capsys.readouterr().out == ""


def test_worker_refuses_json_escaped_credential(monkeypatch, capsys):
    """含引号或反斜杠的密钥经 JSON 转义后仍不能出现在公开报告中。"""
    settings = ModelSettings(model="test-model", base_url="https://model.invalid/v1", api_key='private"test\\key')

    async def run(_scenario_id, model_settings):
        """完整密钥埋入嵌套数据，防线应检查解析后的值。"""
        return {"passed": False, "data": [{"value": f"Bearer {model_settings.api_key}"}]}

    _worker_stdin(monkeypatch, settings)
    monkeypatch.setattr(live, "_run_worker", run)
    with pytest.raises(RuntimeError):
        live.main()
    assert capsys.readouterr().out == ""


def test_parent_refuses_unicode_escaped_credential_report(monkeypatch, model_settings):
    """worker stdout 的 Unicode 转义不能让解析后明文密钥绕过父进程检查。"""
    encoded_key = "".join(f"\\u{ord(char):04x}" for char in model_settings.api_key)
    stdout = '{"passed":false,"data":[{"value":"' + encoded_key + '"}]}'
    assert model_settings.api_key not in stdout
    assert model_settings.api_key in json.loads(stdout)["data"][0]["value"]
    monkeypatch.setattr(live.subprocess, "run", Mock(return_value=SimpleNamespace(returncode=0, stdout=stdout, stderr="")))
    with pytest.raises(RuntimeError) as failure:
        live.run_live("unknown_download", model_settings)
    assert model_settings.api_key not in str(failure.value)


def test_public_cli_reports_worker_failure_without_traceback(monkeypatch, capsys, model_settings):
    """公开 CLI 把隔离进程失败转成简洁错误，不回显连接设置或 Python traceback。"""
    from scripts.evaluation import models

    monkeypatch.setattr(models, "load_codex_model_settings", Mock(return_value=model_settings))
    monkeypatch.setattr(live, "run_live", Mock(side_effect=RuntimeError("隔离模型评测超时")))
    with pytest.raises(SystemExit) as failure:
        evaluation_cli.main(["--live", "--scenario", "unknown_download"])
    assert failure.value.code == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "Traceback" not in output.err
    assert model_settings.api_key not in output.err
    assert "超时" in output.err


def test_provenance_changes_with_production_code_and_skills(monkeypatch, tmp_path):
    """生产 Agent 与技能的变化必须改变独立指纹，不能继续冒充同一 Harness 基线。"""
    main_file = tmp_path / "scripts" / "evaluation" / "__main__.py"
    agent_file = tmp_path / "app" / "agent" / "orchestrator.py"
    skill_file = tmp_path / "skills" / "moviepilot-api" / "SKILL.md"
    for path in (main_file, agent_file, skill_file):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("original\n", encoding="utf-8")
    monkeypatch.setattr(evaluation_cli, "__file__", str(main_file))
    world = EvaluationWorld("dedup_existing")
    initial = evaluation_cli._provenance(world)
    agent_file.write_text("changed agent\n", encoding="utf-8")
    changed_agent = evaluation_cli._provenance(world)
    assert changed_agent["production_agent_sha256"] != initial["production_agent_sha256"]
    assert changed_agent["skills_sha256"] == initial["skills_sha256"]
    assert changed_agent["scenario_sha256"] == initial["scenario_sha256"]
    skill_file.write_text("changed skill\n", encoding="utf-8")
    changed_skill = evaluation_cli._provenance(world)
    assert changed_skill["skills_sha256"] != changed_agent["skills_sha256"]
    assert changed_skill["production_agent_sha256"] == changed_agent["production_agent_sha256"]
    bytecode = agent_file.parent / "__pycache__" / "orchestrator.cpython.pyc"
    bytecode.parent.mkdir()
    bytecode.write_bytes(b"local bytecode")
    assert evaluation_cli._provenance(world) == changed_skill
