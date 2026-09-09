"""真实模型评测的独立进程边界，业务调用仍只进入内存假世界。"""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from contextlib import redirect_stdout
from dataclasses import asdict
from importlib.metadata import version
from typing import Any

from scripts.evaluation.models import ModelSettings, ModelUsageTracker


def _worker_environment() -> dict[str, str]:
    """只传递解释器所需平台环境；业务设置、代理和其他服务凭据不进入评测。"""
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "LANG", "TMPDIR", "TMP", "TEMP"}
    environment = {name: value for name, value in os.environ.items() if name in allowed or name.startswith("LC_")}
    environment.update(PYTHONNOUSERSITE="1", PYTHONUTF8="1")
    return environment


def _validate_report(report: Any, credential: str) -> dict[str, Any]:
    """对解码后的键和值检查凭据，JSON 转义不能绕过输出边界。"""
    if not isinstance(report, dict):
        raise RuntimeError("隔离模型评测报告未通过输出校验")
    pending = [report]
    while pending:
        value = pending.pop()
        if isinstance(value, str) and credential in value:
            raise RuntimeError("评测报告意外包含连接凭据，已拒绝输出")
        if isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, (list, tuple)):
            pending.extend(value)
    return report


def run_live(scenario_id: str, settings: ModelSettings) -> dict[str, Any]:
    """经标准输入传递连接凭据，独立进程避免复用调用方已经加载的真实配置。"""
    payload = json.dumps({"scenario_id": scenario_id, "settings": asdict(settings)})
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "scripts.evaluation.live", "--worker"],
            input=payload, text=True, capture_output=True, check=False, env=_worker_environment(),
            timeout=settings.timeout_seconds + 30,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("隔离模型评测进程超时，已终止") from None
    if completed.returncode != 0:
        # 不回显 worker stderr，SDK 或宿主初始化错误可能含用户连接信息。
        raise RuntimeError(f"隔离模型评测进程退出，状态码 {completed.returncode}")
    try:
        report = json.loads(completed.stdout)
    except (ValueError, TypeError):
        raise RuntimeError("隔离模型评测未返回有效报告") from None
    return _validate_report(report, settings.api_key)


def _parse_final(text: str) -> Any:
    """接受普通 JSON 或单个 Markdown JSON 围栏，其他话术不作为结构化完成证据。"""
    value = text.strip()
    if value.startswith(("```\n", "```json\n")) and value.endswith("```") and value.count("```") == 2:
        value = value.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return None


def _trace_metrics(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """补充业务账本之外的工具调用；压缩后仅统计保留的父图消息，不冒充全部子图历史。"""
    requests = sum(len(message["data"].get("tool_calls", [])) for message in messages if message["type"] == "ai")
    results = [message["data"] for message in messages if message["type"] == "tool"]
    return {"scope": "retained_parent_graph", "requested": requests, "results": len(results),
            "errors": sum(result.get("status") == "error" for result in results)}


async def _run_worker(scenario_id: str, settings: ModelSettings) -> dict[str, Any]:
    """创建临时宿主环境，直到此处才导入业务运行时和建立真实模型连接。"""
    if "app.runtime.config" in sys.modules:
        raise RuntimeError("真实评测 worker 必须在业务导入前隔离配置")
    with tempfile.TemporaryDirectory(prefix="moviepilot-model-evaluation-") as config_dir:
        os.environ["CONFIG_DIR"] = config_dir
        from app.testing.bootstrap import prepare_backend

        prepare_backend()
        from langchain_openai import ChatOpenAI

        from scripts.evaluation.__main__ import _provenance
        from scripts.evaluation.runtime import run_moviepilot
        from scripts.evaluation.score import evaluate
        from scripts.evaluation.world import EvaluationWorld

        tracker = ModelUsageTracker(settings.max_model_calls)
        model = ChatOpenAI(
            model=settings.model, api_key=settings.api_key, base_url=settings.base_url,
            use_responses_api=True, reasoning={"effort": settings.reasoning_effort},
            max_tokens=settings.max_output_tokens, max_retries=0, timeout=min(120, settings.timeout_seconds),
            profile={"max_input_tokens": settings.context_window}, callbacks=[tracker],
        )
        world = EvaluationWorld(scenario_id)
        started = time.monotonic()
        capture: dict[str, Any] = {}
        failure = None
        cleanup_errors = []
        try:
            capture = await asyncio.wait_for(
                run_moviepilot(world, model, model_name=settings.model, context_window=settings.context_window,
                               max_iterations=64 + settings.max_model_calls * 16),
                timeout=settings.timeout_seconds,
            )
        except Exception as error:
            failure = type(error).__name__
        finally:
            try:
                await model.root_async_client.close()
            except Exception as error:
                cleanup_errors.append(type(error).__name__)
            try:
                model.root_client.close()
            except Exception as error:
                cleanup_errors.append(type(error).__name__)
        final_text = str(capture.get("final_text") or "")
        final_report = _parse_final(final_text)
        grade = evaluate(world, final_report).to_dict()
        usage = tracker.snapshot()
        # 首次 HTTP 被拒绝不属于模型能力证据；有响应也仍不构成 Codex 配对比较。
        return {
            **grade, **_provenance(world), **usage, "evidence_kind": "moviepilot_live_model",
            "intelligence_evaluated": usage["completed_model_calls"] > 0, "codex_comparison": False,
            "model": settings.public_metadata(), "elapsed_seconds": round(time.monotonic() - started, 3),
            "runtime_versions": {"python": sys.version.split()[0], **{
                name: version(name) for name in ("langchain", "langgraph", "langchain-openai", "openai")
            }},
            "runner_error_type": failure, "cleanup_error_types": cleanup_errors,
            "final_text": final_text, "final_report": final_report,
            "tool_catalog_scope": capture.get("tool_catalog_scope"),
            "tool_names": capture.get("tool_names"), "graph_nodes": capture.get("graph_nodes"),
            "agent_execution_success": capture.get("execution_success"),
            "agent_trace": capture.get("raw_messages", []), "task_plan": capture.get("task_plan"),
            "trace_tool_metrics": _trace_metrics(capture.get("raw_messages", [])),
            "ledger": world.ledger,
        }


def main() -> int:
    """worker 只从私有 stdin 接收配置，stdout 只输出无凭据评测报告。"""
    if sys.argv[1:] != ["--worker"]:
        raise SystemExit("请通过 python -m scripts.evaluation --live 启动")
    payload = json.loads(sys.stdin.read(256 * 1024))
    settings = ModelSettings(**payload["settings"])
    with redirect_stdout(sys.stderr):
        result = asyncio.run(_run_worker(payload["scenario_id"], settings))
    serialized = json.dumps(_validate_report(result, settings.api_key), ensure_ascii=False)
    print(serialized)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
