"""全量单测入口：以 pytest 跑 tests 目录全部用例，命令行参数透传给 pytest。

用 __file__ 推导 tests 目录绝对路径，使脚本不依赖当前工作目录，从任意位置调用均可。
"""
import sys
from pathlib import Path

import pytest

# 本文件即位于 tests/ 下，其所在目录即测试根目录
_TESTS_DIR = Path(__file__).resolve().parent

if __name__ == "__main__":
    sys.exit(pytest.main([str(_TESTS_DIR), *sys.argv[1:]]))
