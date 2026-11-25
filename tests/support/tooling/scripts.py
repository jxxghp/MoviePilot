"""
开发技能脚本的隔离加载；不注册测试用例。
"""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _load_script(relative, monkeypatch):
    """
    加载当前工作树脚本并在结束时恢复导入登记，不执行命令行主函数。
    """
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location("review_" + path.stem.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module
