# -*- coding: utf-8 -*-
"""
S5b 解耦回归测试：core/plugin 通过 plugin source seam 获取插件来源,
不再在模块顶层 import app.helper.plugin.PluginHelper。

验证：
  1. 未注入 provider 时 get_plugin_source() 懒加载返回真实 PluginHelper 单例（行为不变）;
  2. 注入 provider 后返回注入对象;
  3. core/plugin.py 不再顶层 import helper.plugin / 引用 PluginHelper;
  4. plugin_source seam 对 helper 的引用为惰性（函数内），模块顶层无 helper import。
"""
from pathlib import Path
from unittest import TestCase

import app.core.plugin_source as plugin_source
from app.core.plugin_source import get_plugin_source, set_plugin_source_provider


class CorePluginSourceSeamTest(TestCase):

    def tearDown(self) -> None:
        plugin_source._provider = None

    def test_default_returns_real_plugin_helper(self):
        """未注入 provider 时返回真实 PluginHelper 单例"""
        plugin_source._provider = None
        from app.helper.plugin import PluginHelper
        source = get_plugin_source()
        self.assertIsInstance(source, PluginHelper)

    def test_injected_provider_is_used(self):
        """注入 provider 后 get_plugin_source() 返回注入对象"""
        sentinel = object()
        set_plugin_source_provider(lambda: sentinel)
        self.assertIs(get_plugin_source(), sentinel)

    def test_core_plugin_no_top_level_helper_plugin_import(self):
        """app/core/plugin.py 不再顶层 import helper.plugin / 引用 PluginHelper"""
        import app.core.plugin as plugin
        source = Path(plugin.__file__).read_text(encoding="utf-8")
        for line in source.splitlines():
            stripped = line.strip()
            self.assertFalse(
                stripped.startswith("from app.helper.plugin") or stripped.startswith("import app.helper.plugin"),
                f"core/plugin.py 仍有 helper.plugin 导入: {line}",
            )
        self.assertNotIn("PluginHelper", source)
        self.assertIn("from app.core.plugin_source import get_plugin_source", source)

    def test_seam_has_no_top_level_helper_import(self):
        """plugin_source seam 对 helper 的引用为惰性（缩进在函数内），顶层无 helper import"""
        source = Path(plugin_source.__file__).read_text(encoding="utf-8")
        for line in source.splitlines():
            # 顶层 import 不缩进；惰性 import 在函数体内有缩进
            if line.startswith("from app.helper") or line.startswith("import app.helper"):
                self.fail(f"seam 存在顶层 helper import: {line}")
        # 惰性 import 确实存在（缩进形式）
        self.assertIn("from app.helper.plugin import PluginHelper", source)
