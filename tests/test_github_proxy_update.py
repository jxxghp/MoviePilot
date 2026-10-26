"""
验证 Python 更新器与 Docker 控制脚本使用相同的 GitHub 代理地址规则。
"""

import os
import subprocess
from pathlib import Path

import pytest

from app.adapters.system import update as update_module


@pytest.mark.parametrize(
    ("proxy", "prefix"),
    [
        ("", ""),
        ("https://proxy.example", "https://proxy.example/"),
        ("https://proxy.example/", "https://proxy.example/"),
        (" https://proxy.example/path ", "https://proxy.example/path/"),
        ("proxy.example/path", "http://proxy.example/path/"),
    ],
)
def test_update_proxy_prefix_matches_docker(monkeypatch, tmp_path, proxy, prefix):
    """
    代理首尾空白、路径及尾斜杠兼容规则在两种部署更新入口保持一致。
    """
    url = "https://github.com/jxxghp/MoviePilot/archive/refs/tags/v3.1.9.zip"
    monkeypatch.setattr(update_module, "get_runtime_setting", lambda _key: proxy)
    updater = Path(__file__).resolve().parents[1] / "docker" / "update.sh"
    result = subprocess.run(
        ["/bin/bash", "-c", 'source "$1"; github_download_url "$2"', "proxy-test", str(updater), url],
        env={**os.environ, "GITHUB_PROXY": proxy, "CONFIG_DIR": str(tmp_path)},
        text=True, capture_output=True, check=True,
    )

    assert result.stdout.strip() == prefix + url
    assert update_module.SystemUpdateManager._proxied(url) == prefix + url
