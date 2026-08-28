"""识别当前进程已经加载、但磁盘载荷在依赖安装中发生变化的原生发行包。"""

from __future__ import annotations

import ctypes
import os
import re
import sys
from dataclasses import dataclass
from importlib.metadata import Distribution, PackageNotFoundError, distribution, distributions
from pathlib import Path
from typing import Iterable

from packaging.utils import canonicalize_name

from app.runtime.log import logger

_NATIVE_FILE_PATTERN = re.compile(
    r"(?:\.pyd|\.dll|\.dylib|\.so(?:\.\d+)*)$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class NativeArtifactState:
    """原生文件在一次依赖安装边界上的轻量磁盘指纹。"""

    path: str
    size: int
    modified_ns: int
    file_id: tuple[int, int]


@dataclass(frozen=True, slots=True)
class NativeDistributionState:
    """当前已加载发行包及其全部原生载荷的磁盘状态。"""

    name: str
    version: str
    artifacts: tuple[NativeArtifactState, ...]


@dataclass(frozen=True, slots=True)
class LoadedNativeDependencySnapshot:
    """依赖安装前，当前进程已加载原生发行包的稳定快照。"""

    distributions: tuple[NativeDistributionState, ...] = ()


@dataclass(frozen=True, slots=True)
class NativeDependencyChange:
    """一个已加载发行包在磁盘上被替换的原生载荷摘要。"""

    distribution: str
    previous_version: str
    current_version: str | None
    artifacts: tuple[str, ...]


def capture_loaded_native_dependencies() -> LoadedNativeDependencySnapshot:
    """捕获当前进程已加载原生文件所属发行包的磁盘状态。"""
    loaded_paths = _loaded_native_paths()
    if not loaded_paths:
        return LoadedNativeDependencySnapshot()

    states = []
    for installed_distribution in _iter_installed_distributions():
        state = _distribution_state(installed_distribution)
        if state is None:
            continue
        if loaded_paths.intersection(_path_key(item.path) for item in state.artifacts):
            states.append(state)
    return LoadedNativeDependencySnapshot(
        distributions=tuple(sorted(states, key=lambda item: item.name)),
    )


def detect_changed_native_dependencies(
    baseline: LoadedNativeDependencySnapshot,
) -> tuple[NativeDependencyChange, ...]:
    """比较安装后的磁盘状态，只报告基线中已经加载的原生发行包。"""
    changes = []
    for previous in baseline.distributions:
        current = _current_distribution_state(previous.name)
        previous_artifacts = {item.path: item for item in previous.artifacts}
        current_artifacts = (
            {item.path: item for item in current.artifacts}
            if current is not None
            else {}
        )
        changed_paths = tuple(
            sorted(
                Path(path).name
                for path in previous_artifacts.keys() | current_artifacts.keys()
                if previous_artifacts.get(path) != current_artifacts.get(path)
            )
        )
        if not changed_paths:
            continue
        changes.append(
            NativeDependencyChange(
                distribution=previous.name,
                previous_version=previous.version,
                current_version=current.version if current is not None else None,
                artifacts=changed_paths,
            )
        )
    return tuple(changes)


def _iter_installed_distributions() -> Iterable[Distribution]:
    """隔离发行包枚举，便于在测试中构造确定性文件布局。"""
    return distributions()


def _current_distribution_state(name: str) -> NativeDistributionState | None:
    """读取安装后的同名发行包；卸载完成时返回空状态。"""
    try:
        installed_distribution = distribution(name)
    except PackageNotFoundError:
        return None
    return _distribution_state(installed_distribution)


def _distribution_state(
    installed_distribution: Distribution,
) -> NativeDistributionState | None:
    """读取一个发行包的全部原生文件，避免漏掉扩展旁加载的本地库。"""
    distribution_name = installed_distribution.metadata.get("Name")
    if not distribution_name:
        return None
    artifacts = []
    for relative_path in installed_distribution.files or ():
        if not _is_native_file(str(relative_path)):
            continue
        path = Path(installed_distribution.locate_file(relative_path))
        state = _artifact_state(path)
        if state is not None:
            artifacts.append(state)
    if not artifacts:
        return None
    return NativeDistributionState(
        name=canonicalize_name(distribution_name),
        version=installed_distribution.version,
        artifacts=tuple(sorted(artifacts, key=lambda item: item.path)),
    )


def _artifact_state(path: Path) -> NativeArtifactState | None:
    """读取文件身份而不散列大型本地库，避免插件安装前产生明显 I/O。"""
    try:
        stat = path.stat()
    except OSError:
        return None
    return NativeArtifactState(
        path=_path_key(path),
        size=stat.st_size,
        modified_ns=stat.st_mtime_ns,
        file_id=(stat.st_dev, stat.st_ino),
    )


def _loaded_native_paths() -> set[str]:
    """合并 Python 扩展模块和平台加载器可见的原生文件路径。"""
    paths = {
        _path_key(module_file)
        for module in tuple(sys.modules.values())
        if (module_file := getattr(module, "__file__", None))
        and _is_native_file(module_file)
    }
    for path in _platform_loaded_library_paths():
        if _is_native_file(path):
            paths.add(_path_key(path))
    return paths


def _platform_loaded_library_paths() -> set[str]:
    """读取当前进程映射的本地库；失败时由 Python 扩展模块路径继续兜底。"""
    try:
        if sys.platform.startswith("linux"):
            return _linux_loaded_library_paths()
        if sys.platform == "darwin":
            return _macos_loaded_library_paths()
        if sys.platform == "win32":
            return _windows_loaded_library_paths()
    except (OSError, RuntimeError, ValueError) as error:
        logger.debug("读取当前进程原生库映射失败：%s", error)
    return set()


def _linux_loaded_library_paths() -> set[str]:
    """从 procfs 读取 Linux 当前进程的文件映射。"""
    paths = set()
    with Path("/proc/self/maps").open(encoding="utf-8", errors="replace") as maps:
        for line in maps:
            fields = line.rstrip().split(maxsplit=5)
            if len(fields) != 6 or not fields[5].startswith("/"):
                continue
            paths.add(fields[5].removesuffix(" (deleted)"))
    return paths


def _macos_loaded_library_paths() -> set[str]:
    """通过 dyld 查询 macOS 当前进程已装载镜像。"""
    process = ctypes.CDLL(None)
    image_count = process._dyld_image_count
    image_count.argtypes = []
    image_count.restype = ctypes.c_uint32
    image_name = process._dyld_get_image_name
    image_name.argtypes = [ctypes.c_uint32]
    image_name.restype = ctypes.c_char_p
    return {
        os.fsdecode(path)
        for index in range(image_count())
        if (path := image_name(index))
    }


def _windows_loaded_library_paths() -> set[str]:
    """通过 PSAPI 查询 Windows 当前进程已装载模块。"""
    from ctypes import wintypes

    process = ctypes.windll.kernel32.GetCurrentProcess()
    module_count = 256
    while True:
        modules = (wintypes.HMODULE * module_count)()
        needed = wintypes.DWORD()
        if not ctypes.windll.psapi.EnumProcessModulesEx(
            process,
            modules,
            ctypes.sizeof(modules),
            ctypes.byref(needed),
            0x03,
        ):
            raise OSError(ctypes.get_last_error(), "EnumProcessModulesEx failed")
        required_count = needed.value // ctypes.sizeof(wintypes.HMODULE)
        if required_count <= module_count:
            break
        module_count = required_count

    paths = set()
    for module in modules[:required_count]:
        buffer = ctypes.create_unicode_buffer(32768)
        length = ctypes.windll.psapi.GetModuleFileNameExW(
            process,
            module,
            buffer,
            len(buffer),
        )
        if length:
            paths.add(buffer.value)
    return paths


def _is_native_file(path: str) -> bool:
    """识别 Python 扩展及其常见旁加载本地库。"""
    return bool(_NATIVE_FILE_PATTERN.search(path))


def _path_key(path: str | os.PathLike[str]) -> str:
    """把不同平台的等价路径归一到可比较键。"""
    return os.path.normcase(os.path.realpath(os.fspath(path)))
