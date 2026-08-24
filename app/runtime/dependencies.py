"""按解释器 ABI 选择主程序运行依赖 profile。"""

from __future__ import annotations

import tomllib
from collections.abc import Iterable
from pathlib import Path

from app.foundation.environment import is_free_threaded_runtime


RUNTIME_STANDARD_GROUP = "runtime-standard"
RUNTIME_FREE_THREADED_GROUP = "runtime-free-threaded"


def runtime_dependency_group() -> str:
    """返回当前解释器必须使用的互斥运行依赖组。"""
    if is_free_threaded_runtime():
        return RUNTIME_FREE_THREADED_GROUP
    return RUNTIME_STANDARD_GROUP


def runtime_sync_arguments() -> tuple[str, ...]:
    """返回 uv sync 选择当前运行依赖组所需的稳定参数。"""
    return "--no-default-groups", "--group", runtime_dependency_group()


def iter_runtime_requirement_strings(project_file: Path) -> Iterable[str]:
    """读取主项目依赖及当前运行 profile 的根依赖声明。"""
    with project_file.open("rb") as file:
        document = tomllib.load(file)

    project = document.get("project") or {}
    for requirement in project.get("dependencies") or ():
        if isinstance(requirement, str):
            yield requirement

    groups = document.get("dependency-groups") or {}
    for requirement in groups.get(runtime_dependency_group()) or ():
        if isinstance(requirement, str):
            yield requirement


def iter_runtime_profile_requirement_strings(project_file: Path) -> Iterable[str]:
    """读取当前解释器 profile 的根依赖声明。"""
    with project_file.open("rb") as file:
        document = tomllib.load(file)

    groups = document.get("dependency-groups") or {}
    for requirement in groups.get(runtime_dependency_group()) or ():
        if isinstance(requirement, str):
            yield requirement


if __name__ == "__main__":
    print(runtime_dependency_group())
