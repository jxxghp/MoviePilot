"""按函数维护圈复杂度与嵌套深度低水位；源码行数仅用于热点报告。"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BASELINE = PROJECT_ROOT / "tests/fixtures/architecture/complexity-baseline.json"
THRESHOLDS = {"C901": 15, "PLR1702": 5}
SCHEMA_VERSION = 3
_METRIC = re.compile(r"\((\d+) > (\d+)\)$")


def _iter_python_files(root: Path) -> list[Path]:
    """完整扫描宿主 Python 源码，插件副本由插件仓治理。"""
    paths = [
        path for path in sorted((root / "app").rglob("*.py"))
        if not path.relative_to(root).as_posix().startswith("app/plugins/")
    ]
    if not paths:
        raise ValueError("未找到宿主 Python 源码，拒绝生成空基线")
    return paths


def _walk_owned_nodes(
    nodes: Iterable[ast.AST], prefix: str = "",
) -> Iterable[tuple[str, ast.AST]]:
    """遍历任意控制流下的类和函数，保留私有、dunder 与嵌套词法归属。"""
    for node in nodes:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            qualname = f"{prefix}.{node.name}" if prefix else node.name
            yield qualname, node
            yield from _walk_owned_nodes(ast.iter_child_nodes(node), qualname)
        else:
            yield from _walk_owned_nodes(ast.iter_child_nodes(node), prefix)


def _owned_nodes(tree: ast.Module) -> Iterable[tuple[str, ast.AST]]:
    """为条件重定义和 property 同名定义分配序号，避免相互覆盖预算。"""
    nodes = list(_walk_owned_nodes(tree.body))
    totals = Counter(name for name, _ in nodes)
    seen: Counter[str] = Counter()
    for name, node in nodes:
        seen[name] += 1
        yield (f"{name}#{seen[name]}" if totals[name] > 1 else name), node


def run_ruff(root: Path) -> list[dict[str, Any]]:
    """使用锁定的 Ruff 计算指标；配置、noqa 与 gitignore 不能隐藏新增复杂度。"""
    result = subprocess.run(
        [
            sys.executable, "-m", "ruff", "check", "app",
            "--isolated", "--no-cache", "--no-respect-gitignore", "--ignore-noqa",
            "--preview", "--target-version", "py314", "--select", ",".join(THRESHOLDS),
            "--exclude", "app/plugins", "--config", 'include = ["*.py"]',
            "--config", f"lint.mccabe.max-complexity={THRESHOLDS['C901']}",
            "--config", f"lint.pylint.max-nested-blocks={THRESHOLDS['PLR1702']}",
            "--output-format", "json",
        ],
        cwd=root, capture_output=True, text=True, check=False,
    )
    if result.returncode not in {0, 1} or result.stderr.strip():
        raise ValueError(f"Ruff 执行失败：{result.stderr or result.stdout}")
    diagnostics = json.loads(result.stdout)
    if not isinstance(diagnostics, list) or bool(diagnostics) != (result.returncode == 1):
        raise ValueError("Ruff 退出状态与诊断列表不一致")
    return diagnostics


def collect_complexity(root: Path = PROJECT_ROOT) -> dict[str, dict[str, int]]:
    """把 Ruff 诊断映射为规则、文件和词法 owner，不使用易变行号作为基线身份。"""
    root = root.resolve()
    owners = {}
    for path in _iter_python_files(root):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        owners[path.relative_to(root).as_posix()] = [
            (node.lineno, node.end_lineno, name) for name, node in _owned_nodes(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
    report: dict[str, dict[str, int]] = {code: {} for code in THRESHOLDS}
    for diagnostic in run_ruff(root):
        code = diagnostic.get("code")
        if code not in THRESHOLDS:
            raise ValueError(f"Ruff 返回非预期诊断：{diagnostic}")
        match = _METRIC.search(diagnostic["message"])
        if not match or int(match[2]) != THRESHOLDS[code] or int(match[1]) <= THRESHOLDS[code]:
            raise ValueError(f"无法解析 Ruff 复杂度：{diagnostic}")
        path = Path(diagnostic["filename"]).resolve().relative_to(root).as_posix()
        row = diagnostic["location"]["row"]
        candidates = [
            (start, name) for start, end, name in owners.get(path, [])
            if start <= row <= end and (code != "C901" or start == row)
        ]
        if not candidates:
            raise ValueError(f"Ruff 诊断无法定位函数：{diagnostic}")
        owner = max(candidates)[1]
        key = f"{path}:{owner}"
        if code == "C901" and key in report[code]:
            raise ValueError(f"Ruff 返回重复函数诊断：{code} {key}")
        # PLR1702 按控制块报告，同一函数可能有多处；取最大深度保留逐函数预算。
        report[code][key] = max(report[code].get(key, 0), int(match[1]))
    return report


def collect_source_size(root: Path = PROJECT_ROOT) -> dict[str, dict[str, int]]:
    """记录方法超过 150、类超过 500、文件超过 1000 行的观察项，不参与阻断。"""
    report: dict[str, dict[str, int]] = {"method": {}, "class": {}, "file": {}}
    for path in _iter_python_files(root):
        relative = path.relative_to(root).as_posix()
        source = path.read_text(encoding="utf-8")
        if len(source.splitlines()) > 1000:
            report["file"][relative] = len(source.splitlines())
        for name, node in _owned_nodes(ast.parse(source, filename=str(path))):
            category, limit = ("class", 500) if isinstance(node, ast.ClassDef) else ("method", 150)
            count = node.end_lineno - node.lineno + 1
            if count > limit:
                report[category][f"{relative}:{name}"] = count
    return report


def baseline_payload(metrics: dict[str, dict[str, int]]) -> dict[str, Any]:
    """把指标与算法版本、阈值一起持久化，避免静默改变门禁口径。"""
    return {"schema_version": SCHEMA_VERSION, "thresholds": THRESHOLDS, "metrics": metrics}


def read_baseline(path: Path) -> dict[str, dict[str, int]]:
    """严格校验基线格式，旧行数基线与策略变更必须显式迁移。"""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "thresholds", "metrics"}
        or payload["schema_version"] != SCHEMA_VERSION
        or payload["thresholds"] != THRESHOLDS
    ):
        raise ValueError("复杂度基线版本或阈值不匹配，必须审查指标迁移")
    metrics = payload["metrics"]
    if not isinstance(metrics, dict) or set(metrics) != set(THRESHOLDS):
        raise ValueError("复杂度基线缺少规则或包含未知规则")
    for code, entries in metrics.items():
        if not isinstance(entries, dict):
            raise ValueError(f"复杂度基线 {code} 必须是函数指标对象")
        for owner, value in entries.items():
            path, separator, name = owner.partition(":")
            if (
                not separator or not name or not path.startswith("app/")
                or not path.endswith(".py") or ".." in Path(path).parts
                or path.startswith("app/plugins/")
                or type(value) is not int or value <= THRESHOLDS[code]
            ):
                raise ValueError(f"复杂度基线存在无效指标：{code} {owner}={value}")
    return metrics


def classify_complexity(
    baseline: dict[str, dict[str, int]], current: dict[str, dict[str, int]],
) -> tuple[list[str], list[str]]:
    """分别返回逐函数新增/增长与待固化下降，函数之间不能抵消债务。"""
    regressions: list[str] = []
    stale: list[str] = []
    for code, threshold in THRESHOLDS.items():
        previous, latest = baseline.get(code, {}), current.get(code, {})
        for owner in sorted(previous.keys() | latest.keys()):
            old, new = previous.get(owner, threshold), latest.get(owner, threshold)
            if new > old:
                regressions.append(f"{code}: 新增超限或复杂度增长 {owner} {old}->{new}")
            elif new < old:
                value = str(new) if owner in latest else f"<={threshold} 或已删除"
                stale.append(f"{code}: 低水位未固化 {owner} {old}->{value}")
    return regressions, stale


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """以稳定排序写入可审查的 JSON 基线或观察报告。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    """检查结构复杂度；显式写入只接受首次建账或已证明的低水位下降。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="首次建账或固化下降后的低水位")
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--report", type=Path, help="输出结构指标与不阻断的源码行数热点")
    args = parser.parse_args()
    try:
        if args.report and args.report.resolve() == args.baseline.resolve():
            raise ValueError("报告路径不能覆盖复杂度基线")
        current = collect_complexity()
        if args.report:
            _write_json(args.report, {**baseline_payload(current), "source_size": collect_source_size()})
        exists = args.baseline.exists()
        baseline = read_baseline(args.baseline) if exists else {}
        regressions, stale = classify_complexity(baseline, current)
        if args.write:
            if exists and regressions:
                print("\n".join(regressions))
                print("拒绝写入：--write 只能固化下降后的低水位，不能接受复杂度增长。")
                return 1
            _write_json(args.baseline, baseline_payload(current))
            print(f"已写入 {args.baseline}")
            return 0
        if not exists:
            raise ValueError(f"缺少复杂度基线：{args.baseline}")
        if regressions or stale:
            print("\n".join([*regressions, *stale]))
            print("先消除新增超限与增长；仅存在下降时使用 --write 固化低水位。")
            return 1
    except (OSError, ValueError, SyntaxError, KeyError, TypeError) as error:
        print(f"复杂度检查失败：{error}")
        return 1
    print("结构复杂度 ratchet 通过（C901 <= 15、PLR1702 <= 5，存量逐函数只降不增）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
