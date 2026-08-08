# -*- coding: utf-8 -*-
"""
S1c 解耦回归测试：core/plugin 通过注入式 seam 上报插件安装,
不再直接 import app.helper.server.MoviePilotServerHelper。

验证：
  1. 未注册 reporter 时 report_plugin_install() 为 no-op,返回 None 且不抛异常;
  2. 注册 reporter 后以正确 kwargs 调用（组合根注入 MoviePilotServerHelper.install_plugin_reg）;
  3. core 层不再反向依赖 helper.server:
     - app/core/plugin.py 不再 import app.helper.server / 引用 MoviePilotServerHelper;
     - app/core/plugin_reporter.py 自身无 app.helper 依赖。
"""
from pathlib import Path
from unittest import TestCase

import app.core.plugin_reporter as plugin_reporter
from app.core.plugin_reporter import report_plugin_install, set_plugin_install_reporter


class CorePluginReporterSeamTest(TestCase):

    def tearDown(self) -> None:
        # 复位全局 reporter,避免测试间状态泄漏
        plugin_reporter._install_reporter = None

    def test_noop_when_no_reporter(self):
        """未注册 reporter 时为 no-op:返回 None 且不抛异常"""
        plugin_reporter._install_reporter = None
        self.assertIsNone(report_plugin_install(plugin_id="demo", repo_url="https://example.com/repo"))

    def test_injected_reporter_called_with_kwargs(self):
        """注册 reporter 后以正确 kwargs 调用并透传返回值"""
        calls = []

        def fake_reporter(plugin_id, repo_url=None):
            calls.append({"plugin_id": plugin_id, "repo_url": repo_url})
            return True

        set_plugin_install_reporter(fake_reporter)
        result = report_plugin_install(plugin_id="p1", repo_url="https://example.com/p1")
        self.assertTrue(result)
        self.assertEqual(calls, [{"plugin_id": "p1", "repo_url": "https://example.com/p1"}])

    def test_core_plugin_no_longer_imports_helper_server(self):
        """app/core/plugin.py 不再 import app.helper.server / 引用 MoviePilotServerHelper"""
        import app.core.plugin as plugin
        source = Path(plugin.__file__).read_text(encoding="utf-8")
        self.assertNotIn("from app.helper.server", source)
        self.assertNotIn("import app.helper.server", source)
        self.assertNotIn("MoviePilotServerHelper", source)

    def test_seam_module_has_no_helper_dependency(self):
        """app/core/plugin_reporter.py 自身不依赖 app.helper（方向正确）"""
        source = Path(plugin_reporter.__file__).read_text(encoding="utf-8")
        self.assertNotIn("app.helper", source)
