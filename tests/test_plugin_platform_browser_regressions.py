"""
用隔离子进程验证浏览器并发容量淘汰不会互相等待。
"""

import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from app.adapters.network.browser import BrowserSessionHelper
from tests.test_browser_helper import _FakeContext


def _concurrent_capacity_case():
    """
    两个会话同时达到淘汰点，保留真实会话线程和关闭线程归属。
    """
    BrowserSessionHelper.MAX_SESSIONS = 1
    helper = BrowserSessionHelper()
    original = BrowserSessionHelper._enforce_session_limit.__func__
    capacity_barrier = threading.Barrier(2)
    contexts = []

    def launch(**_kwargs):
        """
        仅替换真实浏览器资源边界，并记录创建线程。
        """
        context = _FakeContext()
        contexts.append((context, threading.get_ident()))
        return context

    def enforce(cls, protect_session_key=None):
        """
        固定两个新增会话互相进入容量淘汰的竞争窗口。
        """
        capacity_barrier.wait(5)
        return original(cls, protect_session_key)

    try:
        with patch.object(helper, "_launch_context", side_effect=launch), patch.object(
            BrowserSessionHelper, "_enforce_session_limit", classmethod(enforce),
        ), ThreadPoolExecutor(max_workers=2) as callers:
            futures = [callers.submit(helper.with_session, key, lambda session: session.session_key)
                       for key in ("one", "two")]
            assert {future.result(6) for future in futures} == {"one", "two"}
            assert len(BrowserSessionHelper._sessions) <= 1
    finally:
        BrowserSessionHelper.close_all_sessions()
    assert not BrowserSessionHelper._session_executors
    assert len(contexts) == 2
    assert all(context.closed and context.close_thread_id == thread_id
               for context, thread_id in contexts)
    print(json.dumps({"completed": True, "contexts_closed_on_owner_thread": 2}))


def test_parallel_session_capacity_eviction_converges_in_subprocess(tmp_path):
    """
    真正死锁由父进程超时终止，红测不会把不可回收线程留在pytest进程中。
    """
    env = dict(os.environ, CONFIG_DIR=str(tmp_path / "config"), DEBUG="false")
    script = (
        "from app.testing.bootstrap import prepare_backend; prepare_backend(); "
        "from tests.test_plugin_platform_browser_regressions import _concurrent_capacity_case; "
        "_concurrent_capacity_case()"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
        env=env, capture_output=True, text=True, timeout=12,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert '"completed": true' in result.stdout
