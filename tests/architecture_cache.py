"""架构测试共享的全量扫描结果：同一 pytest 进程内每类扫描只执行一次。

门禁把多个架构测试文件放在同一进程运行，这些文件会对同一份源码重复做相同的全量收集。
缓存只存放原始结果，每次返回深拷贝，测试修改返回值不会影响其他用例。
"""

from __future__ import annotations

import ast
import copy
import functools
from pathlib import Path
from typing import Any

from scripts.architecture.baseline import collect_current_event_facts, collect_dependency_baseline


@functools.cache
def _dependency_baseline() -> dict[str, Any]:
    return collect_dependency_baseline()


@functools.cache
def _current_event_facts() -> dict[str, list[dict[str, Any]]]:
    return collect_current_event_facts()


def dependency_baseline() -> dict[str, Any]:
    """返回当前宿主依赖基线的独立副本。"""
    return copy.deepcopy(_dependency_baseline())


def current_event_facts() -> dict[str, list[dict[str, Any]]]:
    """返回当前宿主事件事实的独立副本。"""
    return copy.deepcopy(_current_event_facts())


@functools.cache
def parse_module(path: Path) -> ast.Module:
    """解析并缓存源码 AST；调用方只读取，不得修改返回的节点。"""
    return ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))


@functools.cache
def walk_module(tree: ast.Module) -> tuple[ast.AST, ...]:
    """按 ``ast.walk`` 顺序返回模块全部节点，供多个测试重复遍历同一只读 AST。"""
    return tuple(ast.walk(tree))
