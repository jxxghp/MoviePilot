"""跨平台 CI 使用的运行依赖 profile 验证入口。"""

from __future__ import annotations

import argparse
import platform
import re
import shutil
import subprocess
import sys
import sysconfig
from importlib import import_module
from importlib.util import find_spec
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from app.doctor import dependencies as dependency_doctor
from app.runtime.dependencies.profile import runtime_excluded_dependency_pairs

_UV_MISSING_DEPENDENCY_PATTERN = re.compile(
    r"The package `(?P<package>[^`]+)` requires `(?P<requirement>[^`]+)`, "
    r"but [^\r\n;]+"
)
_UV_PIP_CHECK_SUMMARY_PATTERNS = (
    re.compile(r"^Using Python .+ environment at: .+$"),
    re.compile(r"^Checked \d+ packages? in .+$"),
    re.compile(r"^Found \d+ incompatibilit(?:y|ies)$"),
)


def _uv_health_errors(message: str, project_file: Path) -> set[str]:
    """返回未被项目 uv 排除策略覆盖的依赖健康诊断。"""
    excluded_pairs = runtime_excluded_dependency_pairs(project_file)
    errors: set[str] = set()
    for line in {item.strip() for item in message.splitlines() if item.strip()}:
        matches = list(_UV_MISSING_DEPENDENCY_PATTERN.finditer(line))
        if not matches:
            if not any(pattern.fullmatch(line) for pattern in _UV_PIP_CHECK_SUMMARY_PATTERNS):
                errors.add(line)
            continue
        for match in matches:
            try:
                dependency_name = Requirement(match.group("requirement")).name
            except InvalidRequirement:
                errors.add(match.group(0))
                continue
            pair = (
                canonicalize_name(match.group("package")),
                canonicalize_name(dependency_name),
            )
            if pair not in excluded_pairs:
                errors.add(match.group(0))
    return errors


def verify_uv_environment(project_file: Path = Path("pyproject.toml")) -> None:
    """执行 uv 元数据健康检查，仅接受项目显式声明的排除边。"""
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("未找到 uv 可执行文件")
    result = subprocess.run(
        [uv, "pip", "check", "--python", sys.executable],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode == 0:
        return
    errors = _uv_health_errors(
        "\n".join((result.stdout, result.stderr)),
        project_file.resolve(),
    )
    if errors:
        raise RuntimeError("uv 依赖健康检查失败：" + " | ".join(sorted(errors)))


def verify_platform_profile(
        *,
        expected_profile: str,
        expected_system: str,
        expected_machine: str,
) -> None:
    """验证运行平台、解释器 ABI 和 Windows Docker 能力边界。"""
    current_system = platform.system()
    current_machine = platform.machine()
    if current_system != expected_system:
        raise RuntimeError(f"运行系统不匹配：{current_system} != {expected_system}")
    if current_machine != expected_machine:
        raise RuntimeError(f"机器架构不匹配：{current_machine} != {expected_machine}")

    free_threaded = sysconfig.get_config_var("Py_GIL_DISABLED") == 1
    expected_free_threaded = expected_profile == "free-threaded"
    if free_threaded != expected_free_threaded:
        raise RuntimeError(f"解释器 profile 不匹配：free-threaded={free_threaded}")
    if sys._is_gil_enabled() is expected_free_threaded:
        raise RuntimeError("解释器 GIL 状态与运行 profile 不匹配")

    import_module("docker")
    if find_spec("pympler") is not None:
        raise RuntimeError("运行环境仍包含已移除的 Pympler")
    if current_system != "Windows":
        return

    docker_transport = import_module("docker.transport")
    win32_modules = ("pywintypes", "win32api", "win32file", "win32pipe")
    installed_win32_modules = {
        module_name for module_name in win32_modules if find_spec(module_name) is not None
    }
    if expected_free_threaded:
        if installed_win32_modules:
            raise RuntimeError(
                "Windows free-threaded profile 意外安装 pywin32："
                + ", ".join(sorted(installed_win32_modules))
            )
        if getattr(docker_transport, "NpipeHTTPAdapter", None) is not None:
            raise RuntimeError("Windows free-threaded profile 意外启用了 Docker named-pipe 能力")
        return

    missing_modules = set(win32_modules) - installed_win32_modules
    if missing_modules:
        raise RuntimeError(
            "Windows standard profile 缺少 pywin32 模块："
            + ", ".join(sorted(missing_modules))
        )
    for module_name in win32_modules:
        import_module(module_name)
    if getattr(docker_transport, "NpipeHTTPAdapter", None) is None:
        raise RuntimeError("Windows standard profile 缺少 Docker named-pipe 能力")


def verify_application_lifecycle() -> None:
    """在隔离配置下运行 FastAPI lifespan 并验证 readiness。"""
    from app.testing.bootstrap import ensure_sites_stub, isolate_config_dir

    isolate_config_dir()
    ensure_sites_stub()
    from fastapi.testclient import TestClient

    from app.factory import create_app

    with TestClient(create_app()) as client:
        response = client.get("/health/ready")
        if response.status_code != 200 or response.json() != {"status": "ready"}:
            raise RuntimeError(f"应用 readiness 验证失败：{response.text}")
        if sysconfig.get_config_var("Py_GIL_DISABLED") == 1 and sys._is_gil_enabled():
            raise RuntimeError("应用启动后启用了 GIL")


def main(
        *,
        expected_profile: str,
        expected_system: str,
        expected_machine: str,
) -> None:
    """验证锁定环境、原生能力和应用生命周期。"""
    verify_platform_profile(
        expected_profile=expected_profile,
        expected_system=expected_system,
        expected_machine=expected_machine,
    )
    dependency_doctor.main(full=True)
    verify_uv_environment()
    if expected_system == "Windows":
        verify_application_lifecycle()
    if sysconfig.get_config_var("Py_GIL_DISABLED") == 1 and sys._is_gil_enabled():
        raise RuntimeError("完整运行依赖验证后启用了 GIL")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--expected-profile",
        required=True,
        choices=("standard", "free-threaded"),
    )
    parser.add_argument("--expected-system", required=True)
    parser.add_argument("--expected-machine", required=True)
    arguments = parser.parse_args()
    main(
        expected_profile=arguments.expected_profile,
        expected_system=arguments.expected_system,
        expected_machine=arguments.expected_machine,
    )
