"""MoviePilot 后端测试包。

在导入任何用例之前隔离 CONFIG_DIR。测试普遍直接 ``import app.*``，而 app 在导入链路中
按 ``settings.CONFIG_PATH`` 连接 ``user.db``：本地非容器布局下默认落到
``MoviePilot/config/user.db``（线上真实库），``import app.chain.*`` 等在导入时即会建立/连接。
本文件在 ``unittest discover``（会先导入 tests 包）与 ``pytest``（导入 tests.* 用例前先导入本包）
两种入口下都最先执行，故在此把 CONFIG_DIR 指向进程私有临时目录，避免测试连到或写入真实库与配置。
已显式设置 CONFIG_DIR 时（如 CI 指定隔离目录）尊重之、不覆盖。
"""
import atexit
import os
import shutil
import tempfile

# 必须早于首个 import app.*：app.db 在导入时即按 CONFIG_PATH 建立/连接 user.db
if not os.environ.get("CONFIG_DIR"):
    _isolated_config_dir = tempfile.mkdtemp(prefix="mp-test-config-")
    os.environ["CONFIG_DIR"] = _isolated_config_dir
    # 进程退出时清理临时库与目录，避免 /tmp 堆积
    atexit.register(shutil.rmtree, _isolated_config_dir, ignore_errors=True)
