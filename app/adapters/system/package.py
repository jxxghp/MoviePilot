"""
构造 Python 包安装和主项目锁定环境恢复命令，不直接执行安装。
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from app.runtime.dependencies.profile import runtime_sync_arguments


@dataclass(frozen=True)
class PackageInstallRequest:
    """
    一次 Python 包安装所需的依赖、解释器和下载配置。

    :param dependency_files: 依赖清单路径；主项目恢复时必须仅包含一个 pyproject.toml
    :param python_bin: 安装目标环境的 Python 解释器路径
    :param find_links_dirs: 额外搜索 wheel 的本地目录，默认空列表
    :param constraints_file: 安装时使用的约束文件；None 表示不额外限制版本
    :param config_dir: 持久化配置目录，默认 /config，供计算默认缓存路径
    :param package_cache_root: 包缓存根目录；None 时使用环境变量或配置目录下的 .cache
    :param package_index_url: 首选包索引地址；空值表示仅使用安装器默认来源
    :param proxy_url: 包下载代理地址；空值表示不覆盖继承的代理环境变量
    :param purpose: 安装用途标识，默认 plugin，供调用方区分插件与主项目恢复
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
    一次安装尝试的命令和子进程环境，另存脱敏后的日志命令。

    :param strategy_name: 安装器和网络路径组合的显示名称
    :param command: 直接传给子进程的参数列表，可能含原始下载凭据
    :param env: 安装子进程使用的完整环境变量映射
    :param safe_log_command: 仅去除 URL 用户凭据后的展示命令；不是安全过滤后的可执行命令
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


def _build_uv_command(
        uv_bin: Path,
        request: PackageInstallRequest,
        use_index: bool,
        dry_run: bool = False,
) -> list[str]:
    command = [str(uv_bin), "pip", "install", "--python", str(request.python_bin)]
    if dry_run:
        command.append("--dry-run")
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


def build_package_install_strategies(
        request: PackageInstallRequest,
        dry_run: bool = False,
) -> list[PackageInstallStrategy]:
    """
    为固定 uv 安装器构造镜像、代理和直连降级策略。

    dry_run 为 True 时构造 resolve-only 预检命令（`uv pip install --dry-run`），
    与真实安装共用同一组依赖清单、约束文件、本地 wheels 和网络降级矩阵，
    保证预检结论与真实安装的解析输入一致。
    """
    strategies: list[PackageInstallStrategy] = []
    variants = _network_variants(request)
    uv_bin = find_uv(Path(request.python_bin))
    if not uv_bin:
        return strategies

    for variant_name, use_index, use_proxy in variants:
        command = _build_uv_command(uv_bin, request, use_index, dry_run=dry_run)
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
    """
    为主项目锁定依赖恢复构造 uv 网络降级策略。
    """
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


# uv 在目标解释器为 free-threaded（cp314t）且依赖没有可用 wheel 时输出的解析提示
_FREE_THREADED_ABI_HINTS = (
    "free-threading compatible ABI tag",
    "You require free-threaded CPython",
)
# uv 源码构建失败的首行，例如 "Failed to build `tokenizers==0.23.2`"
_UV_BUILD_FAILURE_PATTERN = re.compile(r"Failed to build `([^`]+)`")


def describe_free_threaded_install_failure(message: str) -> str | None:
    """
    把 uv 在 free-threaded 运行时下的安装/解析失败输出归类为可读原因，无法归类时返回 None。

    只识别两类有据可依的失败：
    - 解析阶段：依赖没有 cp314t wheel 且没有可用 sdist，uv 会给出 free-threading ABI 提示；
    - 构建阶段：依赖回退到 sdist 源码构建后失败。v3t 镜像运行层不含编译工具链，
      没有 cp314t wheel 的原生扩展包会落到这里；纯 Python sdist 仍可正常构建，不会命中。
    调用方负责只在 free-threaded 运行时使用本函数，标准解释器下的构建失败另有原因。
    """
    if any(hint in message for hint in _FREE_THREADED_ABI_HINTS):
        hints = [
            line.strip()
            for line in message.splitlines()
            if line.strip().startswith("hint:") and "free-thread" in line
        ]
        reason = "依赖不兼容 free-threaded 运行时（v3t）：存在没有 free-threaded（cp314t）wheel 的依赖"
        return f"{reason}（{' '.join(hints)}）" if hints else reason
    matched = _UV_BUILD_FAILURE_PATTERN.search(message)
    if matched:
        return (
            f"依赖 {matched.group(1)} 需要从源码构建但构建失败：v3t 镜像不含编译工具链，"
            f"该依赖通常缺少 free-threaded（cp314t）wheel"
        )
    return None
