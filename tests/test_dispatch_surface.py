"""app/application 分发调用面的静态门禁。

六个多来源能力契约（match_media、person_detail/person_credits、media_credits、
media_recommend/media_similar、discover/discover_board、media_detail）把数据源降为
`source` 参数后，分发方法名不应再以数据源为前缀——那正是「源 × 实体 × 类型」笛卡尔展开
的成文化。本文件用 AST 静态扫描全部分发调用点的方法名字面量，防止该问题回潮。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).parents[1]
APPLICATION_ROOT = PROJECT_ROOT / "app" / "application"

# 分发原语：字符串方法名经这些调用抵达运行期能力索引。
_DISPATCH_METHOD_NAMES = frozenset({
    "unicast", "async_unicast",
    "multicast", "async_multicast",
    "broadcast", "async_broadcast",
    "pipeline", "async_pipeline",
    "run_module", "async_run_module",
})

_SOURCE_PREFIXES = ("tmdb_", "douban_", "bangumi_", "anilist_", "tvdb_")
_BANNED_PREFIXES = _SOURCE_PREFIXES + tuple(f"async_{prefix}" for prefix in _SOURCE_PREFIXES)

# 单一来源专有访问器：TMDB 合集/季/剧集组/识别缓存管理是 TMDB API 原生结构，
# TVDB slug 是 TVDB 专有的详情页别名，两者都不存在跨源等价物，不属于
# 「源 × 实体 × 类型」笛卡尔展开问题。分发方法名与 app/modules 侧实现方法名一一对应，
# 改名需两侧同步，超出本文件门禁的调整范围。
_SINGLE_SOURCE_ACCESSOR_ALLOWLIST = frozenset({
    "tmdb_collection", "async_tmdb_collection",
    "tmdb_seasons", "async_tmdb_seasons",
    "tmdb_group_seasons", "async_tmdb_group_seasons",
    "tmdb_episodes", "async_tmdb_episodes",
    "tmdb_cache_items", "tmdb_cache_delete", "tmdb_cache_clear",
    "tvdb_slug",
})


def _iter_dispatch_calls(path: Path):
    """产出文件内全部「分发原语调用且首个位置参数为字符串字面量」的记录。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in _DISPATCH_METHOD_NAMES:
            continue
        if not node.args:
            continue
        first_arg = node.args[0]
        if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
            yield path, node.lineno, func.attr, first_arg.value


def _all_dispatch_calls() -> list[tuple[Path, int, str, str]]:
    calls: list[tuple[Path, int, str, str]] = []
    for path in sorted(APPLICATION_ROOT.rglob("*.py")):
        calls.extend(_iter_dispatch_calls(path))
    return calls


@pytest.fixture(scope="module")
def dispatch_calls() -> list[tuple[Path, int, str, str]]:
    return _all_dispatch_calls()


def test_dispatch_surface_has_no_source_prefixed_method_names(dispatch_calls) -> None:
    """分发方法名不得以数据源前缀开头，防止「源编进方法名」问题回潮。"""
    violations = [
        (path, lineno, attr, method)
        for path, lineno, attr, method in dispatch_calls
        if method.startswith(_BANNED_PREFIXES) and method not in _SINGLE_SOURCE_ACCESSOR_ALLOWLIST
    ]

    assert not violations, "发现源前缀编码进分发方法名的调用点：\n" + "\n".join(
        f"{path.relative_to(PROJECT_ROOT)}:{lineno}: {attr}(\"{method}\", ...)"
        for path, lineno, attr, method in violations
    )


def test_single_source_accessor_allowlist_has_no_stale_entries(dispatch_calls) -> None:
    """允许清单条目须仍在分发面出现，避免清单腐化为无引用的僵尸例外。"""
    dispatched_methods = {method for _, _, _, method in dispatch_calls}
    stale = _SINGLE_SOURCE_ACCESSOR_ALLOWLIST - dispatched_methods

    assert not stale, f"允许清单中存在已不再被分发的方法名，应当清理：{sorted(stale)}"
