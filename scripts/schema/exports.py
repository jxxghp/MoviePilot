#!/usr/bin/env python3
"""生成并校验 ``app.schemas`` 根入口的惰性导出清单。"""

import argparse
import importlib
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
OUTPUT_PATH = PROJECT_ROOT / "app" / "schemas" / "exports.py"
SCHEMA_MODULES = (
    "agent",
    "cache",
    "category",
    "common",
    "context",
    "dashboard",
    "download",
    "event",
    "exception",
    "file",
    "history",
    "llm",
    "mediaserver",
    "message",
    "mfa",
    "music",
    "monitoring",
    "notification",
    "plugin",
    "response",
    "rule",
    "search",
    "storage",
    "openai",
    "servarr",
    "servcookie",
    "site",
    "subscribe",
    "system",
    "tmdb",
    "token",
    "transfer",
    "user",
    "workflow",
    "mcp",
)


def collect_exports() -> tuple[dict[str, tuple[str, str]], dict[str, list[str]]]:
    """按旧星号导入顺序收集最终导出所有者和重名来源。"""
    exports: dict[str, tuple[str, str]] = {}
    sources: dict[str, list[str]] = {}
    for module_basename in SCHEMA_MODULES:
        module_name = f"app.schemas.{module_basename}"
        module = importlib.import_module(module_name)
        names = getattr(module, "__all__", None)
        if names is None:
            names = [name for name in vars(module) if not name.startswith("_")]
        for name in names:
            if not hasattr(module, name):
                continue
            exports[name] = (module_name, name)
            sources.setdefault(name, []).append(module_name)
    conflicts = {
        name: module_names
        for name, module_names in sources.items()
        if len(set(module_names)) > 1
    }
    return dict(sorted(exports.items())), dict(sorted(conflicts.items()))


def render_manifest() -> str:
    """把导出与冲突清单渲染为稳定、可审查的 Python 模块。"""
    exports, conflicts = collect_exports()
    lines = [
        '"""由 scripts/schema/exports.py 生成，请勿手工编辑。"""',
        "",
        "SCHEMA_EXPORTS = {",
    ]
    lines.extend(
        f"    {name!r}: ({module_name!r}, {symbol_name!r}),"
        for name, (module_name, symbol_name) in exports.items()
    )
    lines.extend(["}", "", "SCHEMA_CONFLICTS = {"])
    lines.extend(
        f"    {name!r}: {module_names!r},"
        for name, module_names in conflicts.items()
    )
    lines.extend(["}", ""])
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    """解析写入或校验动作。"""
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--write", action="store_true")
    action.add_argument("--check", action="store_true")
    return parser.parse_args()


def main() -> int:
    """写入清单，或检查当前 schema 公开面是否发生漂移。"""
    args = parse_args()
    rendered = render_manifest()
    if args.write:
        OUTPUT_PATH.write_text(rendered, encoding="utf-8")
        print(f"已写入 {OUTPUT_PATH.relative_to(PROJECT_ROOT)}")
        return 0
    current = OUTPUT_PATH.read_text(encoding="utf-8")
    if current == rendered:
        return 0
    print(
        "schema 导出清单已变化；确认兼容性后运行 "
        "scripts/schema/exports.py --write",
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
