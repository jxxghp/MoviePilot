"""核心回归集入口：以 pytest 跑一组核心测试文件，命令行参数透传给 pytest。"""
import sys

import pytest

CORE = [
    "tests/test_metainfo.py",
    "tests/test_object.py",
    "tests/test_bluray.py",
    "tests/test_mediascrape.py",
    "tests/test_subscribe_chain.py",
]

if __name__ == "__main__":
    sys.exit(pytest.main(CORE + sys.argv[1:]))
