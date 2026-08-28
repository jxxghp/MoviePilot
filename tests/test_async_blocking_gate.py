"""async 阻塞调用 ratchet 与 debug 模式测试。"""

import asyncio
import textwrap
from pathlib import Path

import pytest

from scripts.architecture.async_blocking import (
    SCAN_ROOTS,
    collect_async_blocking,
    compare_async_blocking,
)


def _scan_source(
    tmp_path: Path,
    source: str,
    *,
    oper_sources: dict[str, str] | None = None,
) -> dict[str, int]:
    """构造最小仓库并通过公开扫描入口验证源码，而非测试内部 AST 细节。"""
    api_root = tmp_path / "app/api"
    api_root.mkdir(parents=True)
    (api_root / "sample.py").write_text(textwrap.dedent(source), encoding="utf-8")
    for filename, oper_source in (oper_sources or {}).items():
        oper_path = tmp_path / "app/db/oper" / filename
        oper_path.parent.mkdir(parents=True, exist_ok=True)
        oper_path.write_text(textwrap.dedent(oper_source), encoding="utf-8")
    return collect_async_blocking(tmp_path, scan_roots=("app/api",))


def test_async_blocking_scan_covers_runtime_entrypoints() -> None:
    """扫描范围必须覆盖全部 canonical 宿主目录和顶层运行入口。"""
    assert {
        "app/adapters",
        "app/api",
        "app/agent",
        "app/application",
        "app/chain",
        "app/db",
        "app/doctor",
        "app/domain",
        "app/foundation",
        "app/monitor",
        "app/modules",
        "app/runtime",
        "app/schemas",
        "app/startup",
        "app/workflow",
        "app/cli.py",
        "app/command.py",
        "app/factory.py",
        "app/main.py",
        "app/scheduler",
    }.issubset({str(path) for path in SCAN_ROOTS})


def test_async_blocking_ratchet_allows_removal_and_rejects_growth() -> None:
    """存量减少合法，新增或增加直接阻塞调用必须失败。"""
    baseline = {
        "app/api/a.py:stream:Path.read_text": 2,
        "app/agent/a.py:run:time.sleep": 1,
    }
    reduced = {"app/api/a.py:stream:Path.read_text": 1}
    increased = {
        "app/api/a.py:stream:Path.read_text": 3,
        "app/application/a.py:load:requests.get": 1,
    }

    assert compare_async_blocking(baseline, reduced) == []
    problems = compare_async_blocking(baseline, increased)
    assert any("增长" in problem for problem in problems)
    assert any("新增" in problem for problem in problems)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            """
            from app.adapters.network.http import RequestUtils as RU

            async def load():
                client = RU()
                alias = client
                return alias.get_res("https://example.com")
            """,
            {"app/api/sample.py:load:RequestUtils.get_res": 1},
        ),
        (
            """
            import app.adapters.network.http as http

            async def submit():
                return http.RequestUtils().post_res("https://example.com")
            """,
            {"app/api/sample.py:submit:RequestUtils.post_res": 1},
        ),
        (
            """
            from pathlib import Path as P

            async def inspect_file():
                path = P("payload") / "item.json"
                return path.exists()
            """,
            {"app/api/sample.py:inspect_file:Path.exists": 1},
        ),
        (
            """
            import shutil as files
            from subprocess import run as run_process

            async def cleanup():
                files.rmtree("payload")
                run_process(["true"])
            """,
            {
                "app/api/sample.py:cleanup:shutil.rmtree": 1,
                "app/api/sample.py:cleanup:subprocess.run": 1,
            },
        ),
        (
            """
            import os as operating_system
            import time as clock
            from requests import Session as HttpSession

            async def legacy_io():
                open("payload")
                operating_system.listdir(".")
                clock.sleep(0.1)
                HttpSession().get("https://example.com")
            """,
            {
                "app/api/sample.py:legacy_io:Session.get": 1,
                "app/api/sample.py:legacy_io:open": 1,
                "app/api/sample.py:legacy_io:os.listdir": 1,
                "app/api/sample.py:legacy_io:time.sleep": 1,
            },
        ),
        (
            """
            from pathlib import Path

            async def outer():
                async def inner():
                    return Path("payload").read_text()
                return await inner()
            """,
            {"app/api/sample.py:outer.inner:Path.read_text": 1},
        ),
        (
            """
            from pathlib import Path

            async def inspect_files(files: list[Path]):
                for file in files:
                    if file.is_file():
                        return file
                return None
            """,
            {"app/api/sample.py:inspect_files:Path.is_file": 1},
        ),
    ],
)
def test_async_blocking_scan_resolves_imports_aliases_and_nested_async(
    tmp_path: Path,
    source: str,
    expected: dict[str, int],
) -> None:
    """别名、简单局部传播和嵌套 async 都必须进入真实扫描结果。"""
    assert _scan_source(tmp_path, source) == expected


def test_async_blocking_scan_uses_oper_source_method_kinds(tmp_path: Path) -> None:
    """Oper 仅按源码中真实同步方法报告，不依赖方法名前缀猜测。"""
    source = """
        from app.db.oper import SiteOper
        from app.db.oper.site import SiteOper as SO

        async def load(oper: SO):
            oper.list()
            return await oper.get_by_id(1)

        async def load_from_facade():
            return SiteOper().list()
    """
    oper_sources = {
        "site.py": """
            class SiteOper:
                def list(self):
                    return []

                async def get_by_id(self, site_id):
                    return None
        """,
    }

    assert _scan_source(tmp_path, source, oper_sources=oper_sources) == {
        "app/api/sample.py:load:SiteOper.list": 1,
        "app/api/sample.py:load_from_facade:SiteOper.list": 1,
    }


def test_async_blocking_scan_exempts_async_apis_and_memory_reads(
    tmp_path: Path,
) -> None:
    """异步实现、受控 worker 与内存配置读取不得成为阻塞债务。"""
    source = """
        import asyncio
        import anyio
        import subprocess
        from anyio import Path as AsyncPath
        from app.agent.tools.base import run_agent_blocking
        from app.adapters.network.http import AsyncRequestUtils
        from app.db.oper.systemconfig import SystemConfigOper
        from app.runtime.execution import run_in_threadpool as runtime_worker
        from fastapi.concurrency import run_in_threadpool

        async def load(config: SystemConfigOper):
            await AsyncRequestUtils().get_res("https://example.com")
            await AsyncPath("payload").exists()
            await run_in_threadpool(lambda: subprocess.run(["true"]))
            await runtime_worker(lambda: subprocess.run(["true"]))
            await run_agent_blocking("plugin", lambda: subprocess.run(["true"]))
            await asyncio.to_thread(lambda: subprocess.run(["true"]))
            await anyio.to_thread.run_sync(lambda: subprocess.run(["true"]))
            deferred = lambda: subprocess.run(["true"])
            assert deferred
            return config.get("key")

        def ordinary_sync():
            subprocess.run(["true"])
    """
    oper_sources = {
        "systemconfig.py": """
            class SystemConfigOper:
                def get(self, key):
                    return key
        """,
    }

    assert _scan_source(tmp_path, source, oper_sources=oper_sources) == {}


def test_async_blocking_scan_checks_worker_arguments_evaluated_on_loop(
    tmp_path: Path,
) -> None:
    """worker 调用前同步求值的普通参数仍在事件循环中执行。"""
    source = """
        import asyncio
        from pathlib import Path

        async def load():
            await asyncio.to_thread(print, Path("payload").read_text())
    """

    assert _scan_source(tmp_path, source) == {
        "app/api/sample.py:load:Path.read_text": 1,
    }


def test_async_blocking_scan_merges_branch_bindings_conservatively(
    tmp_path: Path,
) -> None:
    """任一互斥分支可能产生同步对象时，合流调用仍属于阻塞风险。"""
    source = """
        from anyio import Path as AsyncPath
        from pathlib import Path

        async def load(use_sync: bool):
            if use_sync:
                target = Path("payload")
            else:
                target = AsyncPath("payload")
            return target.read_text()
    """

    assert _scan_source(tmp_path, source) == {
        "app/api/sample.py:load:Path.read_text": 1,
    }


def test_nested_async_inherits_bindings_from_definition_scope(tmp_path: Path) -> None:
    """嵌套 async 使用定义点已有的局部 import 和别名。"""
    source = """
        async def outer():
            from pathlib import Path as LocalPath
            alias = LocalPath

            async def inner():
                return alias("payload").read_text()

            return await inner()
    """

    assert _scan_source(tmp_path, source) == {
        "app/api/sample.py:outer.inner:Path.read_text": 1,
    }


def test_definition_time_expressions_remain_in_async_execution_body(
    tmp_path: Path,
) -> None:
    """延迟函数体不扫描，但默认值和 decorator 在定义时立即求值。"""
    source = """
        import asyncio
        from pathlib import Path

        def register(value):
            return lambda function: function

        async def outer():
            await asyncio.to_thread(
                lambda value=Path("lambda").read_text(): value
            )

            @register(Path("decorator").read_text())
            async def inner(value=Path("default").read_text()):
                return value

            return await inner()
    """

    assert _scan_source(tmp_path, source) == {
        "app/api/sample.py:outer:Path.read_text": 3,
    }


def test_local_shadowing_does_not_reuse_import_or_builtin_bindings(
    tmp_path: Path,
) -> None:
    """参数和 comprehension target 会遮蔽同名 builtin 或导入符号。"""
    source = """
        from pathlib import Path

        async def invoke(open, items):
            open()
            return [Path.exists() for Path in items]
    """

    assert _scan_source(tmp_path, source) == {}


@pytest.mark.asyncio
async def test_asyncio_debug_is_enabled_for_async_tests() -> None:
    """专项异步测试必须启用慢 callback 和阻塞诊断所需的 debug 模式。"""
    assert asyncio.get_running_loop().get_debug() is True
