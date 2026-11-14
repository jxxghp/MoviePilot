"""
验证 115 存储客户端的文件操作映射与递归监控快照。
"""

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import Mock, patch

import pytest

from app.application.transfer.execution import TransferExecutionConflictError
from app.modules.filemanager.module import FileManagerModule
from app.modules.filemanager.storages.local import LocalStorage
from app.modules.filemanager.storages.u115 import U115Pan
from app.modules.filemanager.transhandler import TransHandler
from app.monitor.poller import RemotePoller
from app.monitor.snapshot import SnapshotStore
from app.schemas.file import FileItem


def _target_dir() -> FileItem:
    """
    构造 115 上传目标目录。
    """
    return FileItem(
        storage="u115",
        path="/library/Test Show (2026)/Season 1",
        type="dir",
        name="Season 1",
        fileid="100",
    )


def _fake_sha1(*_args, **_kwargs) -> str:
    """
    返回固定 SHA1，避免单测重复计算文件哈希。
    """
    return "sha1"


def _fake_request_api(_method, endpoint, *_args, **_kwargs):
    """
    模拟 115 初始化、凭证和断点续传接口。
    """
    if endpoint == "/open/upload/init":
        return {
            "state": True,
            "data": {
                "bucket": "bucket",
                "object": "object",
                "callback": {"callback": "callback", "callback_var": "var"},
                "pick_code": "pickcode",
                "status": 1,
            },
        }
    if endpoint == "/open/upload/get_token":
        return {
            "endpoint": "endpoint",
            "AccessKeyId": "access_key_id",
            "AccessKeySecret": "access_key_secret",
            "SecurityToken": "security_token",
        }
    if endpoint == "/open/upload/resume":
        return None
    return None


class _FakeBucket:
    """
    模拟 OSS 分片上传客户端。
    """

    complete_payload = {"state": True}

    def __init__(self, *_args, **_kwargs):
        """
        初始化伪造 Bucket。
        """
        pass

    def init_multipart_upload(self, *_args, **_kwargs):
        """
        返回固定 upload_id。
        """
        return SimpleNamespace(upload_id="upload_id")

    def upload_part(self, *_args, **_kwargs):
        """
        返回固定分片 etag。
        """
        return SimpleNamespace(etag="etag")

    def complete_multipart_upload(self, *_args, **_kwargs):
        """
        返回可配置的 115 回调结果。
        """
        response = SimpleNamespace(json=lambda: self.complete_payload)
        return SimpleNamespace(status=200, resp=SimpleNamespace(response=response))


def _build_storage() -> U115Pan:
    """
    构造跳过初始化流程的 115 存储实例。
    """
    storage = object.__new__(U115Pan)
    storage._calc_sha1 = _fake_sha1
    storage.get_item = lambda _path: None
    storage._request_api = _fake_request_api
    return storage


def _upload_with_fakes(storage: U115Pan, target_dir: FileItem, local_file):
    """
    使用伪造 OSS 和进度回调执行上传。
    """

    with patch(
        "oss2.StsAuth",
        return_value=object(),
    ), patch(
        "oss2.Bucket",
        _FakeBucket,
    ), patch(
        "app.modules.filemanager.storages.u115.transfer_process",
        return_value=lambda _progress: None,
    ):
        return storage.upload(target_dir, local_file)


def test_upload_returns_target_fileitem_when_uploaded_metadata_is_delayed(tmp_path):
    """
    115 上传完成后目录索引暂不可见时，应返回可落库的目标文件项。
    """
    local_file = tmp_path / "Test.Show.S01E01.mkv"
    local_file.write_bytes(b"movie")

    storage = _build_storage()
    uploaded_item = _upload_with_fakes(storage, _target_dir(), local_file)

    assert uploaded_item is not None
    assert uploaded_item.storage == "u115"
    assert uploaded_item.path == "/library/Test Show (2026)/Season 1/Test.Show.S01E01.mkv"
    assert uploaded_item.type == "file"
    assert uploaded_item.size == local_file.stat().st_size


@pytest.mark.parametrize("changed_target", [False, True])
def test_instant_upload_move_uses_exact_bytes_before_deleting_source(
    tmp_path, monkeypatch, changed_target,
):
    """
    秒传回执与严格查询使用相同字节数；真实大小变化仍必须保留源文件。
    """
    local_file = tmp_path / "Test.Show.S01E01.mkv"
    local_file.write_bytes(b"movie" * 251)
    file_size = local_file.stat().st_size
    target_dir = _target_dir()
    target_path = Path(target_dir.path) / local_file.name
    info = {
        "file_id": "200",
        "file_category": "1",
        "file_name": local_file.name,
        "pick_code": "pickcode",
        "size": "0.98 KB",
        "size_byte": file_size,
        "utime": 1,
    }

    def request_api(method, endpoint, *_args, **_kwargs):
        """
        按文件 ID 返回秒传详情，按路径返回删除前的最新详情。
        """
        if endpoint == "/open/upload/init":
            return {"state": True, "data": {"status": 2, "file_id": "200"}}
        assert endpoint == "/open/folder/get_info"
        if method == "GET":
            return info
        return {"state": True, "data": {**info, "size_byte": file_size + int(changed_target)}}

    storage = _build_storage()
    storage._request_api = request_api
    storage.get_folder = lambda _path: target_dir
    monkeypatch.setattr("app.modules.filemanager.storages.u115.time.sleep", lambda _seconds: None)
    local = object.__new__(LocalStorage)
    source = local.get_item_strict(local_file)

    def move():
        """
        执行真实跨存储移动编排，在 API 边界离线模拟 115。
        """
        return TransHandler._TransHandler__execute_transfer_with_steps(
            step_runner=None,
            source_fileitem=source.model_dump(mode="json"),
            target_storage="u115",
            source_oper=local,
            target_oper=storage,
            target_file=target_path,
            transfer_type="move",
        )

    if changed_target:
        with pytest.raises(TransferExecutionConflictError, match="size 已变化"):
            move()
        assert local_file.read_bytes() == b"movie" * 251
    else:
        item, error = move()
        assert error == ""
        assert item.size == file_size
        assert item.fileid == "200"
        assert not local_file.exists()


def test_upload_returns_none_when_complete_callback_reports_failure(tmp_path):
    """
    115 完成回调失败时，不应把文件视为上传成功。
    """
    local_file = tmp_path / "Test.Show.S01E02.mkv"
    local_file.write_bytes(b"movie")

    storage = _build_storage()
    with patch.object(
        _FakeBucket,
        "complete_payload",
        {"state": False, "message": "callback failed"},
    ):
        uploaded_item = _upload_with_fakes(storage, _target_dir(), local_file)

    assert uploaded_item is None


def test_upload_uses_dynamic_part_size(tmp_path):
    """
    115 上传应根据文件大小动态控制 OSS 分片大小。
    """
    local_file = tmp_path / "Test.Show.S01E03.mkv"
    local_file.write_bytes(b"movie")

    storage = _build_storage()
    with patch(
        "oss2.determine_part_size",
        return_value=local_file.stat().st_size,
    ) as determine_part_size_mock:
        uploaded_item = _upload_with_fakes(storage, _target_dir(), local_file)

    assert uploaded_item is not None
    determine_part_size_mock.assert_called_once_with(
        local_file.stat().st_size, preferred_size=10 * 1024 * 1024
    )


def test_upload_part_size_grows_for_large_files():
    """
    大文件上传应自动放大分片大小，避免产生过多分片。
    """
    file_size = int(5.6 * 1024 * 1024 * 1024)

    part_size = U115Pan._U115Pan__get_upload_part_size(file_size)

    assert part_size == 64 * 1024 * 1024


def test_expired_access_token_uses_refreshed_credentials():
    """
    令牌刷新成功后，请求头和返回值应使用新令牌。
    """
    storage = object.__new__(U115Pan)
    storage.get_conf = lambda: {
        "refresh_token": "old-refresh",
        "access_token": "old-access",
        "refresh_time": 1,
        "expires_in": 1,
    }
    storage._U115Pan__refresh_access_token = lambda _token: {
        "refresh_token": "new-refresh",
        "access_token": "new-access",
        "expires_in": 3600,
    }
    saved = []
    headers = []
    storage.set_config = saved.append
    storage._request_utils = SimpleNamespace(update_headers=headers.append)

    assert storage.access_token == "new-access"
    assert saved[0]["refresh_token"] == "new-refresh"
    assert saved[0]["access_token"] == "new-access"
    assert headers == [{"Authorization": "Bearer new-access"}]


@pytest.mark.parametrize("response,expected", [
    (None, False), ({"state": False}, False), ({"state": True}, True),
])
def test_delete_requires_explicit_api_success(response, expected):
    """
    115 删除被拒绝或请求失败时，不能向整理结算谎报源文件已经删除。
    """
    storage = _build_storage()
    storage._request_api = lambda *_args, **_kwargs: response
    assert storage.delete(FileItem(storage="u115", path="/source.mkv", fileid="200")) is expected


def _snapshot_node(
    path: Path, fileid: str, *, directory: bool = False,
    modify_time: int = 50, size: int = 0,
) -> dict[str, Any]:
    """
    构造 115 路径详情接口使用的离线文件元数据。
    """
    return {
        "file_id": fileid,
        "file_category": "0" if directory else "1",
        "file_name": path.name,
        "pick_code": "pickcode",
        "size_byte": size,
        "utime": modify_time,
    }


@pytest.fixture
def snapshot_storage(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """
    在 115 API 边界模拟五层目录，保留真实文件项转换、分页、快照与模块分发。

    所有目录时间固定为 50，不随深层文件变化推进，不读取配置或建立网络会话。
    """
    root = Path("/incoming")
    directories = [root, root / "TV", root / "TV/Chinese", root / "TV/Chinese/Show",
                   root / "TV/Chinese/Show/Season 1"]
    tree = {
        path.as_posix(): _snapshot_node(path, str(index + 1), directory=True)
        for index, path in enumerate(directories)
    }
    parent = directories[-1]
    old_path = (parent / "old.mkv").as_posix()
    tree[old_path] = _snapshot_node(Path(old_path), "100", modify_time=100, size=10)

    def request_api(
        method: str, endpoint: str, field: Optional[str] = None, **kwargs: Any,
    ) -> Any:
        """
        按真实接口的路径、目录标识和分页参数返回元数据，不替换快照遍历。
        """
        if endpoint == "/open/folder/get_info":
            assert method == "POST" and field is None
            return {"state": True, "data": tree.get(kwargs["data"]["path"])}
        assert endpoint == "/open/ufile/files" and method == "GET" and field == "data"
        params = kwargs["params"]
        directory = next(path for path, node in tree.items() if node["file_id"] == str(params["cid"]))
        items = [
            {"fid": node["file_id"], "fn": node["file_name"], "fc": node["file_category"],
             "fs": node["size_byte"], "upt": node["utime"], "pc": node["pick_code"],
             "ico": Path(path).suffix[1:]}
            for path, node in tree.items() if Path(path).parent.as_posix() == directory
        ]
        return items[params["offset"]:params["offset"] + params["limit"]]

    storage = object.__new__(U115Pan)
    api = Mock(side_effect=request_api)
    monkeypatch.setattr(storage, "_request_api", api)
    module = object.__new__(FileManagerModule)
    module._support_storages = ["u115"]
    monkeypatch.setattr(module, "_FileManagerModule__get_storage_oper", Mock(return_value=storage))
    return SimpleNamespace(storage=storage, module=module, api=api, tree=tree,
                           root=root, parent=parent, old_path=old_path)


@pytest.mark.parametrize(("change", "empty_baseline"), [
    ("added", False), ("added", True), ("moved_old", False), ("moved_old", True),
    ("modified", False), ("deleted", False),
])
def test_snapshot_detects_descendant_changes_without_parent_mtime(
    snapshot_storage: SimpleNamespace, change: str, empty_baseline: bool,
) -> None:
    """
    祖先目录时间不变时仍发现新增、保留旧时间的移入、修改和删除，兼容空基线。
    """
    state = snapshot_storage
    if empty_baseline:
        del state.tree[state.old_path]
    baseline = state.storage.snapshot(state.root)
    assert baseline is not None
    assert len(baseline) == (0 if empty_baseline else 1)
    new_path = state.parent / "new.mkv"
    if change in {"added", "moved_old"}:
        state.tree[new_path.as_posix()] = _snapshot_node(
            new_path, "101", modify_time=200 if change == "added" else 1, size=20,
        )
    elif change == "modified":
        state.tree[state.old_path].update(size_byte=20, utime=200)
    else:
        del state.tree[state.old_path]

    snapshot = state.module.snapshot_storage(
        "u115", state.root, last_snapshot_time=100, previous_snapshot=baseline,
    )
    assert snapshot is not None
    expected = {"added": [], "modified": []}
    if change in {"added", "moved_old"}:
        expected["added"] = [new_path.as_posix()]
        assert snapshot[new_path.as_posix()]["fileid"] == "101"
    elif change == "modified":
        expected["modified"] = [state.old_path]
        assert snapshot[state.old_path]["size"] == 20
    else:
        assert state.old_path not in snapshot
    assert SnapshotStore.compare(baseline, snapshot) == expected
    if not empty_baseline:
        assert baseline[state.old_path]["size"] == 10


@pytest.mark.parametrize(("max_depth", "file_count"), [(2, 0), (4, 0), (5, 1)])
def test_snapshot_keeps_depth_limit(
    snapshot_storage: SimpleNamespace, max_depth: int, file_count: int,
) -> None:
    """
    不按目录时间剪枝后仍遵守递归深度预算，不访问预算外的目录。
    """
    state = snapshot_storage
    snapshot = state.storage.snapshot(state.root, last_snapshot_time=100, max_depth=max_depth)
    assert snapshot is not None and len(snapshot) == file_count
    assert sum(call.args[1] == "/open/ufile/files" for call in state.api.call_args_list) == max_depth


def test_snapshot_keeps_u115_directory_pagination(snapshot_storage: SimpleNamespace) -> None:
    """
    深层目录超过单页上限时继续分页，不因祖先目录时间旧而漏掉后续页面。
    """
    state = snapshot_storage
    del state.tree[state.old_path]
    for index in range(1001):
        path = state.parent / f"episode-{index}.mkv"
        state.tree[path.as_posix()] = _snapshot_node(path, str(1000 + index), size=10)

    snapshot = state.storage.snapshot(state.root, last_snapshot_time=100, previous_snapshot={})
    assert snapshot is not None and len(snapshot) == 1001
    pages = [call.kwargs["params"] for call in state.api.call_args_list
             if call.args[1] == "/open/ufile/files" and call.kwargs["params"]["cid"] == 5]
    assert [(page["offset"], page["limit"]) for page in pages] == [(0, 1000), (1000, 1000)]


def test_poll_dispatches_old_mtime_file_from_empty_baseline_once(
    snapshot_storage: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    空基线下发现保留旧时间的深层移入文件，并且后续轮询不重复派发整理。
    """
    state = snapshot_storage
    del state.tree[state.old_path]
    records: dict[str, bytes] = {}
    cache = Mock()
    cache.get.side_effect = lambda key, **_kwargs: records.get(key)
    cache.set.side_effect = lambda key, value, **_kwargs: records.__setitem__(key, value)
    store = SnapshotStore(cache=cache)
    assert store.save("u115", {}, file_count=0, last_snapshot_time=100)
    dispatcher = Mock()
    dispatcher.is_transfer_candidate_path.return_value = True
    dispatcher.handle_file.return_value = True
    chain = SimpleNamespace(snapshot_storage=state.module.snapshot_storage)
    monkeypatch.setattr("app.monitor.poller.StorageChain", lambda: chain)
    poller = RemotePoller(store=store, dispatcher=dispatcher)
    path = state.parent / "new.mkv"
    state.tree[path.as_posix()] = _snapshot_node(path, "101", modify_time=1, size=20)

    assert poller.poll("u115", [state.root]) == 1
    dispatcher.handle_file.assert_called_once()
    assert dispatcher.handle_file.call_args.kwargs["event_path"] == path
    assert dispatcher.handle_file.call_args.kwargs["fileid"] == "101"
    assert poller.poll("u115", [state.root]) == 1
    dispatcher.handle_file.assert_called_once()
