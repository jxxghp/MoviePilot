"""离线回放评测轨迹；此入口不会调用模型、真实 MoviePilot 或外部服务。"""

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


def _provenance(world: EvaluationWorld) -> dict[str, Any]:
    """记录实际场景与判定代码指纹，避免未提交或修改后的轨迹被混为同一基线。"""
    fixture = json.dumps({"scenario": asdict(world.scenario), "initial": world.initial_snapshot()},
                         ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob("*.py")):
        digest.update(path.name.encode("utf-8") + b"\0" + path.read_bytes() + b"\0")
    return {"schema_version": 1, "scenario_sha256": hashlib.sha256(fixture.encode("utf-8")).hexdigest(),
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
    """打印公开场景或回放文件，并以非零退出码表达验收未通过。"""
    parser = argparse.ArgumentParser(description="MoviePilot Agent 离线轨迹验收；不代表真实模型评分")
    parser.add_argument("--list", action="store_true", help="列出公开场景，不输出初态或答案")
    parser.add_argument("--scenario", choices=[item.scenario_id for item in list_scenarios()])
    parser.add_argument("--replay", type=Path, help="包含 calls 与 final 的 JSON 轨迹")
    parser.add_argument("--output", type=Path, help="可选 JSON 报告路径")
    args = parser.parse_args(argv)
    if args.list:
        print(json.dumps([{"id": item.scenario_id, "task": item.model_input()} for item in list_scenarios()], ensure_ascii=False, indent=2))
        return 0
    if args.scenario is None or args.replay is None:
        parser.error("回放需要 --scenario 和 --replay")
    try:
        if args.replay.stat().st_size > MAX_REPLAY_BYTES:
            raise ValueError("回放文件超过 256 KiB")
        payload = json.loads(args.replay.read_text(encoding="utf-8"))
        result = replay(args.scenario, payload)
    except (OSError, UnicodeError, ValueError, TypeError) as error:
        parser.error(str(error))
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output is not None:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
