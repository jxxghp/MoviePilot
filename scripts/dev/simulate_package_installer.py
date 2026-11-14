#!/usr/bin/env python3

"""
离线检查依赖安装策略生成的命令、代理和缓存环境。

只构造策略并断言脱敏输出，不安装依赖、不发起网络请求。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.adapters.system.package import PackageInstallRequest, build_package_install_strategies


def sample(name: str, request: PackageInstallRequest) -> None:
    """
    检查一个安装场景生成的全部候选策略，并打印脱敏命令。

    :param name: 输出中区分场景的标识
    :param request: 待验证的安装请求，仅用于构造策略
    :raises AssertionError: 策略缺失、泄漏凭据或代理与缓存环境不符合预期
    """
    print(f"## {name}")
    strategies = build_package_install_strategies(request)
    assert strategies, f"{name}: no strategies generated"
    for strategy in strategies:
        rendered = " ".join(strategy.safe_log_command)
        print(strategy.strategy_name)
        print(rendered)
        assert all("--proxy" not in arg for arg in strategy.command)
        assert "user:pass" not in rendered
        assert strategy.env["UV_CACHE_DIR"].endswith("/.cache/uv")
        if strategy.strategy_name.endswith("代理") or strategy.strategy_name.endswith("镜像+代理"):
            assert strategy.env["HTTPS_PROXY"] == "http://proxy.example:7890"


def main() -> None:
    """
    验证直连、镜像、代理及本地轮子目录组合，不执行安装命令。
    """
    root = ROOT
    config_dir = root / "config"
    python_bin = root.parent / ".venv-test" / "bin" / "python"
    requirements = root / "requirements.txt"

    samples = {
        "plain": PackageInstallRequest(
            dependency_files=(requirements,),
            python_bin=python_bin,
            config_dir=config_dir,
        ),
        "mirror": PackageInstallRequest(
            dependency_files=(requirements,),
            python_bin=python_bin,
            config_dir=config_dir,
            package_index_url="https://user:pass@mirror.example/simple",
        ),
        "proxy": PackageInstallRequest(
            dependency_files=(requirements,),
            python_bin=python_bin,
            config_dir=config_dir,
            proxy_url="http://proxy.example:7890",
        ),
        "mirror_proxy_wheels": PackageInstallRequest(
            dependency_files=(requirements,),
            python_bin=python_bin,
            config_dir=config_dir,
            find_links_dirs=[
                root / "plugins.v2" / "demo" / "wheels",
                root / "plugins.v2" / "other" / "wheels",
            ],
            package_index_url="https://user:pass@mirror.example/simple",
            proxy_url="http://proxy.example:7890",
        ),
    }

    for name, request in samples.items():
        sample(name, request)

    print("Package installer simulation passed")


if __name__ == "__main__":
    main()
