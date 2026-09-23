"""free-threaded（v3t）运行时插件依赖 resolve-only 预检与失败原因归类测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.adapters.system import package as package_module
from app.adapters.system.package import (
    PackageInstallRequest,
    build_package_install_strategies,
    describe_free_threaded_install_failure,
)
from app.adapters.system.plugin import health as health_module
from app.adapters.system.plugin.health import PluginRuntimeHealth

# 摘自 uv 0.12 在 cp314t 下解析 faster-whisper（onnxruntime 无 cp314t wheel 且无 sdist）的真实输出
UV_FREE_THREADED_RESOLVE_FAILURE = """  × No solution found when resolving dependencies:
  ╰─▶ Because onnxruntime>=1.14.0,<=1.23.2 has no wheels with a free-threading
      compatible ABI tag and only the following versions of onnxruntime are
      available:
      we can conclude that your requirements are unsatisfiable.

hint: You require free-threaded CPython 3.14 (`cp314t`), but we only found wheels for `onnxruntime` (v1.23.2) with the following Python ABI tags: `cp310`, `cp311`, `cp312`, `cp313`, `cp313t`
hint: Wheels are available for `onnxruntime` (v1.30.0) on the following platforms: `manylinux_2_28_aarch64`
"""

# 摘自 uv 0.12 在无编译工具链的 python:3.14-slim 容器内为 cp314t 构建 tokenizers sdist 的真实输出
UV_SOURCE_BUILD_FAILURE = """error: Failed to build `tokenizers==0.23.2`
  cause: The build backend returned an error
  cause: Call to `maturin.build_wheel` failed (exit status: 1)

hint: `tokenizers` (v0.23.2) was included because `faster-whisper` (v1.2.1) depends on `tokenizers`
"""

HEALTHY = {"uv check": (True, "ok"), "核心依赖导入检查": (True, "ok")}


def test_describe_resolve_failure_keeps_free_threaded_hint():
    """解析阶段的 ABI 不兼容归类为 v3t 不兼容，并保留 uv 的 cp314t 提示行。"""
    reason = describe_free_threaded_install_failure(UV_FREE_THREADED_RESOLVE_FAILURE)

    assert reason is not None
    assert reason.startswith("依赖不兼容 free-threaded 运行时（v3t）")
    assert "You require free-threaded CPython 3.14" in reason
    assert "Wheels are available for" not in reason


def test_describe_source_build_failure_names_package():
    """源码构建失败归类为缺编译工具链，并指出具体依赖。"""
    reason = describe_free_threaded_install_failure(UV_SOURCE_BUILD_FAILURE)

    assert reason is not None
    assert "tokenizers==0.23.2" in reason
    assert "编译工具链" in reason


def test_describe_unknown_failure_returns_none():
    """网络等无法归类的失败不附加 v3t 原因。"""
    assert describe_free_threaded_install_failure("error: Request failed after 3 retries") is None


def test_dry_run_strategies_share_install_inputs(tmp_path):
    """预检命令与真实安装共用清单、约束、wheels 与网络矩阵，只多一个 --dry-run。"""
    uv_bin = tmp_path / "uv"
    uv_bin.write_text("", encoding="utf-8")
    request = PackageInstallRequest(
        dependency_files=(tmp_path / "pyproject.toml",),
        python_bin=tmp_path / "python",
        find_links_dirs=[tmp_path / "wheels"],
        constraints_file=tmp_path / "constraints.txt",
        package_index_url="https://mirror.example/simple",
        proxy_url="http://proxy.example:7890",
    )
    original_find_uv = package_module.find_uv
    package_module.find_uv = lambda _python_bin: uv_bin
    try:
        install = build_package_install_strategies(request)
        preflight = build_package_install_strategies(request, dry_run=True)
    finally:
        package_module.find_uv = original_find_uv

    assert [item.strategy_name for item in preflight] == [item.strategy_name for item in install]
    for install_strategy, preflight_strategy in zip(install, preflight):
        assert "--dry-run" not in install_strategy.command
        expected = list(install_strategy.command)
        expected.insert(expected.index("--python") + 2, "--dry-run")
        assert preflight_strategy.command == expected
        assert preflight_strategy.env == install_strategy.env


@pytest.fixture
def install_env(tmp_path, monkeypatch):
    """构造可执行安装链路的最小环境：镜像+代理产生四个网络策略，健康检查恒为健康。"""
    manifest = tmp_path / "requirements.txt"
    manifest.write_text("demo\n", encoding="utf-8")
    uv_bin = tmp_path / "uv"
    uv_bin.write_text("", encoding="utf-8")
    settings = {
        "ROOT_PATH": Path(__file__).resolve().parents[1],
        "TEMP_PATH": tmp_path / "temp",
        "CONFIG_PATH": tmp_path / "config",
        "PACKAGE_CACHE_PATH": tmp_path / "cache",
        "PROXY_HOST": "http://proxy.example:7890",
        "PIP_PROXY": "https://mirror.example/simple",
    }
    monkeypatch.setattr(package_module, "find_uv", lambda _python_bin: uv_bin)
    monkeypatch.setattr(health_module, "get_runtime_setting", settings.get)
    monkeypatch.setattr(
        PluginRuntimeHealth,
        "_PluginRuntimeHealth__get_protected_runtime_packages",
        classmethod(lambda cls, _installed: {}),
    )
    monkeypatch.setattr(
        PluginRuntimeHealth,
        "_PluginRuntimeHealth__run_runtime_healthcheck",
        classmethod(lambda cls: dict(HEALTHY)),
    )

    async def healthy_async(cls):
        return dict(HEALTHY)

    monkeypatch.setattr(
        PluginRuntimeHealth,
        "_PluginRuntimeHealth__async_run_runtime_healthcheck",
        classmethod(healthy_async),
    )
    return manifest


def _patch_subprocess(monkeypatch, responder):
    """同一应答函数同时替换同步与异步子进程执行，记录全部命令。"""
    calls: list[list[str]] = []

    def execute(command, env=None, safe_command=None, timeout=None):
        calls.append(list(command))
        return responder(command)

    async def execute_async(command, env=None, safe_command=None, timeout=None):
        return execute(command, env=env, safe_command=safe_command, timeout=timeout)

    monkeypatch.setattr(health_module.SystemUtils, "execute_with_subprocess", staticmethod(execute))
    monkeypatch.setattr(
        health_module.SystemUtils,
        "execute_with_subprocess_async",
        staticmethod(execute_async),
    )
    return calls


def _install(manifest: Path, use_async: bool):
    if use_async:
        return asyncio.run(PluginRuntimeHealth().async_install_packages_with_fallback(manifest))
    return PluginRuntimeHealth.install_packages_with_fallback(manifest)


@pytest.mark.parametrize("use_async", [False, True])
def test_preflight_blocks_when_every_strategy_reports_free_threaded_incompatibility(
    install_env, monkeypatch, use_async
):
    """全部预检策略都给出 ABI 不兼容时直接拒绝，不进入真实安装。"""
    monkeypatch.setattr(health_module, "is_free_threaded_runtime", lambda: True)
    calls = _patch_subprocess(monkeypatch, lambda _command: (False, UV_FREE_THREADED_RESOLVE_FAILURE))

    success, message = _install(install_env, use_async)

    assert not success
    assert message.startswith("依赖不兼容 free-threaded 运行时（v3t）")
    assert len(calls) == 4
    assert all("--dry-run" in command for command in calls)


@pytest.mark.parametrize("use_async", [False, True])
def test_preflight_passes_when_any_strategy_resolves(install_env, monkeypatch, use_async):
    """镜像策略解析失败但后续代理策略能解析时放行，避免把单一网络路径的失败误判为不兼容。"""
    monkeypatch.setattr(health_module, "is_free_threaded_runtime", lambda: True)

    def responder(command):
        if "--dry-run" in command and "--default-index" in command:
            return False, UV_FREE_THREADED_RESOLVE_FAILURE
        return True, "ok"

    calls = _patch_subprocess(monkeypatch, responder)

    success, message = _install(install_env, use_async)

    assert success
    assert message == "ok"
    preflight_calls = [command for command in calls if "--dry-run" in command]
    assert len(preflight_calls) == 3
    assert "--default-index" not in preflight_calls[-1]
    assert "--dry-run" not in calls[-1]


@pytest.mark.parametrize("use_async", [False, True])
def test_unclassified_preflight_failure_falls_through_to_install(install_env, monkeypatch, use_async):
    """网络等无法归类的预检失败不拦截，交给真实安装按原有链路处理。"""
    monkeypatch.setattr(health_module, "is_free_threaded_runtime", lambda: True)

    def responder(command):
        if "--dry-run" in command:
            return False, "error: Request failed after 3 retries"
        return True, "ok"

    calls = _patch_subprocess(monkeypatch, responder)

    success, _message = _install(install_env, use_async)

    assert success
    assert sum("--dry-run" in command for command in calls) == 4


@pytest.mark.parametrize("use_async", [False, True])
def test_standard_runtime_skips_preflight(install_env, monkeypatch, use_async):
    """标准解释器不付预检成本，也不给失败附加 v3t 原因。"""
    monkeypatch.setattr(health_module, "is_free_threaded_runtime", lambda: False)
    calls = _patch_subprocess(
        monkeypatch,
        lambda command: (False, UV_SOURCE_BUILD_FAILURE) if "install" in command else (True, "ok"),
    )

    success, message = _install(install_env, use_async)

    assert not success
    assert not any("--dry-run" in command for command in calls)
    assert "编译工具链" not in message


@pytest.mark.parametrize("use_async", [False, True])
def test_install_source_build_failure_gets_readable_reason(install_env, monkeypatch, use_async):
    """解析通过但源码构建失败时，真实安装的错误前置可读原因并保留原始输出。"""
    monkeypatch.setattr(health_module, "is_free_threaded_runtime", lambda: True)

    def responder(command):
        if "--dry-run" in command:
            return True, "Would install 26 packages"
        if command[1:3] == ["pip", "install"]:
            return False, UV_SOURCE_BUILD_FAILURE
        return True, "ok"

    _patch_subprocess(monkeypatch, responder)

    success, message = _install(install_env, use_async)

    assert not success
    assert message.startswith("依赖 tokenizers==0.23.2 需要从源码构建但构建失败")
    assert "Failed to build `tokenizers==0.23.2`" in message
