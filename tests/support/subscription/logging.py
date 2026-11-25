"""
订阅日志的隔离文件写入；不注册测试用例。
"""

import threading
import pytest
from app.runtime.log import NonBlockingFileHandler


@pytest.fixture(name="file_writer")
def fixture_file_writer():
    """
    只构造同步文件输出状态，避免本组文件回归创建后台线程或修改 singleton。
    """
    writer = object.__new__(NonBlockingFileHandler)
    writer._handlers_lock = threading.RLock()
    writer._state_lock = threading.RLock()
    writer._rotating_handlers = {}
    writer._scoped_handler_times = {}
    writer._scoped_cleanup_at = 0.0
    yield writer
    writer._close_handlers()
