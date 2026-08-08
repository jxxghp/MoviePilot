# -*- coding: utf-8 -*-
"""
S3-module 解耦回归测试：ModuleHelper 从 app.helper.module 迁移到 app.core.module_loader。

验证：
  1. 旧路径 app.helper.module 仍可导入，且与新路径 app.core.module_loader 指向同一个类
     （含插件 app.plugins.autosignin 使用的导入路径，保证插件零改动）；
  2. ModuleHelper 迁移后功能正常（对标准库包做一次动态加载冒烟）；
  3. core 层不再反向依赖 helper：
     - app/core/module.py 不再 import app.helper.module；
     - app/core/module_loader.py 自身不依赖 app.helper。
"""
from pathlib import Path
from unittest import TestCase

import app.core.module_loader as new_mod
import app.helper.module as shim_mod
from app.core.module_loader import ModuleHelper as CoreModuleHelper
from app.helper.module import ModuleHelper as ShimModuleHelper


class CoreModuleRelocationTest(TestCase):

    def test_shim_reexports_same_class(self):
        """旧路径(垫片)与新路径解析到同一个类对象——插件 from app.helper.module import ModuleHelper 零改动可用"""
        self.assertIs(ShimModuleHelper, CoreModuleHelper)
        # 模拟插件 autosignin 的导入路径
        from app.helper.module import ModuleHelper as PluginPathHelper
        self.assertIs(PluginPathHelper, CoreModuleHelper)
        # FilterFuncType 类型别名同样经垫片导出
        self.assertIs(shim_mod.FilterFuncType, new_mod.FilterFuncType)

    def test_module_helper_load_is_functional(self):
        """迁移后的 ModuleHelper.load 仍能动态加载并收集类（用标准库 json 包做冒烟，无 app 依赖）"""
        classes = CoreModuleHelper.load("json")
        self.assertIsInstance(classes, list)
        names = {c.__name__ for c in classes}
        # json 包内含 JSONDecoder / JSONEncoder 等类
        self.assertIn("JSONDecoder", names)

    def test_core_module_no_longer_imports_helper_module(self):
        """app/core/module.py 不再 import app.helper.module（已切到 app.core.module_loader）"""
        import app.core.module as module_mgr
        source = Path(module_mgr.__file__).read_text(encoding="utf-8")
        self.assertNotIn("from app.helper.module", source)
        self.assertNotIn("import app.helper.module", source)

    def test_core_module_loader_has_no_helper_dependency(self):
        """app/core/module_loader.py 自身不依赖 app.helper（方向正确：core 不反向依赖 helper）"""
        source = Path(new_mod.__file__).read_text(encoding="utf-8")
        self.assertNotIn("app.helper", source)
