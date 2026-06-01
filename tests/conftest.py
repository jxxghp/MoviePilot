"""pytest 全局引导：在 import 任何测试模块前把 CONFIG_DIR 指向临时目录并建表，隔离真实库。"""
import atexit
import os
import shutil
import sys
import tempfile
from types import ModuleType

# 必须早于首个 import app.*：app.db 在导入时即按 CONFIG_PATH 连接 user.db
if not os.environ.get("CONFIG_DIR"):
    _isolated_config_dir = tempfile.mkdtemp(prefix="mp-test-config-")
    os.environ["CONFIG_DIR"] = _isolated_config_dir
    atexit.register(shutil.rmtree, _isolated_config_dir, ignore_errors=True)

# app.helper.sites 由独立仓库动态拉取（CI / 全新环境无该模块），而众多 app.chain.* /
# app.modules.* 在 import 期依赖它。在此统一补一个最小垫片，省去各测试文件各自打桩；
# 若真实模块已存在（本地已拉取）则 setdefault 不覆盖，不影响真实行为。
if "app.helper.sites" not in sys.modules:
    try:
        import app.helper.sites  # noqa: F401  本地已拉取时用真实模块
    except ModuleNotFoundError:
        _sites_stub = ModuleType("app.helper.sites")
        _sites_stub.SitesHelper = object
        sys.modules["app.helper.sites"] = _sites_stub

# 必须在 CONFIG_DIR 设好之后再 import；空库会让运行期查表报 no such table，故建表
from app.db.init import init_db  # noqa: E402

init_db()


import pytest as _pytest


@_pytest.fixture(autouse=True)
def _reset_meta_caches():
    """每个用例后重置识别相关的进程级单例与 lru 缓存，避免测试间污染。

    WordsMatcher/CustomizationMatcher/ReleaseGroupsMatcher 等为 Singleton，
    自定义识别词正则走 lru_cache；测试若用不同 custom_words 调用，会让后续
    测试命中被污染的单例状态或缓存，导致识别结果错乱。这里在每个用例后清理。
    """
    yield
    try:
        from app.core.meta.words import _compile_custom_word_regex
        _compile_custom_word_regex.cache_clear()
    except Exception:
        pass
    try:
        from app.utils.singleton import Singleton
        for _cls in list(Singleton._instances):
            name = getattr(_cls, "__name__", "")
            if name in ("WordsMatcher", "CustomizationMatcher", "ReleaseGroupsMatcher", "StreamingPlatforms"):
                Singleton._instances.pop(_cls, None)
    except Exception:
        pass
