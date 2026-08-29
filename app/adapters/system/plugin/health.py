"""插件依赖安装后的运行环境准入、健康检查与自动修复。"""

from __future__ import annotations

import asyncio
import importlib
import re
import site
import sys
import tempfile
import threading
from collections import deque
from importlib.metadata import distributions
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from packaging.markers import default_environment
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from app.adapters.system.host import SystemUtils
from app.adapters.system.package import (
    PackageInstallRequest,
    build_package_install_strategies,
    build_project_sync_strategies,
    find_uv,
)
from app.adapters.system.plugin.manifest import load_dependency_file
from app.runtime.dependencies.profile import (
    iter_runtime_profile_requirement_strings,
    iter_runtime_requirement_strings,
    runtime_excluded_dependency_pairs,
)
from app.runtime.execution import await_task_to_terminal
from app.runtime.execution import (
    run_in_threadpool_to_completion as _await_thread_operation,
)
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting


class PluginRuntimeHealth:
    """负责插件依赖写入的运行环境保护、检查和故障恢复。"""

    _package_install_lock = threading.Lock()
    _protected_runtime_packages = frozenset({
        "alembic", "fastapi", "pydantic", "pydantic_core",
        "pydantic_settings", "sqlalchemy", "starlette", "uvicorn",
    })
    _runtime_import_probe = "app.doctor.dependencies"
    PLUGIN_DEPENDENCY_INSTALL_TIMEOUT = 300

    @classmethod
    def __get_installed_packages(cls) -> Dict[str, Version]:
        """
        获取已安装的包及其版本
        使用 importlib.metadata 获取当前环境中已安装的包，标准化包名并转换版本信息
        对于无法解析的版本，记录警告日志并跳过
        :return: 已安装包的字典，格式为 {package_name: Version}
        """
        installed_packages: Dict[str, Version] = {}
        try:
            for dist in distributions():
                name = dist.metadata.get("Name")
                if not name:
                    continue
                pkg_name = cls.__standardize_pkg_name(name)
                version_str = dist.metadata.get("Version") or getattr(dist, "version", None)
                if not version_str:
                    continue
                try:
                    v = Version(version_str)
                    if pkg_name not in installed_packages or v > installed_packages[pkg_name]:
                        installed_packages[pkg_name] = v
                except InvalidVersion:
                    logger.debug(f"无法解析已安装包 '{pkg_name}' 的版本：{version_str}")
                    continue
            return installed_packages
        except Exception as e:
            logger.error(f"获取已安装的包时发生错误：{e}")
            return {}

    @staticmethod
    def __standardize_pkg_name(name: str) -> str:
        """
        标准化包名，将包名转换为小写，连字符与点替换为下划线（与 PEP 503 归一化风格一致）

        :param name: 原始包名
        :return: 标准化后的包名
        """
        if not name:
            return name
        return name.lower().replace("-", "_").replace(".", "_")

    @staticmethod
    def __build_runtime_uv_check_command() -> List[str]:
        """构造绑定当前解释器环境的 uv 依赖诊断命令。"""
        uv_bin = find_uv(Path(sys.executable))
        if not uv_bin:
            return []
        return [str(uv_bin), "pip", "check", "--python", sys.executable]

    @staticmethod
    def __format_package_name(name: str) -> str:
        """将内部包名转换为依赖清单常用的连字符形式。"""
        return name.replace("_", "-")

    @staticmethod
    def __marker_matches(marker: Any, extra: str = "") -> bool:
        """
        使用当前运行环境和可选 extra 上下文判断 marker 是否生效。
        """
        if not marker:
            return True
        try:
            env = default_environment()
            env["extra"] = extra
            return bool(marker.evaluate(env))
        except Exception as err:
            logger.debug(f"依赖 marker 计算失败，按不匹配处理：{err}")
            return False

    @classmethod
    def __parse_project_requirement_roots(
            cls,
            project_file: Path,
    ) -> Dict[str, Set[str]]:
        """解析主项目 pyproject，收集当前平台生效的根依赖和 extras。"""
        roots: Dict[str, Set[str]] = {}
        if not project_file.exists():
            logger.warning(f"主项目依赖文件不存在：{project_file}")
            return roots

        try:
            for raw_requirement in iter_runtime_requirement_strings(project_file):
                requirement = Requirement(raw_requirement)
                if not cls.__marker_matches(requirement.marker):
                    continue
                package_name = cls.__standardize_pkg_name(requirement.name)
                roots.setdefault(package_name, set()).update(
                    extra.lower() for extra in requirement.extras
                )
            return roots
        except Exception as e:
            logger.error(f"解析主项目依赖文件失败：{project_file} - {e}")
            return {}

    @classmethod
    def __get_installed_distribution_requirements(cls) -> Dict[str, Tuple[Version, List[Requirement]]]:
        """
        获取当前环境中每个已安装包的依赖声明，用于展开主程序依赖图。
        """
        requirement_graph: Dict[str, Tuple[Version, List[Requirement]]] = {}
        try:
            for dist in distributions():
                name = dist.metadata.get("Name")
                if not name:
                    continue

                package_name = cls.__standardize_pkg_name(name)
                version_str = dist.metadata.get("Version") or getattr(dist, "version", None)
                if not version_str:
                    continue

                try:
                    version = Version(version_str)
                except InvalidVersion:
                    logger.debug(f"无法解析已安装包 '{package_name}' 的版本：{version_str}")
                    continue

                requirements = []
                for raw_requirement in dist.requires or []:
                    try:
                        requirements.append(Requirement(raw_requirement))
                    except Exception as err:
                        logger.debug(f"无法解析已安装包 '{package_name}' 的依赖项 '{raw_requirement}'：{err}")

                if package_name not in requirement_graph or version > requirement_graph[package_name][0]:
                    requirement_graph[package_name] = (version, requirements)
            return requirement_graph
        except Exception as e:
            logger.error(f"收集已安装包依赖图时发生错误：{e}")
            return {}

    @classmethod
    def __get_protected_runtime_packages(
            cls,
            installed_packages: Optional[Dict[str, Version]] = None
    ) -> Dict[str, Version]:
        """
        仅收集主程序依赖图中的已安装包版本。

        主项目 pyproject 中声明的根依赖及其当前已安装的传递依赖都会被冻结，
        未被主程序依赖图引用的插件自带包允许后续插件按需升级或降级。
        """
        if installed_packages is None:
            installed_packages = cls.__get_installed_packages()
        protected_packages = {
            package_name: version
            for package_name, version in installed_packages.items()
            if package_name in cls._protected_runtime_packages
        }

        project_file = get_runtime_setting('ROOT_PATH') / "pyproject.toml"
        root_requirements = cls.__parse_project_requirement_roots(project_file)
        if not root_requirements:
            return protected_packages

        requirement_graph = cls.__get_installed_distribution_requirements()
        active_extras = {
            package_name: set(extras)
            for package_name, extras in root_requirements.items()
        }
        pending_packages = deque(active_extras.keys())
        processed_extras: Dict[str, Set[str]] = {}

        while pending_packages:
            package_name = pending_packages.popleft()
            selected_extras = active_extras.get(package_name, set())
            previous_extras = processed_extras.get(package_name)
            if previous_extras is not None and selected_extras.issubset(previous_extras):
                continue

            processed_extras[package_name] = set(selected_extras)
            if package_name in installed_packages:
                protected_packages[package_name] = installed_packages[package_name]

            _, requirements = requirement_graph.get(package_name, (None, []))
            if not requirements:
                continue

            active_extra_values = [""] + sorted(selected_extras)
            for requirement in requirements:
                if requirement.marker and not any(
                        cls.__marker_matches(requirement.marker, extra)
                        for extra in active_extra_values
                ):
                    continue

                dep_name = cls.__standardize_pkg_name(requirement.name)
                known_extras = active_extras.setdefault(dep_name, set())
                before_len = len(known_extras)
                known_extras.update(extra.lower() for extra in requirement.extras)
                if dep_name not in processed_extras or len(known_extras) != before_len:
                    pending_packages.append(dep_name)

        return protected_packages

    @classmethod
    def __get_strict_runtime_packages(cls) -> Set[str]:
        """返回核心包及当前 ABI profile 中不得被插件改写的根包。"""
        packages = set(cls._protected_runtime_packages)
        project_file = get_runtime_setting('ROOT_PATH') / "pyproject.toml"
        try:
            for raw_requirement in iter_runtime_profile_requirement_strings(project_file):
                requirement = Requirement(raw_requirement)
                if cls.__marker_matches(requirement.marker):
                    packages.add(cls.__standardize_pkg_name(requirement.name))
        except Exception as error:
            logger.error(f"解析运行依赖 profile 失败：{project_file} - {error}")
        return packages

    @staticmethod
    def __is_upgrade_only_conflict(specifier_set: SpecifierSet, installed_version: Version) -> bool:
        """
        判断版本冲突是否只能通过升级来解决（specifier 允许的所有版本都严格高于已安装版本）。
        返回 True 表示纯升级冲突；返回 False 表示可能需要降级或无法确定方向。
        """
        has_lower_bound = False
        for spec in specifier_set:
            op = spec.operator
            ver_str = spec.version.rstrip("*").rstrip(".") or "0"
            try:
                ver = Version(ver_str)
            except InvalidVersion:
                return False

            if op in ("<", "<="):
                upper = ver if op == "<" else Version(f"{ver}.post0")
                if upper <= installed_version:
                    return False
            elif op == "==":
                if ver <= installed_version:
                    return False
            elif op == "~=":
                # ~=X.Y.Z 等价于 >=X.Y.Z, <X.(Y+1)；若 X.Y.Z <= 已安装版本说明需降级
                if ver <= installed_version:
                    return False
                has_lower_bound = True
            elif op in (">=", ">"):
                has_lower_bound = True
            # != 操作符：单独出现时可能允许低版本，需结合其他约束判断

        # 若没有任何明确的下限约束（仅 != 等），保守地视为不确定 → 返回 False
        return has_lower_bound

    @classmethod
    def __validate_runtime_dependency_conflicts(
            cls,
            dependency_file: Path,
            protected_packages: Dict[str, Version]
    ) -> Tuple[bool, str]:
        """
        在真正执行安装前，先拦截插件对主程序依赖的显式覆盖请求。

        共享 venv 场景下，仅冻结主程序依赖；插件新增依赖、以及插件之间共享的额外依赖，
        允许后续安装继续调整版本。
        """
        conflicts = []
        strict_packages = cls.__get_strict_runtime_packages()
        try:
            manifest = load_dependency_file(dependency_file)
            for requirement in manifest.dependencies:
                if not cls.__marker_matches(requirement.marker):
                    continue

                package_name = cls.__standardize_pkg_name(requirement.name)
                installed_version = protected_packages.get(package_name)
                if installed_version is None:
                    continue

                if requirement.url:
                    conflicts.append((
                        package_name,
                        str(installed_version),
                        f"来自 {requirement.url} 的同名包",
                        package_name in strict_packages,
                    ))
                    continue

                if requirement.specifier and not requirement.specifier.contains(
                        installed_version,
                        prereleases=True
                ):
                    is_core = package_name in strict_packages
                    # 非核心包的纯升级冲突允许放行，由安装约束控制实际版本。
                    if is_core or not cls.__is_upgrade_only_conflict(
                            requirement.specifier, installed_version):
                        conflicts.append((
                            package_name,
                            str(installed_version),
                            str(requirement.specifier),
                            is_core,
                        ))
        except Exception as e:
            logger.error(f"执行运行环境依赖冲突预检时发生错误：{e}")
            return False, f"插件依赖预检失败：{e}"

        if not conflicts:
            return True, ""

        def sort_key(item: Tuple[str, str, str, bool]) -> Tuple[int, str]:
            return 0 if item[3] else 1, item[0]

        details = []
        for package_name, installed_version, expected, _is_protected in sorted(conflicts, key=sort_key)[:5]:
            details.append(
                f"{cls.__format_package_name(package_name)} 当前为 {installed_version}，"
                f"插件要求 {expected}"
            )
        if len(conflicts) > 5:
            details.append(f"其余 {len(conflicts) - 5} 项冲突已省略")

        scope = "主程序核心依赖" if any(item[3] for item in conflicts) else "主程序依赖"
        return False, (
            f"插件依赖与当前运行环境的{scope}冲突：{'；'.join(details)}。"
            f"为避免共享运行环境被污染，已拒绝安装。"
        )

    @classmethod
    def __create_runtime_constraints_file(cls, protected_packages: Dict[str, Version]) -> Path:
        """
        以主程序依赖的当前已安装版本生成临时约束文件，确保插件安装不会改写主程序依赖。
        """
        temp_dir = Path(get_runtime_setting('TEMP_PATH')) / "plugin_dependencies"
        temp_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=temp_dir,
                prefix="runtime-constraints-",
                suffix=".txt",
                delete=False
        ) as temp_file:
            strict_packages = cls.__get_strict_runtime_packages()
            for package_name, version in sorted(protected_packages.items()):
                if package_name in strict_packages:
                    # 核心与 ABI profile 根包严格锁定，插件不得改写
                    temp_file.write(f"{cls.__format_package_name(package_name)}=={version}\n")
                else:
                    # 非核心主程序依赖：允许升级，但禁止降级
                    temp_file.write(f"{cls.__format_package_name(package_name)}>={version}\n")
        return Path(temp_file.name)

    @classmethod
    async def __async_create_runtime_constraints_file(
            cls,
            protected_packages: Dict[str, Version],
    ) -> Path:
        """创建临时约束文件，取消时等待创建收口并删除已产生的文件。"""
        create_task = asyncio.create_task(
            asyncio.to_thread(
                cls.__create_runtime_constraints_file,
                protected_packages,
            )
        )
        try:
            return await asyncio.shield(create_task)
        except asyncio.CancelledError:
            async def cleanup_created_file() -> None:
                try:
                    created_file = await create_task
                except BaseException:
                    return
                await asyncio.to_thread(created_file.unlink, missing_ok=True)

            cleanup_task = asyncio.create_task(cleanup_created_file())
            try:
                await await_task_to_terminal(cleanup_task)
            except Exception as err:
                logger.warning(f"[UV] 取消后清理运行环境约束文件失败：{err}")
            raise

    @staticmethod
    def __refresh_import_system() -> None:
        """
        依赖安装或修复后刷新当前解释器的导入缓存，保证后续动态导入能看到新状态。
        """
        importlib.reload(site)
        importlib.invalidate_caches()

    @classmethod
    def __build_package_install_request(
            cls,
            dependency_files: Path | Sequence[Path],
            find_links_dirs: Optional[List[Path]] = None,
            constraints_file: Optional[Path] = None,
            purpose: str = "plugin",
    ) -> PackageInstallRequest:
        """
        将 MoviePilot 运行配置转换为 uv 安装请求，统一缓存、镜像和代理语义。
        """
        resolved_dependency_files: tuple[Path, ...]
        if isinstance(dependency_files, Path):
            resolved_dependency_files = (dependency_files,)
        else:
            resolved_dependency_files = tuple(Path(item) for item in dependency_files)
        return PackageInstallRequest(
            dependency_files=resolved_dependency_files,
            python_bin=Path(sys.executable),
            find_links_dirs=find_links_dirs or [],
            constraints_file=constraints_file,
            config_dir=get_runtime_setting('CONFIG_PATH'),
            package_cache_root=get_runtime_setting('PACKAGE_CACHE_PATH'),
            package_index_url=get_runtime_setting('PIP_PROXY') or None,
            proxy_url=get_runtime_setting('PROXY_HOST') or None,
            purpose=purpose,
        )

    @classmethod
    def __repair_if_runtime_broken(
            cls,
            snapshot_file: Optional[Path] = None,
            baseline_health: Optional[Dict[str, Tuple[bool, str]]] = None
    ) -> Tuple[bool, str]:
        """
        安装失败后检查主运行环境；若相对安装前新增异常，先恢复主程序依赖再返回。
        """
        current_health = cls.__run_runtime_healthcheck()
        health_message = cls.__runtime_health_regression_message(
            baseline_health or {},
            current_health
        )
        if not health_message:
            return True, ""
        repair_ok, repair_message = cls.__repair_main_runtime_dependencies(snapshot_file)
        if not repair_ok:
            return False, f"插件依赖安装失败后主运行环境异常，且恢复失败：{health_message}; {repair_message}"
        restored_health = cls.__run_runtime_healthcheck()
        restored_message = cls.__runtime_health_regression_message(
            baseline_health or {},
            restored_health
        )
        if restored_message:
            return False, f"插件依赖安装失败后主运行环境异常，恢复后仍异常：{restored_message}"
        return True, "主运行环境已恢复"

    @classmethod
    def __run_runtime_healthcheck(cls) -> Dict[str, Tuple[bool, str]]:
        """
        执行全部运行环境自检并返回逐项结果，避免前一项失败遮蔽后续异常。
        """
        health_snapshot = {}
        uv_check = cls.__build_runtime_uv_check_command()
        if uv_check:
            checks = [("uv check", uv_check)]
        else:
            health_snapshot["uv check"] = (False, "未找到 uv 可执行文件")
            checks = []
        checks.append(("核心依赖导入检查", [
            sys.executable,
            "-m",
            cls._runtime_import_probe,
            "--full",
        ]))
        for check_name, command in checks:
            success, message = SystemUtils.execute_with_subprocess(command)
            health_snapshot[check_name] = (success, message)
        return health_snapshot

    @staticmethod
    def __runtime_health_error_lines(check_name: str, message: str) -> set[str]:
        """提取未被项目依赖策略排除的稳定诊断项。"""
        lines = {line.strip() for line in message.splitlines() if line.strip()}
        if check_name != "uv check":
            return lines

        matches = list(re.finditer(
            r"The package `(?P<package>[^`]+)` requires `(?P<requirement>[^`]+)`, "
            r"but [^\r\n;]+",
            message,
        ))
        if not matches:
            return lines

        excluded_pairs = runtime_excluded_dependency_pairs(
            Path(get_runtime_setting('ROOT_PATH')) / "pyproject.toml"
        )
        package_errors = set()
        for match in matches:
            try:
                dependency_name = Requirement(match.group("requirement")).name
            except InvalidRequirement:
                package_errors.add(match.group(0))
                continue
            pair = (
                canonicalize_name(match.group("package")),
                canonicalize_name(dependency_name),
            )
            if pair not in excluded_pairs:
                package_errors.add(match.group(0))
        return package_errors

    @classmethod
    def __runtime_health_regression_message(
            cls,
            baseline_health: Dict[str, Tuple[bool, str]],
            current_health: Dict[str, Tuple[bool, str]],
    ) -> str:
        """
        汇总相对基线新增的异常；已有诊断失败不能遮蔽后续新增错误。
        """
        regressions = []
        for check_name, (success, message) in current_health.items():
            baseline_success, baseline_message = baseline_health.get(check_name, (True, ""))
            if baseline_success and not success:
                current_lines = cls.__runtime_health_error_lines(
                    check_name,
                    message,
                )
                if current_lines:
                    regressions.append(
                        f"{check_name}失败：{' | '.join(sorted(current_lines))}"
                    )
            elif not baseline_success and not success:
                baseline_lines = cls.__runtime_health_error_lines(
                    check_name,
                    baseline_message,
                )
                current_lines = cls.__runtime_health_error_lines(
                    check_name,
                    message,
                )
                added_lines = sorted(current_lines - baseline_lines)
                if added_lines:
                    regressions.append(f"{check_name}新增错误：{' | '.join(added_lines)}")
        return "；".join(regressions)

    @classmethod
    def __repair_main_runtime_dependencies(cls, snapshot_file: Optional[Path] = None) -> Tuple[bool, str]:
        """
        依赖安装后如果发现主运行环境已异常，优先恢复主程序依赖快照；
        若快照不可用，再按主项目锁定依赖恢复运行环境。
        """
        repair_target = snapshot_file
        repair_desc = "主程序依赖快照"
        if repair_target and not repair_target.exists():
            repair_target = None
        if repair_target is None:
            repair_target = get_runtime_setting('ROOT_PATH') / "pyproject.toml"
            repair_desc = "主程序 uv.lock"
        if not repair_target.exists():
            return False, f"恢复依赖文件不存在：{repair_target}"
        if snapshot_file is None and not (get_runtime_setting('ROOT_PATH') / "uv.lock").exists():
            return False, f"恢复依赖文件不存在：{get_runtime_setting('ROOT_PATH') / 'uv.lock'}"

        last_error = ""
        request = cls.__build_package_install_request(repair_target, purpose="runtime-repair")
        strategies = (
            build_package_install_strategies(request)
            if snapshot_file is not None
            else build_project_sync_strategies(request)
        )
        for strategy in strategies:
            logger.warning(f"[UV] 运行环境异常，尝试使用策略：{strategy.strategy_name} 恢复{repair_desc}")
            success, message = SystemUtils.execute_with_subprocess(
                strategy.command,
                env=strategy.env,
                safe_command=strategy.safe_log_command,
            )
            if success:
                cls.__refresh_import_system()
                return True, message
            last_error = message
            logger.error(f"[UV] 使用策略：{strategy.strategy_name} 恢复{repair_desc}失败：{message}")
        return False, last_error or f"恢复{repair_desc}失败"

    @classmethod
    def install_packages_with_fallback(cls,
                                       dependency_files: Path | Sequence[Path],
                                       find_links_dirs: Optional[List[Path]] = None) -> Tuple[bool, str]:
        """
        使用自动降级策略安装依赖，并确保新安装的包可被动态导入
        :param dependency_files: 一个或多个插件依赖清单路径
        :param find_links_dirs: 额外的本地 wheels 目录列表
        :return: (是否成功, 错误信息)
        """
        resolved_dependency_files: tuple[Path, ...]
        if isinstance(dependency_files, Path):
            resolved_dependency_files = (dependency_files,)
        else:
            resolved_dependency_files = tuple(Path(item) for item in dependency_files)
        if not resolved_dependency_files:
            return False, "没有传入插件依赖清单"

        candidate_dirs = []
        for dependency_file in resolved_dependency_files:
            wheels_dir = dependency_file.parent / "wheels"
            if wheels_dir.is_dir():
                candidate_dirs.append(wheels_dir)
        if find_links_dirs:
            candidate_dirs.extend(find_links_dirs)

        # 去重并保持传入顺序
        resolved_dirs = []
        seen_dirs = set()
        for candidate_dir in candidate_dirs:
            candidate_path = Path(candidate_dir)
            if not candidate_path.is_dir():
                continue
            candidate_key = str(candidate_path.resolve())
            if candidate_key in seen_dirs:
                continue
            seen_dirs.add(candidate_key)
            resolved_dirs.append(candidate_path)

        if resolved_dirs:
            for local_wheels_dir in resolved_dirs:
                logger.debug(f"[UV] 发现可用的 wheels 目录: {local_wheels_dir}，将优先从本地安装。")
        else:
            logger.debug("[UV] 未发现可用的 wheels 目录，将仅使用在线源。")

        installed_packages = cls.__get_installed_packages()
        protected_packages = cls.__get_protected_runtime_packages(installed_packages)
        for dependency_file in resolved_dependency_files:
            check_ok, check_message = cls.__validate_runtime_dependency_conflicts(
                dependency_file,
                protected_packages,
            )
            if not check_ok:
                logger.error(f"[UV] 运行环境冲突预检失败：{check_message}")
                return False, check_message

        constraints_file = None
        if protected_packages:
            try:
                constraints_file = cls.__create_runtime_constraints_file(protected_packages)
            except Exception as e:
                logger.error(f"[UV] 创建运行环境约束文件失败：{e}")
                return False, f"创建运行环境约束文件失败：{e}"

        request = cls.__build_package_install_request(
            resolved_dependency_files,
            find_links_dirs=resolved_dirs,
            constraints_file=constraints_file,
            purpose="plugin",
        )
        strategies = build_package_install_strategies(request)

        try:
            # 安装器会修改当前解释器的 site-packages，安装与缓存刷新必须串行。
            with cls._package_install_lock:
                loaded_modules_before_install = set(sys.modules.keys())
                baseline_health = cls.__run_runtime_healthcheck()
                baseline_health_message = cls.__runtime_health_regression_message({}, baseline_health)
                if baseline_health_message:
                    logger.warning(
                        f"[UV] 安装前运行环境已存在异常，本次安装仅拦截新增异常：{baseline_health_message}"
                    )
                # 遍历策略进行安装
                last_error = ""
                for strategy in strategies:
                    logger.debug(
                        f"[UV] 尝试使用策略：{strategy.strategy_name} 安装依赖，"
                        f"命令：{' '.join(strategy.safe_log_command)}"
                    )
                    success, message = SystemUtils.execute_with_subprocess(
                        strategy.command,
                        env=strategy.env,
                        safe_command=strategy.safe_log_command,
                    )
                    if success:
                        logger.debug(f"[UV] 策略：{strategy.strategy_name} 安装依赖成功，输出：{message}")
                        current_health = cls.__run_runtime_healthcheck()
                        health_message = cls.__runtime_health_regression_message(
                            baseline_health,
                            current_health
                        )
                        if health_message:
                            logger.error(f"[UV] 依赖安装后运行环境自检失败：{health_message}")
                            repair_ok, repair_message = cls.__repair_main_runtime_dependencies(
                                constraints_file if protected_packages else None
                            )
                            if repair_ok:
                                restored_health = cls.__run_runtime_healthcheck()
                                restored_message = cls.__runtime_health_regression_message(
                                    baseline_health,
                                    restored_health
                                )
                                if not restored_message:
                                    cls.__refresh_import_system()
                                    return False, (
                                        f"依赖安装后运行环境自检失败，已自动恢复主程序依赖：{health_message}"
                                    )
                                logger.error(
                                    f"[UV] 主程序依赖恢复后仍未通过健康检查：{restored_message}"
                                )
                                return False, (
                                    f"依赖安装后运行环境自检失败，恢复主程序依赖后仍异常："
                                    f"{restored_message}"
                                )
                            return False, (
                                f"依赖安装后运行环境自检失败，且自动恢复主程序依赖失败："
                                f"{repair_message}"
                            )

                        remaining_health_message = cls.__runtime_health_regression_message({}, current_health)
                        if remaining_health_message:
                            logger.warning(
                                f"[UV] 依赖安装成功，安装前已有的运行环境异常仍然存在："
                                f"{remaining_health_message}"
                            )

                        cls.__refresh_import_system()
                        loaded_modules_after_install = set(sys.modules.keys())
                        loaded_modules_during_install = loaded_modules_after_install - loaded_modules_before_install
                        logger.debug(f"[UV] 已刷新导入系统，新加载的模块: {loaded_modules_during_install}")
                        return True, message

                    last_error = message
                    repair_ok, repair_message = cls.__repair_if_runtime_broken(
                        constraints_file if protected_packages else None,
                        baseline_health
                    )
                    logger.error(f"[UV] 策略：{strategy.strategy_name} 安装依赖失败，错误信息：{message}")
                    if not repair_ok or repair_message:
                        return False, (
                            f"策略 {strategy.strategy_name} 安装依赖失败：{message}；"
                            f"{repair_message}"
                        )
        finally:
            if constraints_file:
                constraints_file.unlink(missing_ok=True)

        if last_error:
            return False, f"[UV] 所有策略均安装依赖失败：{last_error}"
        return False, "[UV] 所有策略均安装依赖失败，请检查网络连接、包源配置或插件依赖约束"

    @classmethod
    async def __async_run_runtime_healthcheck(cls) -> Dict[str, Tuple[bool, str]]:
        """异步执行插件安装后的运行环境检查。"""
        health_snapshot: Dict[str, Tuple[bool, str]] = {}
        uv_check = cls.__build_runtime_uv_check_command()
        if uv_check:
            checks = [("uv check", uv_check)]
        else:
            health_snapshot["uv check"] = (False, "未找到 uv 可执行文件")
            checks = []
        checks.append(("核心依赖导入检查", [
            sys.executable,
            "-m",
            cls._runtime_import_probe,
            "--full",
        ]))
        for check_name, command in checks:
            health_snapshot[check_name] = (
                await SystemUtils.execute_with_subprocess_async(
                    command,
                    timeout=30,
                )
            )
        return health_snapshot

    @classmethod
    async def __async_repair_main_runtime_dependencies(
            cls,
            snapshot_file: Optional[Path] = None,
    ) -> Tuple[bool, str]:
        """异步恢复主程序运行依赖，避免修复命令绕过可取消进程边界。"""
        repair_target = snapshot_file
        repair_desc = "主程序依赖快照"
        if repair_target and not await _await_thread_operation(repair_target.exists):
            repair_target = None
        if repair_target is None:
            repair_target = get_runtime_setting('ROOT_PATH') / "pyproject.toml"
            repair_desc = "主程序 uv.lock"
        if not await _await_thread_operation(repair_target.exists):
            return False, f"恢复依赖文件不存在：{repair_target}"
        lock_file = get_runtime_setting('ROOT_PATH') / "uv.lock"
        if snapshot_file is None and not await _await_thread_operation(lock_file.exists):
            return False, f"恢复依赖文件不存在：{get_runtime_setting('ROOT_PATH') / 'uv.lock'}"

        request = cls.__build_package_install_request(
            repair_target,
            purpose="runtime-repair",
        )
        strategies = (
            build_package_install_strategies(request)
            if snapshot_file is not None
            else build_project_sync_strategies(request)
        )
        last_error = ""
        for strategy in strategies:
            logger.warning(
                f"[UV] 运行环境异常，尝试使用策略：{strategy.strategy_name} 恢复{repair_desc}"
            )
            success, message = await SystemUtils.execute_with_subprocess_async(
                strategy.command,
                env=strategy.env,
                safe_command=strategy.safe_log_command,
                timeout=cls.PLUGIN_DEPENDENCY_INSTALL_TIMEOUT,
            )
            if success:
                cls.__refresh_import_system()
                return True, message
            last_error = message
            logger.error(
                f"[UV] 使用策略：{strategy.strategy_name} 恢复{repair_desc}失败：{message}"
            )
        return False, last_error or f"恢复{repair_desc}失败"

    @classmethod
    async def __async_repair_if_runtime_broken(
            cls,
            snapshot_file: Optional[Path],
            baseline_health: Dict[str, Tuple[bool, str]],
    ) -> Tuple[bool, str]:
        """异步检查并修复安装过程中新增的主程序环境异常。"""
        current_health = await cls.__async_run_runtime_healthcheck()
        health_message = cls.__runtime_health_regression_message(
            baseline_health,
            current_health,
        )
        if not health_message:
            return True, ""
        repair_ok, repair_message = (
            await cls.__async_repair_main_runtime_dependencies(snapshot_file)
        )
        if not repair_ok:
            return False, (
                f"插件依赖安装失败后主运行环境异常，且恢复失败："
                f"{health_message}; {repair_message}"
            )
        restored_health = await cls.__async_run_runtime_healthcheck()
        restored_message = cls.__runtime_health_regression_message(
            baseline_health,
            restored_health,
        )
        if restored_message:
            return False, (
                f"插件依赖安装失败后主运行环境异常，恢复后仍异常："
                f"{restored_message}"
            )
        return True, "主运行环境已恢复"

    async def async_install_packages_with_fallback(
            self,
            dependency_files: Path | Sequence[Path],
            find_links_dirs: Optional[List[Path]] = None,
    ) -> Tuple[bool, str]:
        """通过可取消子进程异步安装一组插件依赖清单。"""
        return await self.__async_install_packages_with_fallback(
            dependency_files,
            find_links_dirs,
        )

    @classmethod
    async def __async_install_packages_with_fallback(
            cls,
            dependency_files: Path | Sequence[Path],
            find_links_dirs: Optional[List[Path]] = None,
    ) -> Tuple[bool, str]:
        """异步安装插件依赖，并让取消能够终止 uv 子进程。"""
        resolved_dependency_files: tuple[Path, ...]
        if isinstance(dependency_files, Path):
            resolved_dependency_files = (dependency_files,)
        else:
            resolved_dependency_files = tuple(Path(item) for item in dependency_files)
        if not resolved_dependency_files:
            return False, "没有传入插件依赖清单"

        candidate_dirs = []
        for dependency_file in resolved_dependency_files:
            wheels_dir = dependency_file.parent / "wheels"
            if await _await_thread_operation(wheels_dir.is_dir):
                candidate_dirs.append(wheels_dir)
        if find_links_dirs:
            candidate_dirs.extend(find_links_dirs)

        resolved_dirs = []
        seen_dirs = set()
        for candidate_dir in candidate_dirs:
            candidate_path = Path(candidate_dir)
            if not await _await_thread_operation(candidate_path.is_dir):
                continue
            candidate_key = str(
                await _await_thread_operation(candidate_path.resolve)
            )
            if candidate_key in seen_dirs:
                continue
            seen_dirs.add(candidate_key)
            resolved_dirs.append(candidate_path)

        installed_packages = await _await_thread_operation(
            cls.__get_installed_packages,
        )
        protected_packages = await _await_thread_operation(
            cls.__get_protected_runtime_packages,
            installed_packages,
        )
        for dependency_file in resolved_dependency_files:
            check_ok, check_message = await _await_thread_operation(
                cls.__validate_runtime_dependency_conflicts,
                dependency_file,
                protected_packages,
            )
            if not check_ok:
                logger.error(f"[UV] 运行环境冲突预检失败：{check_message}")
                return False, check_message

        constraints_file = None
        if protected_packages:
            try:
                constraints_file = await cls.__async_create_runtime_constraints_file(
                    protected_packages,
                )
            except Exception as err:
                logger.error(f"[UV] 创建运行环境约束文件失败：{err}")
                return False, f"创建运行环境约束文件失败：{err}"

        request = cls.__build_package_install_request(
            resolved_dependency_files,
            find_links_dirs=resolved_dirs,
            constraints_file=constraints_file,
            purpose="plugin",
        )
        strategies = build_package_install_strategies(request)
        acquired = False
        try:
            while not cls._package_install_lock.acquire(blocking=False):
                await asyncio.sleep(0.01)
            acquired = True
            baseline_health = await cls.__async_run_runtime_healthcheck()
            baseline_health_message = cls.__runtime_health_regression_message(
                {},
                baseline_health,
            )
            if baseline_health_message:
                logger.warning(
                    f"[UV] 安装前运行环境已存在异常，本次安装仅拦截新增异常："
                    f"{baseline_health_message}"
                )

            last_error = ""
            for strategy in strategies:
                logger.debug(
                    f"[UV] 尝试使用策略：{strategy.strategy_name} 安装依赖，"
                    f"命令：{' '.join(strategy.safe_log_command)}"
                )
                success, message = await SystemUtils.execute_with_subprocess_async(
                    strategy.command,
                    env=strategy.env,
                    safe_command=strategy.safe_log_command,
                    timeout=cls.PLUGIN_DEPENDENCY_INSTALL_TIMEOUT,
                )
                if success:
                    current_health = await cls.__async_run_runtime_healthcheck()
                    health_message = cls.__runtime_health_regression_message(
                        baseline_health,
                        current_health,
                    )
                    if health_message:
                        logger.error(f"[UV] 依赖安装后运行环境自检失败：{health_message}")
                        repair_ok, repair_message = (
                            await cls.__async_repair_main_runtime_dependencies(
                                constraints_file if protected_packages else None
                            )
                        )
                        if repair_ok:
                            restored_health = await cls.__async_run_runtime_healthcheck()
                            restored_message = cls.__runtime_health_regression_message(
                                baseline_health,
                                restored_health,
                            )
                            if not restored_message:
                                cls.__refresh_import_system()
                                return False, (
                                    f"依赖安装后运行环境自检失败，已自动恢复主程序依赖："
                                    f"{health_message}"
                                )
                            return False, (
                                f"依赖安装后运行环境自检失败，恢复主程序依赖后仍异常："
                                f"{restored_message}"
                            )
                        return False, (
                            f"依赖安装后运行环境自检失败，且自动恢复主程序依赖失败："
                            f"{repair_message}"
                        )

                    cls.__refresh_import_system()
                    return True, message

                last_error = message
                repair_ok, repair_message = await cls.__async_repair_if_runtime_broken(
                    constraints_file if protected_packages else None,
                    baseline_health,
                )
                logger.error(
                    f"[UV] 策略：{strategy.strategy_name} 安装依赖失败，错误信息：{message}"
                )
                if not repair_ok or repair_message:
                    return False, (
                        f"策略 {strategy.strategy_name} 安装依赖失败：{message}；"
                        f"{repair_message}"
                    )
            return False, (
                f"[UV] 所有策略均安装依赖失败：{last_error}"
                if last_error
                else "[UV] 所有策略均安装依赖失败，请检查网络连接、包源配置或插件依赖约束"
            )
        finally:
            if acquired:
                cls._package_install_lock.release()
            if constraints_file:
                await _await_thread_operation(
                    constraints_file.unlink,
                    missing_ok=True,
                )
