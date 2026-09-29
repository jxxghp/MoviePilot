"""SMB 保存配置后的重连与连接状态回归测试。"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import smbclient

from app.application.storage import StorageHelper
from app.modules.filemanager.module import FileManagerModule
from app.modules.filemanager.storages import smb as smb_module


@pytest.fixture
def smb_storage(monkeypatch):
    """隔离配置持久化与 SMB 网络边界，保留真实保存和初始化流程。"""
    state = {"conf": {}}
    client = Mock()
    client.listdir.return_value = []
    for name in ("ClientConfig", "register_session", "listdir", "reset_connection_cache"):
        monkeypatch.setattr(smbclient, name, getattr(client, name))

    def get_conf(_self):
        """读取最近一次保存的配置。"""
        return state["conf"].copy()

    def set_storage(_self, storage, conf):
        """记录配置，确保重连发生在新配置保存之后。"""
        assert storage == "smb"
        state["conf"] = conf.copy()

    # 存储发现会重载实现模块，按当前模块取类，避免保留收集阶段的旧类型身份。
    monkeypatch.setattr(smb_module.SMB, "get_conf", get_conf)
    monkeypatch.setattr(StorageHelper, "set_storage", set_storage)
    storage = object.__new__(smb_module.SMB)
    smb_module.SMB.__init__(storage)
    module = object.__new__(FileManagerModule)
    module._support_storages = ["smb"]
    monkeypatch.setattr(module, "_FileManagerModule__get_storage_oper", Mock(return_value=storage))
    yield SimpleNamespace(storage=storage, module=module, client=client, state=state)
    storage._connected = False


@pytest.mark.parametrize("failure_stage", ["register_session", "listdir"])
def test_save_retries_unchanged_config_until_connection_recovers(smb_storage, failure_stage):
    """连接或共享访问失败后，每次保存相同配置都重连，网络恢复即可使用。"""
    conf = {"host": "smb.example", "share": "video", "username": "user", "password": "secret"}
    getattr(smb_storage.client, failure_stage).side_effect = [OSError("unreachable"), OSError("unreachable"), []]

    for connected in (False, False, True):
        smb_storage.client.reset_mock()
        result = smb_storage.module.storage_manage(storage="smb", action="save_config", conf=conf)

        assert result["success"] is True
        assert smb_storage.state["conf"] == conf
        assert smb_storage.storage._connected is connected
        calls = [call[0] for call in smb_storage.client.mock_calls]
        assert calls[:3] == ["reset_connection_cache", "ClientConfig", "register_session"]
        smb_storage.client.register_session.assert_called_once_with(
            "smb.example", username="user", password="secret", port=445, encrypt=False, connection_timeout=60,
        )
        if connected:
            smb_storage.storage._check_connection()
            smb_storage.client.listdir.assert_called_once_with(r"\\smb.example\video")
        else:
            with pytest.raises(smb_module.SMBConnectionError, match="连接未建立"):
                smb_storage.storage._check_connection()


@pytest.mark.parametrize("conf", [{}, {"host": "smb.example"}, {"share": "video"}])
def test_save_incomplete_config_clears_previous_connection(smb_storage, conf):
    """清空或保存不完整配置后，不得继续把旧共享标记为已连接。"""
    smb_storage.module.storage_manage(
        storage="smb", action="save_config", conf={"host": "smb.example", "share": "video"},
    )
    assert smb_storage.storage._connected is True
    smb_storage.client.reset_mock()

    smb_storage.module.storage_manage(storage="smb", action="save_config", conf=conf)

    assert smb_storage.storage._connected is False
    assert smb_storage.storage._server_path is None
    smb_storage.client.reset_connection_cache.assert_called_once_with()
    smb_storage.client.register_session.assert_not_called()
    with pytest.raises(smb_module.SMBConnectionError, match="连接未建立"):
        smb_storage.storage._check_connection()
