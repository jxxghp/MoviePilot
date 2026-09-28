"""可选 SDK 的按需导入合同：存储发现与 API 路由加载不得把 115、SMB、Passkey 的 SDK 带入常驻内存。"""

from __future__ import annotations

import json
import subprocess
import sys

# 各 SDK 连同依赖的顶层包；oss2 会牵入阿里云 SDK
_OPTIONAL_SDKS = ("oss2", "aliyunsdkcore", "aliyunsdkkms", "smbclient", "smbprotocol", "spnego", "webauthn")


def _run_isolated(script: str) -> dict:
    """在全新解释器中执行导入探针，避免当前 pytest 模块缓存干扰。"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_storage_discovery_and_api_routes_keep_optional_sdks_cold() -> None:
    """文件整理模块发现存储、注册 API 路由后，可选 SDK 仍未导入。"""
    result = _run_isolated(
        f"""
import json
import sys

from app.testing.bootstrap import ensure_sites_stub, isolate_config_dir

isolate_config_dir()
ensure_sites_stub()
import app.api.apiv1
from app.foundation.reflection import ModuleHelper

storages = ModuleHelper.load(
    "app.modules.filemanager.storages",
    filter_func=lambda _, obj: hasattr(obj, "schema") and obj.schema,
)
forbidden = {_OPTIONAL_SDKS!r}
loaded = sorted(name for name in sys.modules if name.split(".")[0] in forbidden)
print(json.dumps({{"loaded": loaded, "schemas": sorted(s.schema.value for s in storages)}}))
"""
    )

    assert result["loaded"] == []
    # 按需导入不影响存储发现
    assert {"smb", "u115"} <= set(result["schemas"])
