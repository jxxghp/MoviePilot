#!/usr/bin/env python3

from __future__ import annotations

import json
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
API_SCRIPT = SCRIPT_DIR.parents[1] / "moviepilot-api" / "scripts" / "mp-api.py"


def run_api_call(args: list[str]) -> int:
    """调用 MoviePilot REST API 客户端执行更新相关接口。"""
    command = [sys.executable, str(API_SCRIPT), *args]
    return_code = __import__("subprocess").run(command, check=False).returncode
    return return_code


def print_usage() -> None:
    """输出更新脚本的命令行用法。"""
    print(
        "Usage:\n"
        f"  python {Path(sys.argv[0]).name} versions\n"
        f"  python {Path(sys.argv[0]).name} status\n"
        f"  python {Path(sys.argv[0]).name} check\n"
        f"  python {Path(sys.argv[0]).name} download\n"
        f"  python {Path(sys.argv[0]).name} install\n"
        f"  python {Path(sys.argv[0]).name} restart\n"
        f"  python {Path(sys.argv[0]).name} upgrade dev"
    )


def main() -> int:
    """执行 MoviePilot 更新脚本入口。"""
    argv = sys.argv[1:]
    if not argv or argv[0] in {"-h", "--help", "help"}:
        print_usage()
        return 0

    command = argv[0].lower()
    if command == "versions":
        return run_api_call(["GET", "/api/v1/system/versions"])

    if command == "restart":
        return run_api_call(["GET", "/api/v1/system/restart"])

    update_commands = {
        "status": ("GET", "/api/v1/system/update/status"),
        "check": ("POST", "/api/v1/system/update/check"),
        "download": ("POST", "/api/v1/system/update/download"),
        "install": ("POST", "/api/v1/system/update/install"),
    }
    if command in update_commands:
        method, path = update_commands[command]
        return run_api_call([method, path])

    if command == "upgrade":
        mode = (argv[1] if len(argv) > 1 else "").strip().lower()
        if mode != "dev":
            print("Error: only Dev uses upgrade; use check/download/install for Release", file=sys.stderr)
            return 1
        return run_api_call([
            "POST",
            "/api/v1/system/upgrade",
            "--json",
            json.dumps(mode, ensure_ascii=False),
        ])

    print(f"Error: unknown command: {command}", file=sys.stderr)
    print_usage()
    return 1


if __name__ == "__main__":
    sys.exit(main())
