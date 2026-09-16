"""文件存储适配器运行时设置读取回归测试。"""

from unittest.mock import patch

import pytest

from app.modules.filemanager.storages.alipan import AliPan
from app.modules.filemanager.storages.alist import Alist
from app.modules.filemanager.storages.rclone import Rclone


@pytest.mark.parametrize(
    ("storage_type", "setting_key"),
    [
        (Alist, "OPENLIST_SNAPSHOT_CHECK_FOLDER_MODTIME"),
        (Rclone, "RCLONE_SNAPSHOT_CHECK_FOLDER_MODTIME"),
        (AliPan, "ALIPAN_SNAPSHOT_CHECK_FOLDER_MODTIME"),
    ],
)
def test_storage_snapshot_setting_is_read_at_use_time(storage_type, setting_key) -> None:
    """已创建的存储实例也必须读取配置变更后的最新目录检查开关。"""
    state = {setting_key: False}
    with patch(
        f"{storage_type.__module__}.get_runtime_setting",
        side_effect=lambda key: state[key],
    ):
        storage = storage_type.__new__(storage_type)
        assert storage.snapshot_check_folder_modtime is False
        state[setting_key] = True
        assert storage.snapshot_check_folder_modtime is True
