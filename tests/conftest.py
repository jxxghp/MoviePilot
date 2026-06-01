"""pytest 全局引导：在 import 任何测试模块前把 CONFIG_DIR 指向临时目录并建表，隔离真实库。"""
import atexit
import os
import shutil
import tempfile

# 必须早于首个 import app.*：app.db 在导入时即按 CONFIG_PATH 连接 user.db
if not os.environ.get("CONFIG_DIR"):
    _isolated_config_dir = tempfile.mkdtemp(prefix="mp-test-config-")
    os.environ["CONFIG_DIR"] = _isolated_config_dir
    atexit.register(shutil.rmtree, _isolated_config_dir, ignore_errors=True)

# 必须在 CONFIG_DIR 设好之后再 import；空库会让运行期查表报 no such table，故建表
from app.db.init import init_db  # noqa: E402

init_db()
