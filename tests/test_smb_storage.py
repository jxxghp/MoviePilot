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


@pytest.fixture
def multi_share(server_transfer):
    """在同一个 SMB 服务中挂载两个共享，拦截所有文件副作用。"""
    state = server_transfer
    state.storage._configure_shares({"shares": ["video", "downloads"]})
    state.source.path = "/downloads/Movie/movie.mkv"
    state.target.path = "/video/Movie/renamed.mkv"
    return state


def test_multiple_shares_share_one_session_and_virtual_root(smb_storage):
    """保存多共享仅建立一次会话，根目录返回所有已配置共享。"""
    smb_storage.module.storage_manage(storage="smb", action="save_config", conf={
        "host": "10.10.10.11", "shares": ["video", "downloads"],
    })
    storage = smb_storage.storage
    smb_storage.client.register_session.assert_called_once()
    assert [call.args[0] for call in smb_storage.client.listdir.call_args_list] == [
        r"\\10.10.10.11\video", r"\\10.10.10.11\downloads",
    ]
    smb_storage.client.listdir.reset_mock()
    items = storage.list(FileItem(path="/", type="dir", storage="smb"))
    assert [(item.name, item.path, item.storage) for item in items] == [
        ("video", "/video/", "smb"), ("downloads", "/downloads/", "smb"),
    ]
    smb_storage.client.listdir.assert_not_called()
    assert storage.get_item_strict(Path("/")).type == "dir"


@pytest.mark.parametrize("shares", [[], "video,downloads", ["../video"], ["video", "VIDEO"], [None], [""]])
def test_invalid_share_config_clears_old_mounts(smb_storage, shares):
    """非法多共享配置不能继续访问之前的共享，也不能退回旧 share 字段。"""
    smb_storage.module.storage_manage(storage="smb", action="save_config", conf={"host": "host", "share": "old"})
    smb_storage.module.storage_manage(storage="smb", action="save_config", conf={"host": "host", "share": "old", "shares": shares})
    assert smb_storage.storage._connected is False
    assert smb_storage.storage._server_path is None
    assert smb_storage.storage._shares == {}


def test_single_legacy_share_keeps_paths_and_multi_mode_survives_one_share(smb_storage):
    """旧配置路径不变，多共享减至一项也不会重新解释已保存路径。"""
    storage = smb_storage.storage
    smb_storage.module.storage_manage(storage="smb", action="save_config", conf={"host": "host", "share": "video"})
    assert storage._normalize_path("/Movies/a.mkv") == r"\\host\video\Movies\a.mkv"
    smb_storage.module.storage_manage(storage="smb", action="save_config", conf={"host": "host", "shares": ["video"]})
    assert storage._normalize_path("/video/Movies/a.mkv") == r"\\host\video\Movies\a.mkv"
    assert storage._relative_path(r"\\host\video\Movies\a.mkv") == "/video/Movies/a.mkv"


@pytest.mark.parametrize("path", ["/unknown/a.mkv", "/downloads/../video/a.mkv", r"\downloads\..\video\a.mkv", "smb:/video/a"])
def test_multi_share_rejects_unknown_or_escaping_paths(multi_share, path):
    """共享选择受配置约束，不能通过路径跳转到未配置的共享。"""
    with pytest.raises(ValueError):
        multi_share.storage._normalize_path(path)


@pytest.mark.parametrize("path", ["/", "/video/", "/downloads/"])
def test_multi_share_roots_cannot_be_deleted_or_renamed(multi_share, path, monkeypatch):
    """共享根是挂载入口，禁止把删除目录变成清空整个共享。"""
    storage = multi_share.storage
    remove = Mock()
    monkeypatch.setattr(smbclient, "rmdir", remove)
    item = FileItem(storage="smb", path=path, type="dir")
    assert smb_module.SMB.delete(storage, item) is False
    assert storage.rename(item, "renamed") is False
    remove.assert_not_called()
    multi_share.client.rename.assert_not_called()


def test_multi_share_listing_and_strict_query_preserve_share_name(multi_share, monkeypatch):
    """浏览和整理查询都返回包含共享名称的路径，不混淆同名文件。"""
    monkeypatch.setattr(smbclient, "listdir", Mock(return_value=["movie.mkv"]))
    monkeypatch.setattr(smbclient, "stat", Mock(return_value=SimpleNamespace(st_size=1024, st_mtime=1)))
    monkeypatch.setattr(smbclient.path, "isdir", Mock(return_value=False))
    items = multi_share.storage.list(FileItem(storage="smb", path="/downloads/", type="dir"))
    assert items[0].path == "/downloads/movie.mkv"
    assert multi_share.storage.get_item_strict(Path("/video/movie.mkv")).path == "/video/movie.mkv"


@pytest.mark.parametrize("mode", ["copy", "move"])
def test_cross_share_transfer_uses_copychunk_then_optional_delete(multi_share, monkeypatch, mode):
    """跨共享只调用服务端 CopyChunk，移动严格在复制完成之后删除源。"""
    state = multi_share
    monkeypatch.setattr(smbclient, "stat", Mock(side_effect=FileNotFoundError(errno.ENOENT, "missing")))
    monkeypatch.setattr(smbclient, "remove", state.client.remove)
    state.client.reset_mock()
    assert getattr(state.storage, mode)(state.source, Path("/video/Movie"), "renamed.mkv") is True
    state.client.copyfile.assert_called_once_with(
        r"\\10.10.10.11\downloads\Movie\movie.mkv", r"\\10.10.10.11\video\Movie\renamed.mkv",
    )
    assert [call[0] for call in state.client.mock_calls] == (["copyfile", "remove"] if mode == "move" else ["copyfile"])
    state.storage.download.assert_not_called()
    state.storage.upload.assert_not_called()


@pytest.mark.parametrize("failure", ["target_exists", "target_denied", "copy", "remove"])
def test_cross_share_move_failure_is_conservative(multi_share, monkeypatch, failure):
    """目标冲突、查询失败和复制失败不删源；删源失败报错并保留已复制结果。"""
    state = multi_share
    stat = Mock(side_effect=FileNotFoundError(errno.ENOENT, "missing"))
    if failure == "target_exists":
        stat.side_effect = None
    elif failure == "target_denied":
        stat.side_effect = PermissionError(errno.EACCES, "denied")
    monkeypatch.setattr(smbclient, "stat", stat)
    monkeypatch.setattr(smbclient, "remove", state.client.remove)
    if failure in ("copy", "remove"):
        getattr(state.client, "copyfile" if failure == "copy" else "remove").side_effect = OSError("failed")
    assert state.storage.move(state.source, Path("/video/Movie"), "renamed.mkv") is False
    assert state.client.remove.call_count == (1 if failure == "remove" else 0)
    assert state.client.copyfile.call_count == (1 if failure in ("copy", "remove") else 0)
    state.storage.download.assert_not_called()
    state.storage.upload.assert_not_called()


def test_cross_share_hardlink_fails_without_side_effects(multi_share):
    """标准 SMB 不支持跨共享硬链接，不能偷偷改为复制或中转。"""
    assert multi_share.storage.link(multi_share.source, Path(multi_share.target.path)) is False
    multi_share.client.link.assert_not_called()
    multi_share.client.copyfile.assert_not_called()
    multi_share.client.makedirs.assert_not_called()


def test_multi_share_usage_is_not_double_counted(multi_share, monkeypatch):
    """多个共享可能共用磁盘，不把容量相加伪装成准确的服务用量。"""
    stat_volume = Mock()
    monkeypatch.setattr(smbclient, "stat_volume", stat_volume)
    assert multi_share.storage.usage() is None
    stat_volume.assert_not_called()
