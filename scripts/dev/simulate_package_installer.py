#!/usr/bin/env python3

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.helper.package_installer import PackageInstallRequest, build_package_install_strategies


def sample(name: str, request: PackageInstallRequest) -> None:
    print(f"## {name}")
    strategies = build_package_install_strategies(request)
    assert strategies, f"{name}: no strategies generated"
    for strategy in strategies:
        rendered = " ".join(strategy.safe_log_command)
        print(strategy.strategy_name)
        print(rendered)
        assert all("--proxy" not in arg for arg in strategy.command)
        assert "user:pass" not in rendered
        assert strategy.env["PIP_CACHE_DIR"].endswith("/.cache/pip")
        assert strategy.env["UV_CACHE_DIR"].endswith("/.cache/uv")
        if strategy.strategy_name.endswith("代理") or strategy.strategy_name.endswith("镜像+代理"):
            assert strategy.env["HTTPS_PROXY"] == "http://proxy.example:7890"


def main() -> None:
    root = ROOT
    config_dir = root / "config"
    python_bin = root.parent / ".venv-test" / "bin" / "python"
    requirements = root / "requirements.txt"

    samples = {
        "plain": PackageInstallRequest(
            requirements_file=requirements,
            python_bin=python_bin,
            config_dir=config_dir,
        ),
        "mirror": PackageInstallRequest(
            requirements_file=requirements,
            python_bin=python_bin,
            config_dir=config_dir,
            pip_index_url="https://user:pass@mirror.example/simple",
        ),
        "proxy": PackageInstallRequest(
            requirements_file=requirements,
            python_bin=python_bin,
            config_dir=config_dir,
            proxy_url="http://proxy.example:7890",
        ),
        "mirror_proxy_wheels": PackageInstallRequest(
            requirements_file=requirements,
            python_bin=python_bin,
            config_dir=config_dir,
            find_links_dirs=[
                root / "plugins.v2" / "demo" / "wheels",
                root / "plugins.v2" / "other" / "wheels",
            ],
            pip_index_url="https://user:pass@mirror.example/simple",
            proxy_url="http://proxy.example:7890",
        ),
    }

    for name, request in samples.items():
        sample(name, request)

    print("Package installer simulation passed")


if __name__ == "__main__":
    main()
