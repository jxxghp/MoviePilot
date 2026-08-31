#!/usr/bin/env python3
"""从 FastAPI OpenAPI 生成 moviepilot_api 的外部 MCP 输入合同。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from fastapi import FastAPI

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.agent.policy.api import API_OPERATION_ROUTES, API_OPERATION_SPECS  # noqa: E402
from app.agent.policy.api_mcp_contract import build_api_mcp_input_schema  # noqa: E402
from app.api.apiv1 import api_router  # noqa: E402

OUTPUT_PATH = PROJECT_ROOT / "app/agent/policy/api_mcp_schema.json"


def generate_schema() -> dict:
    """聚合 v1 路由 OpenAPI 并生成稳定的网关 MCP schema。"""
    app = FastAPI()
    app.include_router(api_router, prefix="/api/v1")
    return build_api_mcp_input_schema(
        openapi=app.openapi(),
        routes=API_OPERATION_ROUTES,
        specs=API_OPERATION_SPECS,
    )


def main() -> int:
    """写入格式稳定的生成文件。"""
    schema = generate_schema()
    OUTPUT_PATH.write_text(
        json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"generated {OUTPUT_PATH.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
