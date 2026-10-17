"""
为架构测试缓存源码扫描和语法树，避免同一进程反复全量读取。

依赖基线和事件事实对每个调用方返回深拷贝；语法树和节点序列直接共享，调用方必须只读。
缓存不会感知源码修改，修改扫描输入后需清除对应缓存或使用新的测试进程。
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
    """
    在当前测试进程中收集并缓存宿主依赖基线。

    :return: 共享原始结果，仅供外层复制，不应由测试直接修改
    """
    return collect_dependency_baseline()


@functools.cache
def _current_event_facts() -> dict[str, list[dict[str, Any]]]:
    """
    在当前测试进程中收集并缓存宿主事件事实。

    :return: 共享原始结果，仅供外层复制，不应由测试直接修改
    """
    return collect_current_event_facts()


def dependency_baseline() -> dict[str, Any]:
    """
    取得宿主依赖基线的独立副本。

    :return: 缓存基线的深拷贝；调用方修改不会影响后续测试
    """
    return copy.deepcopy(_dependency_baseline())


def current_event_facts() -> dict[str, list[dict[str, Any]]]:
    """
    取得宿主事件事实的独立副本。

    :return: 缓存事实的深拷贝；调用方修改不会影响后续测试
    """
    return copy.deepcopy(_current_event_facts())


@functools.cache
def parse_module(path: Path) -> ast.Module:
    """
    按文件路径解析并缓存 Python 语法树。

    同一路径的内容更新不会自动刷新缓存。

    :param path: 待扫描的源码路径；文件内容按可带 BOM 的 UTF-8 读取
    :return: 共享语法树；调用方不得修改节点
    """
    return ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))


@functools.cache
def walk_module(tree: ast.Module) -> tuple[ast.AST, ...]:
    """
    缓存语法树全部节点，供架构测试重复只读遍历。

    :param tree: 已解析的模块语法树
    :return: 按 ast.walk 顺序排列的节点元组，节点仍与原树共享
    """
    return tuple(ast.walk(tree))
