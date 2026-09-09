"""分别提供离线轨迹回放与显式真实模型评测；业务操作只进入假世界。"""

import argparse
import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from scripts.evaluation.scenarios import list_scenarios
from scripts.evaluation.score import evaluate
from scripts.evaluation.world import EvaluationWorld

MAX_REPLAY_BYTES = 256 * 1024
MAX_REPLAY_CALLS = 32
_CALL_FIELDS = frozenset({"operation_id", "path_params", "query", "body"})


def _source_digest(root: Path) -> str:
    """按路径和内容记录生产运行时或内置技能，不把源码正文送进模型或报告。"""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def _provenance(world: EvaluationWorld) -> dict[str, Any]:
    """记录实际场景与判定代码指纹，避免未提交或修改后的轨迹被混为同一基线。"""
    fixture = json.dumps({"scenario": asdict(world.scenario), "initial": world.initial_snapshot()},
                         ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob("*.py")):
        digest.update(path.name.encode("utf-8") + b"\0" + path.read_bytes() + b"\0")
    repository = Path(__file__).resolve().parents[2]
    return {"schema_version": 1, "scenario_sha256": hashlib.sha256(fixture.encode("utf-8")).hexdigest(),
            "production_agent_sha256": _source_digest(repository / "app" / "agent"),
            "skills_sha256": _source_digest(repository / "skills"),
            "harness_sha256": digest.hexdigest(), "model": None, "model_calls": 0, "tokens": None}


def replay(scenario_id: str, payload: Any) -> dict[str, Any]:
    """只接受调用与最终报告，轨迹不能提供初态、账本或预期答案。"""
    if not isinstance(payload, dict) or set(payload) != {"calls", "final"}:
        raise ValueError("回放必须且只能包含 calls 与 final")
    calls = payload["calls"]
    if not isinstance(calls, list) or len(calls) > MAX_REPLAY_CALLS:
        raise ValueError(f"calls 必须为不超过 {MAX_REPLAY_CALLS} 项的数组")
    for call in calls:
        if not isinstance(call, dict) or set(call) - _CALL_FIELDS or not isinstance(call.get("operation_id"), str):
            raise ValueError("调用只接受 operation_id/path_params/query/body")
    world = EvaluationWorld(scenario_id)
    for call in calls:
        world.execute(**call)
    return {**evaluate(world, payload["final"]).to_dict(), **_provenance(world)}


def main(argv: list[str] | None = None) -> int:
    """显式区分回放与真实调用，并以非零退出码表达验收未通过。"""
    parser = argparse.ArgumentParser(description="MoviePilot Agent 隔离任务评测；--live/--native 才会调用真实模型")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--list", action="store_true", help="列出公开场景，不输出初态或答案")
    mode.add_argument("--replay", type=Path, help="包含 calls 与 final 的 JSON 轨迹")
    mode.add_argument("--live", action="store_true", help="显式调用真实模型，业务只进入隔离假世界")
    mode.add_argument("--native", action="store_true", help="用原生 Codex CLI 操作同一假世界并调用真实模型")
    mode.add_argument("--native-probe", action="store_true", help="核对原生 Codex 配置和工具目录，不调用真实模型")
    parser.add_argument("--codex-executable", default="codex", help="原生模式使用的 Codex CLI 可执行文件")
    parser.add_argument("--scenario", choices=[item.scenario_id for item in list_scenarios()])
    parser.add_argument("--output", type=Path, help="可选 JSON 报告路径")
    parser.add_argument("--codex-config", type=Path, default=Path.home() / ".codex" / "config.toml")
    parser.add_argument("--model", help="真实评测的模型名称，默认沿用显式 Codex provider 配置")
    parser.add_argument("--reasoning-effort", help="真实评测推理预算，默认沿用配置")
    parser.add_argument("--max-model-calls", type=int, default=12)
    parser.add_argument("--max-output-tokens", type=int, default=8192)
    parser.add_argument("--timeout-seconds", type=int, default=180)
    args = parser.parse_args(argv)
    if args.list:
        print(json.dumps([{"id": item.scenario_id, "task": item.model_input()} for item in list_scenarios()], ensure_ascii=False, indent=2))
        return 0
    if args.scenario is None:
        parser.error("评测需要 --scenario")
    try:
        if args.live or args.native or args.native_probe:
            from scripts.evaluation.models import load_codex_model_settings

            settings = load_codex_model_settings(
                args.codex_config, model=args.model, reasoning_effort=args.reasoning_effort,
                max_model_calls=args.max_model_calls, max_output_tokens=args.max_output_tokens,
                timeout_seconds=args.timeout_seconds,
            )
            if args.live:
                from scripts.evaluation.live import run_live

                result = run_live(args.scenario, settings)
            else:
                from scripts.evaluation.codex import run_codex

                result = run_codex(args.scenario, settings, executable=args.codex_executable, probe_only=args.native_probe)
        else:
            if args.replay.stat().st_size > MAX_REPLAY_BYTES:
                raise ValueError("回放文件超过 256 KiB")
            payload = json.loads(args.replay.read_text(encoding="utf-8"))
            result = replay(args.scenario, payload)
    except (OSError, UnicodeError, ValueError, TypeError, RuntimeError) as error:
        parser.error(str(error))
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output is not None:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if (result.get("probe_ready") if args.native_probe else result["passed"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
