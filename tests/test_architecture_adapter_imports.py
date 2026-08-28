"""Application/Chain 到 Adapter 原始直连事实的收集契约。"""

import json
from collections import Counter
from pathlib import Path

from scripts.architecture.baseline import (
    collect_dependency_baseline,
    collect_direct_adapter_imports,
)

PROJECT_ROOT = Path(__file__).parents[1]
DEPENDENCY_POLICY_PATH = (
    PROJECT_ROOT / "tests" / "fixtures" / "architecture" / "dependency-policy.json"
)
DIRECT_ADAPTER_SOURCE_ROOTS = ("app.application", "app.chain")
DIRECT_ADAPTER_TARGET_ROOT = "app.adapters"
DIRECT_ADAPTER_SCOPE: dict[str, object] = {
    "source_roots": list(DIRECT_ADAPTER_SOURCE_ROOTS),
    "target_root": DIRECT_ADAPTER_TARGET_ROOT,
    "runtime_only": True,
    "parent_package_expansion": False,
    "imported_symbols": False,
}
FROZEN_DIRECT_ADAPTER_IMPORTS = {
    ("app.application.backup", "app.adapters.system.backup.files"): "S2-L5",
    ("app.application.directory", "app.adapters.system.host"): "S2-L6",
    ("app.application.image", "app.adapters.network.http"): "S2-L6",
    ("app.application.image", "app.adapters.network.ip"): "S2-L6",
    ("app.application.messaging.ingress", "app.adapters.network.http"): "S2-L6",
    ("app.application.rss", "app.adapters.network.browser"): "S2-L6",
    ("app.application.rss", "app.adapters.network.http"): "S2-L6",
    ("app.application.rss", "app.adapters.system"): "S2-L6",
    ("app.application.rules", "app.adapters.system"): "S2-L6",
    ("app.application.security.cookie", "app.adapters.external.ocr"): "S2-L6",
    ("app.application.security.cookie", "app.adapters.network.browser"): "S2-L6",
    ("app.application.security.cookie", "app.adapters.network.http"): "S2-L6",
    ("app.application.torrent", "app.adapters.network.http"): "S2-L6",
    ("app.application.transfer.workflow", "app.adapters.system.host"): "S2-L6",
    ("app.chain._recognition", "app.adapters.external.server"): "S2-L7",
    ("app.chain._transfer", "app.adapters.system.host"): "S2-L7",
    ("app.chain.download", "app.adapters.network.http"): "S2-L7",
    ("app.chain.download", "app.adapters.system.host"): "S2-L7",
    ("app.chain.message", "app.adapters.network.http"): "S2-L7",
    ("app.chain.scraping", "app.adapters.network.http"): "S2-L7",
    ("app.chain.site", "app.adapters.external.cookiecloud"): "S2-L7",
    ("app.chain.site", "app.adapters.network.browser"): "S2-L7",
    ("app.chain.site", "app.adapters.network.cloudflare"): "S2-L7",
    ("app.chain.site", "app.adapters.network.http"): "S2-L7",
    ("app.chain.subscribe", "app.adapters.external.server"): "S2-L7",
    ("app.chain.system", "app.adapters.network.http"): "S2-L7",
    ("app.chain.system", "app.adapters.system.host"): "S2-L7",
}


def _source(tmp_path: Path, name: str, content: str) -> tuple[str, Path]:
    """创建一个供 AST collector 使用的独立源码文件。"""
    path = tmp_path / f"{name.replace('.', '_')}.py"
    path.write_text(content, encoding="utf-8")
    return name, path


def _adapter_policy_violations(
    actual: list[dict[str, str]],
    reviewed: list[dict[str, str]],
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """返回新增直连与已经消失但未清理的陈旧 policy。"""
    actual_edges = {(edge["source"], edge["target"]) for edge in actual}
    reviewed_edges = {(edge["source"], edge["target"]) for edge in reviewed}
    return sorted(actual_edges - reviewed_edges), sorted(reviewed_edges - actual_edges)


def _is_module_or_child(module_name: str, root: str) -> bool:
    """判断模块是否等于指定根或位于其点分子树内。"""
    return module_name == root or module_name.startswith(f"{root}.")


def _adapter_policy_entry_errors(entries: list[dict[str, str]]) -> list[str]:
    """校验 policy 条目结构、范围、唯一性和冻结 owner。"""
    errors: list[str] = []
    seen: set[tuple[str, str]] = set()
    for index, entry in enumerate(entries):
        if set(entry) != {"source", "target", "tracking"}:
            errors.append(f"entry[{index}]: fields")
            continue
        source = entry["source"]
        target = entry["target"]
        edge = (source, target)
        if edge in seen:
            errors.append(f"entry[{index}]: duplicate")
        seen.add(edge)
        if not any(
            _is_module_or_child(source, root)
            for root in DIRECT_ADAPTER_SOURCE_ROOTS
        ):
            errors.append(f"entry[{index}]: source scope")
        if not _is_module_or_child(target, DIRECT_ADAPTER_TARGET_ROOT):
            errors.append(f"entry[{index}]: target scope")
        if FROZEN_DIRECT_ADAPTER_IMPORTS.get(edge) != entry["tracking"]:
            errors.append(f"entry[{index}]: frozen edge or tracking")
    return errors


def _adapter_policy_scope_errors(
    policy_scope: dict[str, object],
    fact_scope: dict[str, object],
) -> list[str]:
    """要求人工 policy 与事实收集器共同锁定同一精确范围。"""
    if policy_scope == fact_scope == DIRECT_ADAPTER_SCOPE:
        return []
    return ["direct adapter scope drift"]


def test_direct_adapter_collector_preserves_raw_runtime_imports(
    tmp_path: Path,
) -> None:
    """原始模块只去重，不展开父包、猜测导入符号或纳入类型期依赖。"""
    modules = dict(
        [
            _source(
                tmp_path,
                "app.application.sample",
                """
from typing import TYPE_CHECKING
import typing
import app.adapters.network.http as http
from app.adapters.network.http import RequestUtils, AsyncRequestUtils
from app.adapters.system import rust as rust_accel
if TYPE_CHECKING:
    import app.adapters.network.browser
if typing.TYPE_CHECKING:
    import app.adapters.network.cloudflare
def load():
    from app.adapters.cache import redis
try:
    from app.adapters.external import ocr
except ImportError:
    pass
from app import adapters
from app.adapters.network import *
__import__("app.adapters.network.ip")
""",
            ),
            _source(
                tmp_path,
                "app.chain.sample",
                "import app.adapters.system.backup.files\n",
            ),
        ]
    )

    assert collect_direct_adapter_imports(modules) == [
        {"source": "app.application.sample", "target": "app.adapters"},
        {"source": "app.application.sample", "target": "app.adapters.cache"},
        {"source": "app.application.sample", "target": "app.adapters.external"},
        {"source": "app.application.sample", "target": "app.adapters.network"},
        {"source": "app.application.sample", "target": "app.adapters.network.http"},
        {"source": "app.application.sample", "target": "app.adapters.system"},
        {"source": "app.chain.sample", "target": "app.adapters.system.backup.files"},
    ]


def test_direct_adapter_collector_handles_relative_imports_and_scope(
    tmp_path: Path,
) -> None:
    """相对导入按 package 解析，伪前缀、DB Adapter 和非目标 source 必须排除。"""
    modules = dict(
        [
            _source(
                tmp_path,
                "app.application.feature.worker",
                "from ...adapters.network import ip\n",
            ),
            _source(
                tmp_path,
                "app.application.too_high",
                "from ....adapters.network import http\n",
            ),
            _source(
                tmp_path,
                "app.application.db",
                "import app.db.adapters.workflow\n",
            ),
            _source(
                tmp_path,
                "app.application.fake",
                "import app.adaptersx.network\n",
            ),
            _source(
                tmp_path,
                "app.applicationx.fake",
                "import app.adapters.network.http\n",
            ),
            _source(
                tmp_path,
                "app.api.fake",
                "import app.adapters.network.http\n",
            ),
            _source(
                tmp_path,
                "app.plugins.fake",
                "import app.adapters.network.http\n",
            ),
        ]
    )
    package_path = tmp_path / "__init__.py"
    package_path.write_text(
        "from ...adapters.cache import redis\n",
        encoding="utf-8",
    )
    modules["app.chain.feature"] = package_path

    assert collect_direct_adapter_imports(modules) == [
        {
            "source": "app.application.feature.worker",
            "target": "app.adapters.network",
        },
        {"source": "app.chain.feature", "target": "app.adapters.cache"},
    ]


def test_current_direct_adapter_imports_are_stable_generated_facts() -> None:
    """生成事实必须有稳定排序、自洽统计，且不保存符号或行号。"""
    contract = collect_dependency_baseline()["direct_adapter_imports"]
    edges = contract["edges"]
    sources = sorted({edge["source"] for edge in edges})
    targets = sorted({edge["target"] for edge in edges})
    counts_by_root = Counter(
        root
        for edge in edges
        for root in DIRECT_ADAPTER_SOURCE_ROOTS
        if _is_module_or_child(edge["source"], root)
    )
    expected_counts = {
        root: counts_by_root[root]
        for root in DIRECT_ADAPTER_SOURCE_ROOTS
    }

    assert edges == sorted(edges, key=lambda edge: (edge["source"], edge["target"]))
    assert contract["scope"] == DIRECT_ADAPTER_SCOPE
    assert contract["count"] == len(edges)
    assert contract["counts_by_source_root"] == expected_counts
    assert contract["sources"] == sources
    assert contract["source_count"] == len(sources)
    assert contract["targets"] == targets
    assert contract["target_count"] == len(targets)
    assert all(set(edge) == {"source", "target"} for edge in contract["edges"])


def test_current_direct_adapter_imports_match_temporary_debt_policy() -> None:
    """现存直连必须逐条绑定冻结 owner，并允许债务集合只减不增。"""
    policy = json.loads(DEPENDENCY_POLICY_PATH.read_text(encoding="utf-8"))
    contract = collect_dependency_baseline()["direct_adapter_imports"]
    adapter_policy = policy["direct_adapter_imports"]
    entries = adapter_policy["entries"]

    assert policy["schema_version"] == 3
    assert adapter_policy["classification"] == "temporary_debt"
    assert adapter_policy["target_state"] == "empty"
    assert _adapter_policy_scope_errors(adapter_policy["scope"], contract["scope"]) == []
    assert entries == sorted(entries, key=lambda item: (item["source"], item["target"]))
    assert Counter(FROZEN_DIRECT_ADAPTER_IMPORTS.values()) == {
        "S2-L5": 1,
        "S2-L6": 13,
        "S2-L7": 13,
    }
    assert _adapter_policy_entry_errors(entries) == []

    unreviewed, stale = _adapter_policy_violations(contract["edges"], entries)
    assert unreviewed == []
    assert stale == []


def test_adapter_policy_rejects_add_remove_and_replacement() -> None:
    """新增、删除后未清 policy、以及换成另一条边都不能静默通过。"""
    reviewed = [
        {
            "source": "app.application.security.passkey",
            "target": "app.adapters.cache.redis",
            "tracking": "S2-L4",
        }
    ]
    original = [
        {
            "source": "app.application.security.passkey",
            "target": "app.adapters.cache.redis",
        }
    ]
    added = [
        *original,
        {
            "source": "app.chain.sample",
            "target": "app.adapters.system.host",
        },
    ]
    replacement = [
        {
            "source": "app.application.sample",
            "target": "app.adapters.network.browser",
        }
    ]

    assert _adapter_policy_violations(original, reviewed) == ([], [])
    assert _adapter_policy_violations(added, reviewed) == (
        [("app.chain.sample", "app.adapters.system.host")],
        [],
    )
    assert _adapter_policy_violations([], reviewed) == (
        [],
        [("app.application.security.passkey", "app.adapters.cache.redis")],
    )
    assert _adapter_policy_violations(replacement, reviewed) == (
        [("app.application.sample", "app.adapters.network.browser")],
        [("app.application.security.passkey", "app.adapters.cache.redis")],
    )

    assert _adapter_policy_violations([], []) == ([], [])
    assert _adapter_policy_entry_errors([]) == []


def test_adapter_policy_rejects_manual_policy_bypasses() -> None:
    """手工 policy 也不能接纳新边、错 owner、重复项或越界范围。"""
    valid = {
        "source": "app.application.backup",
        "target": "app.adapters.system.backup.files",
        "tracking": "S2-L5",
    }
    invalid_entries = [
        {
            "source": "app.application.sample",
            "target": "app.adapters.network.http",
            "tracking": "S2-L6",
        },
        {
            "source": valid["source"],
            "target": "app.adapters.network.browser",
            "tracking": "S2-L5",
        },
        {**valid, "tracking": "S2-L6"},
        {
            "source": "app.api.sample",
            "target": valid["target"],
            "tracking": "S2-L5",
        },
        {
            "source": valid["source"],
            "target": "app.db.adapters.subscription",
            "tracking": "S2-L5",
        },
        {
            "source": "app.application.*",
            "target": "app.adapters.*",
            "tracking": "S2-L6",
        },
    ]

    assert _adapter_policy_entry_errors([valid]) == []
    assert all(_adapter_policy_entry_errors([entry]) for entry in invalid_entries)
    assert any("duplicate" in error for error in _adapter_policy_entry_errors([valid, valid]))
    drifted_scope = {**DIRECT_ADAPTER_SCOPE, "runtime_only": False}
    assert _adapter_policy_scope_errors(drifted_scope, DIRECT_ADAPTER_SCOPE) == [
        "direct adapter scope drift"
    ]
