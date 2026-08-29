from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from app.runtime.dependencies.profile import runtime_sync_arguments


@dataclass(frozen=True)
class PackageInstallRequest:
    """
    Python 包安装请求，集中描述依赖清单、工具缓存、代理和本地 wheels 候选源。
    """

    dependency_files: tuple[Path, ...]
    python_bin: Path
    find_links_dirs: list[Path] = field(default_factory=list)
    constraints_file: Path | None = None
    config_dir: Path = Path("/config")
    package_cache_root: Path | None = None
    package_index_url: str | None = None
    proxy_url: str | None = None
    purpose: str = "plugin"


@dataclass(frozen=True)
class PackageInstallStrategy:
    """
    单次安装尝试的完整执行信息，命令和日志展示命令分离以避免泄露凭据。
    """

    strategy_name: str
    command: list[str]
    env: dict[str, str]
    safe_log_command: list[str]


def redact_url(value: str) -> str:
    """
    脱敏 URL 中的 userinfo，保留 scheme、host、path、query 便于定位镜像源。
    """
    parsed = urlsplit(value)
    if "@" not in parsed.netloc:
        return value
    host = parsed.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parsed.scheme, host, parsed.path, parsed.query, parsed.fragment))


def redact_command(command: list[str]) -> list[str]:
    """
    脱敏命令参数中的 URL 凭据，用于日志展示。
    """
    return [redact_url(item) if "://" in item else item for item in command]


def build_package_install_env(request: PackageInstallRequest, include_moviepilot_proxy: bool = True) -> dict[str, str]:
    """
    构造 uv 安装子进程环境，默认把包下载缓存放到持久化配置目录。
    """
    env = os.environ.copy()
    config_dir = Path(request.config_dir)
    if request.package_cache_root:
        package_cache_root = Path(request.package_cache_root)
        env["PACKAGE_CACHE_ROOT"] = str(package_cache_root)
    else:
        package_cache_root = Path(env.get("PACKAGE_CACHE_ROOT") or config_dir / ".cache")
        env.setdefault("PACKAGE_CACHE_ROOT", str(package_cache_root))
    env.setdefault("UV_CACHE_DIR", str(package_cache_root / "uv"))
    proxy = (request.proxy_url or "").strip()
    if proxy and include_moviepilot_proxy:
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            env[key] = proxy
    return env


def find_uv(python_bin: Path) -> Path | None:
    """
    优先使用解释器同目录 uv，保证安装器与目标运行环境使用同一版本。
    """
    uv_name = "uv.exe" if os.name == "nt" else "uv"
    sibling = python_bin.with_name(uv_name)
    if sibling.exists():
        return sibling
    found = shutil.which("uv")
    return Path(found) if found else None


def _base_install_args(request: PackageInstallRequest) -> list[str]:
    args: list[str] = []
    for directory in request.find_links_dirs:
        args.extend(["--find-links", str(directory)])
    if request.constraints_file:
        args.extend(["-c", str(request.constraints_file)])
    for dependency_file in request.dependency_files:
        args.extend(["-r", str(dependency_file)])
    return args


def _network_variants(request: PackageInstallRequest) -> list[tuple[str, bool, bool]]:
    has_index = bool((request.package_index_url or "").strip())
    has_proxy = bool((request.proxy_url or "").strip())
    variants: list[tuple[str, bool, bool]] = []
    if has_index and has_proxy:
        variants.append(("镜像+代理", True, True))
    if has_index:
        variants.append(("镜像", True, False))
    if has_proxy:
        variants.append(("代理", False, True))
    variants.append(("直连", False, False))
    return variants


def _build_uv_command(uv_bin: Path, request: PackageInstallRequest, use_index: bool) -> list[str]:
    command = [str(uv_bin), "pip", "install", "--python", str(request.python_bin)]
    if use_index and request.package_index_url:
        command.extend(["--default-index", request.package_index_url])
    command.extend(_base_install_args(request))
    return command


def _build_uv_sync_command(uv_bin: Path, request: PackageInstallRequest, use_index: bool) -> list[str]:
    if len(request.dependency_files) != 1:
        raise ValueError("主项目锁定依赖恢复只接受一个 pyproject.toml")
    project_file = request.dependency_files[0]
    command = [
        str(uv_bin),
        "sync",
        "--project",
        str(project_file.parent),
        "--locked",
        "--no-dev",
        "--no-install-project",
        "--inexact",
        *runtime_sync_arguments(),
    ]
    if use_index and request.package_index_url:
        command.extend(["--default-index", request.package_index_url])
    return command


def build_package_install_strategies(request: PackageInstallRequest) -> list[PackageInstallStrategy]:
    """
    为固定 uv 安装器构造镜像、代理和直连降级策略。
    """
    strategies: list[PackageInstallStrategy] = []
    variants = _network_variants(request)
    uv_bin = find_uv(Path(request.python_bin))
    if not uv_bin:
        return strategies

    for variant_name, use_index, use_proxy in variants:
        command = _build_uv_command(uv_bin, request, use_index)
        env = build_package_install_env(request, include_moviepilot_proxy=use_proxy)
        strategies.append(
            PackageInstallStrategy(
                strategy_name=f"uv:{variant_name}",
                command=command,
                env=env,
                safe_log_command=redact_command(command),
            )
        )
    return strategies


def build_project_sync_strategies(request: PackageInstallRequest) -> list[PackageInstallStrategy]:
    """为主项目锁定依赖恢复构造 uv 网络降级策略。"""
    uv_bin = find_uv(Path(request.python_bin))
    if not uv_bin:
        return []

    strategies = []
    project_environment = request.python_bin.parent.parent
    for variant_name, use_index, use_proxy in _network_variants(request):
        command = _build_uv_sync_command(uv_bin, request, use_index)
        env = build_package_install_env(request, include_moviepilot_proxy=use_proxy)
        env["UV_PROJECT_ENVIRONMENT"] = str(project_environment)
        strategies.append(
            PackageInstallStrategy(
                strategy_name=f"uv:{variant_name}",
                command=command,
                env=env,
                safe_log_command=redact_command(command),
            )
        )
    return strategies
