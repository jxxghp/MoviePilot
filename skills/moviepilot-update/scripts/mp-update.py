#!/usr/bin/env python3

from __future__ import annotations

import json
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
API_SCRIPT = SCRIPT_DIR.parents[1] / "moviepilot-api" / "scripts" / "mp-api.py"


def run_api_call(args: list[str]) -> int:
    command = [sys.executable, str(API_SCRIPT), *args]
    return_code = __import__("subprocess").run(command, check=False).returncode
    return return_code


def print_usage() -> None:
    print(
        "Usage:\n"
        f"  python {Path(sys.argv[0]).name} versions\n"
        f"  python {Path(sys.argv[0]).name} restart\n"
        f"  python {Path(sys.argv[0]).name} upgrade [release|dev]"
    )


def main() -> int:
    argv = sys.argv[1:]
    if not argv or argv[0] in {"-h", "--help", "help"}:
        print_usage()
        return 0

    command = argv[0].lower()
    if command == "versions":
        return run_api_call(["GET", "/api/v1/system/versions"])

    if command == "restart":
        return run_api_call(["GET", "/api/v1/system/restart"])

    if command == "upgrade":
        mode = (argv[1] if len(argv) > 1 else "release").strip().lower()
        if mode == "true":
            mode = "release"
        if mode not in {"release", "dev"}:
            print("Error: mode must be release or dev", file=sys.stderr)
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
