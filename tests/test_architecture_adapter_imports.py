"""Application/Chain 到 Adapter 原始直连事实的收集契约。"""

from pathlib import Path

from scripts.architecture.baseline import (
    collect_dependency_baseline,
    collect_direct_adapter_imports,
)

EXPECTED_DIRECT_ADAPTER_IMPORTS = {
    ("app.application.backup", "app.adapters.system.backup.files"),
    ("app.application.directory", "app.adapters.system.host"),
    ("app.application.image", "app.adapters.network.http"),
    ("app.application.image", "app.adapters.network.ip"),
    ("app.application.messaging.ingress", "app.adapters.network.http"),
    ("app.application.rss", "app.adapters.network.browser"),
    ("app.application.rss", "app.adapters.network.http"),
    ("app.application.rss", "app.adapters.system"),
    ("app.application.rules", "app.adapters.system"),
    ("app.application.security.cookie", "app.adapters.external.ocr"),
    ("app.application.security.cookie", "app.adapters.network.browser"),
    ("app.application.security.cookie", "app.adapters.network.http"),
    ("app.application.security.passkey", "app.adapters.cache.redis"),
    ("app.application.torrent", "app.adapters.network.http"),
    ("app.application.transfer", "app.adapters.system.host"),
    ("app.chain._recognition", "app.adapters.external.server"),
    ("app.chain._transfer", "app.adapters.system.host"),
    ("app.chain.download", "app.adapters.network.http"),
    ("app.chain.download", "app.adapters.system.host"),
    ("app.chain.message", "app.adapters.network.http"),
    ("app.chain.scraping", "app.adapters.network.http"),
    ("app.chain.site", "app.adapters.external.cookiecloud"),
    ("app.chain.site", "app.adapters.network.browser"),
    ("app.chain.site", "app.adapters.network.cloudflare"),
    ("app.chain.site", "app.adapters.network.http"),
    ("app.chain.subscribe", "app.adapters.external.server"),
    ("app.chain.system", "app.adapters.network.http"),
    ("app.chain.system", "app.adapters.system.host"),
}


def _source(tmp_path: Path, name: str, content: str) -> tuple[str, Path]:
    """创建一个供 AST collector 使用的独立源码文件。"""
    path = tmp_path / f"{name.replace('.', '_')}.py"
    path.write_text(content, encoding="utf-8")
    return name, path


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
    """当前 28 条直连必须完整进入生成事实，且不保存符号或行号。"""
    contract = collect_dependency_baseline()["direct_adapter_imports"]
    edges = {
        (edge["source"], edge["target"])
        for edge in contract["edges"]
    }

    assert contract["count"] == 28
    assert contract["counts_by_source_root"] == {
        "app.application": 15,
        "app.chain": 13,
    }
    assert contract["source_count"] == 18
    assert contract["target_count"] == 11
    assert edges == EXPECTED_DIRECT_ADAPTER_IMPORTS
    assert all(set(edge) == {"source", "target"} for edge in contract["edges"])
