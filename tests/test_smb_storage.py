"""SMB 保存重连和同一共享内服务端整理的回归测试。"""

import errno
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import smbclient

from app.application.storage import StorageHelper
from app.modules.filemanager.module import FileManagerModule
from app.modules.filemanager.storages import smb as smb_module
from app.modules.filemanager.transhandler import TransHandler
from app.schemas.file import FileItem


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


@pytest.fixture
def server_transfer(smb_storage, monkeypatch):
    """拦截 SMB 命令并禁止本地中转，保留真实存储传输分派。"""
    storage = smb_storage.storage
    smb_storage.state["conf"] = {"host": "10.10.10.11", "share": "data"}
    storage.init_storage()
    client = Mock()
    for name in ("copyfile", "rename", "link", "makedirs"):
        monkeypatch.setattr(smbclient, name, getattr(client, name))
    monkeypatch.setattr(smbclient.path, "samefile", Mock(return_value=False))
    monkeypatch.setattr(smbclient.path, "exists", Mock(return_value=True))
    source = FileItem(
        storage="smb", path="/downloads/Movie/movie.mkv", name="movie.mkv",
        type="file", size=1024, extension="mkv",
    )
    target = source.model_copy(update={"path": "/media/Movie/renamed.mkv", "name": "renamed.mkv"})
    monkeypatch.setattr(storage, "get_folder", Mock(return_value=FileItem(
        storage="smb", path="/media/Movie/", type="dir",
    )))
    monkeypatch.setattr(storage, "get_item", Mock(return_value=target))
    for name in ("download", "upload", "delete"):
        monkeypatch.setattr(storage, name, Mock(side_effect=AssertionError("禁止中转或删除源文件")))
    return SimpleNamespace(storage=storage, client=client, source=source, target=target)


@pytest.mark.parametrize("mode, command", [("copy", "copyfile"), ("move", "rename"), ("link", "link")])
def test_same_smb_storage_transfers_on_server(server_transfer, mode, command):
    """真实整理执行器分派到 SMB 服务端命令，保留源、目标存储身份。"""
    state = server_transfer

    result, error = TransHandler._TransHandler__transfer_command(
        fileitem=state.source, target_storage="smb", source_oper=state.storage,
        target_oper=state.storage, target_file=Path(state.target.path), transfer_type=mode,
    )

    assert result == state.target
    assert error == ""
    getattr(state.client, command).assert_called_once_with(
        r"\\10.10.10.11\data\downloads\Movie\movie.mkv",
        r"\\10.10.10.11\data\media\Movie\renamed.mkv",
    )
    state.storage.download.assert_not_called()
    state.storage.upload.assert_not_called()
    state.storage.delete.assert_not_called()


@pytest.mark.parametrize("mode, command", [("copy", "copyfile"), ("move", "rename"), ("link", "link")])
@pytest.mark.parametrize("error_number", [errno.ENOTSUP, errno.EACCES, errno.ECONNRESET, errno.EXDEV, errno.EEXIST])
def test_server_transfer_failure_never_falls_back_or_deletes_source(server_transfer, mode, command, error_number):
    """不支持、拒绝访问及连接失败均保留源文件，不伪装成中转成功。"""
    state = server_transfer
    getattr(state.client, command).side_effect = OSError(error_number, "SMB operation failed")

    result, error = TransHandler._TransHandler__transfer_command(
        fileitem=state.source, target_storage="smb", source_oper=state.storage,
        target_oper=state.storage, target_file=Path(state.target.path), transfer_type=mode,
    )

    assert result is None
    assert error
    state.storage.download.assert_not_called()
    state.storage.upload.assert_not_called()
    state.storage.delete.assert_not_called()


def test_server_copy_rejects_same_file_without_truncation(server_transfer, monkeypatch):
    """源目标实际为同一文件或硬链接时，不能让 CopyChunk 的写入打开截断源。"""
    monkeypatch.setattr(smbclient.path, "samefile", Mock(return_value=True))
    state = server_transfer

    assert state.storage.copy(state.source, Path("/media/Movie"), "renamed.mkv") is False
    state.client.copyfile.assert_not_called()


def test_server_copy_accepts_new_target(server_transfer, monkeypatch):
    """目标尚不存在是正常复制场景，不把它当成查询故障。"""
    monkeypatch.setattr(smbclient.path, "samefile", Mock(side_effect=OSError(errno.ENOENT, "not found")))
    state = server_transfer

    assert state.storage.copy(state.source, Path("/media/Movie"), "renamed.mkv") is True
    state.client.copyfile.assert_called_once()


def test_server_copy_query_failure_does_not_overwrite_target(server_transfer, monkeypatch):
    """无法确认目标身份时不启动可能覆盖文件的复制。"""
    monkeypatch.setattr(smbclient.path, "samefile", Mock(side_effect=PermissionError("denied")))
    state = server_transfer

    assert state.storage.copy(state.source, Path("/media/Movie"), "renamed.mkv") is False
    state.client.copyfile.assert_not_called()
