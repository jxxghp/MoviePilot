from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from app.adapters.system.package import (
    PackageInstallRequest,
    build_package_install_env,
    build_package_install_strategies,
    build_project_sync_strategies,
    redact_url,
)


def test_build_env_maps_proxy_and_cache(tmp_path, monkeypatch):
    monkeypatch.delenv("PIP_CACHE_DIR", raising=False)
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    monkeypatch.delenv("PACKAGE_CACHE_ROOT", raising=False)
    monkeypatch.setenv("HTTP_PROXY", "http://old.example:8080")
    request = PackageInstallRequest(
        dependency_files=(tmp_path / "requirements.txt",),
        python_bin=Path("/venv/bin/python"),
        config_dir=tmp_path / "config",
        package_index_url="https://user:pass@mirror.example/simple",
        proxy_url="http://proxy.example:7890",
    )

    env = build_package_install_env(request)

    assert env["HTTP_PROXY"] == "http://proxy.example:7890"
    assert env["HTTPS_PROXY"] == "http://proxy.example:7890"
    assert env["http_proxy"] == "http://proxy.example:7890"
    assert env["https_proxy"] == "http://proxy.example:7890"
    assert env["PACKAGE_CACHE_ROOT"] == str(tmp_path / "config" / ".cache")
    assert env["UV_CACHE_DIR"] == str(tmp_path / "config" / ".cache" / "uv")


def test_build_env_uses_package_cache_root_and_preserves_tool_cache_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("PACKAGE_CACHE_ROOT", str(tmp_path / "custom-package-cache"))
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    request = PackageInstallRequest(
        dependency_files=(tmp_path / "requirements.txt",),
        python_bin=Path("/venv/bin/python"),
        config_dir=tmp_path / "config",
    )

    env = build_package_install_env(request)

    assert env["PACKAGE_CACHE_ROOT"] == str(tmp_path / "custom-package-cache")
    assert env["UV_CACHE_DIR"] == str(tmp_path / "custom-package-cache" / "uv")


def test_build_strategies_prefers_uv_network_matrix_and_preserves_find_links(tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text("demo\n", encoding="utf-8")
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    uv_bin = tmp_path / "venv" / "bin" / "uv"
    uv_bin.parent.mkdir(parents=True)
    uv_bin.write_text("", encoding="utf-8")

    request = PackageInstallRequest(
        dependency_files=(req,),
        python_bin=tmp_path / "venv" / "bin" / "python",
        find_links_dirs=[wheels],
        config_dir=tmp_path / "config",
        package_index_url="https://mirror.example/simple",
        proxy_url="http://proxy.example:7890",
    )

    strategies = build_package_install_strategies(request)

    assert [strategy.strategy_name for strategy in strategies] == [
        "uv:镜像+代理",
        "uv:镜像",
        "uv:代理",
        "uv:直连",
    ]
    assert strategies[0].command[:3] == [str(uv_bin), "pip", "install"]
    assert "--python" in strategies[0].command
    assert "--find-links" in strategies[0].command
    assert "--default-index" in strategies[0].command
    assert "--no-index" not in strategies[0].command
    assert strategies[0].env["HTTPS_PROXY"] == "http://proxy.example:7890"
    assert "--default-index" in strategies[1].command
    assert "HTTPS_PROXY" not in {
        key for key, value in strategies[1].env.items() if value == "http://proxy.example:7890"
    }
    assert "--default-index" not in strategies[2].command


def test_build_strategies_fail_closed_when_uv_missing(tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text("demo\n", encoding="utf-8")
    request = PackageInstallRequest(
        dependency_files=(req,),
        python_bin=tmp_path / "venv" / "bin" / "python",
        config_dir=tmp_path / "config",
    )

    with patch("app.adapters.system.package.find_uv", return_value=None):
        strategies = build_package_install_strategies(request)

    assert strategies == []


def test_build_strategies_passes_all_manifests_to_one_uv_process(tmp_path):
    """多个插件清单必须进入同一个 uv 命令并保持输入顺序。"""
    modern = tmp_path / "modern" / "pyproject.toml"
    modern.parent.mkdir()
    modern.write_text("[project]\nname='modern'\nversion='1'\n", encoding="utf-8")
    legacy = tmp_path / "legacy" / "requirements.txt"
    legacy.parent.mkdir()
    legacy.write_text("demo\n", encoding="utf-8")
    uv_bin = tmp_path / "venv" / "bin" / "uv"
    uv_bin.parent.mkdir(parents=True)
    uv_bin.write_text("", encoding="utf-8")
    request = PackageInstallRequest(
        dependency_files=(modern, legacy),
        python_bin=tmp_path / "venv" / "bin" / "python",
    )

    strategies = build_package_install_strategies(request)

    command = strategies[0].command
    first_requirement = command.index("-r")
    second_requirement = command.index("-r", first_requirement + 1)
    assert command.count("-r") == 2
    assert command[first_requirement + 1] == str(modern)
    assert command[second_requirement + 1] == str(legacy)


def test_project_sync_selects_current_runtime_group(tmp_path, monkeypatch):
    """主项目恢复必须显式选择当前解释器对应的互斥运行依赖组。"""
    project = tmp_path / "project"
    project.mkdir()
    pyproject = project / "pyproject.toml"
    pyproject.write_text("[project]\nname='moviepilot'\nversion='0'\n", encoding="utf-8")
    uv_bin = tmp_path / "venv" / "bin" / "uv"
    uv_bin.parent.mkdir(parents=True)
    uv_bin.write_text("", encoding="utf-8")
    request = PackageInstallRequest(
        dependency_files=(pyproject,),
        python_bin=tmp_path / "venv" / "bin" / "python",
    )
    monkeypatch.setattr(
        "app.adapters.system.package.runtime_sync_arguments",
        lambda: ("--no-default-groups", "--group", "runtime-free-threaded"),
    )

    command = build_project_sync_strategies(request)[0].command

    assert command.count("--group") == 1
    assert command[command.index("--group") + 1] == "runtime-free-threaded"
    assert "--no-default-groups" in command


def test_redact_url_removes_userinfo():
    assert redact_url("https://user:pass@mirror.example/simple") == "https://mirror.example/simple"


def test_redact_url_removes_userinfo_with_invalid_port():
    assert (
        redact_url("https://user:pass@example.com:notaport/simple")
        == "https://example.com:notaport/simple"
    )
