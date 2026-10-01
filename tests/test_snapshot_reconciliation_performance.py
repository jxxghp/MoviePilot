from pathlib import Path
from unittest.mock import MagicMock

from app import schemas
from app.modules.filemanager.storages import StorageBase


def _directory(path: str, *, modify_time: int = 200):
    return schemas.FileItem(
        storage="alist",
        type="dir",
        path=f"{path.rstrip('/')}/",
        name=Path(path).name,
        modify_time=modify_time,
    )


def _file(path: str, *, size: int = 1):
    return schemas.FileItem(
        storage="alist",
        type="file",
        path=path,
        name=Path(path).name,
        size=size,
        modify_time=200,
    )


def test_snapshot_removes_a_deleted_directory_without_scanning_other_subtrees():
    """
    目录删除只应移除该目录的文件，而不影响其它目录的旧基线。
    """
    storage = MagicMock()
    storage.snapshot_check_folder_modtime = True
    storage.snapshot_strict_query = False
    storage._snapshot_list = lambda item: StorageBase._snapshot_list(storage, item)
    root = _directory("/mon")
    keep = _directory("/mon/keep", modify_time=50)
    storage.get_item.return_value = root
    storage.list.return_value = [keep]

    previous_snapshot = {
        "/mon/gone/episode-1.mkv": {"size": 1, "modify_time": 100},
        "/mon/gone/episode-2.mkv": {"size": 2, "modify_time": 100},
        "/mon/keep/episode-1.mkv": {"size": 3, "modify_time": 50},
    }

    snapshot = StorageBase.snapshot(
        storage,
        Path("/mon"),
        last_snapshot_time=100,
        previous_snapshot=previous_snapshot,
    )

    assert snapshot == {
        "/mon/keep/episode-1.mkv": {"size": 3, "modify_time": 50}
    }


def test_snapshot_reconciles_nested_deleted_files_with_bounded_directory_work():
    """
    每个被列举目录只需处理自己的直接子项；嵌套文件删除仍应正确收敛。
    """
    storage = MagicMock()
    storage.snapshot_check_folder_modtime = False
    storage.snapshot_strict_query = False
    storage._snapshot_list = lambda item: StorageBase._snapshot_list(storage, item)
    root = _directory("/mon")
    season = _directory("/mon/show/Season 1")
    storage.get_item.return_value = root
    storage.list.side_effect = [
        [_directory("/mon/show")],
        [season],
        [_file("/mon/show/Season 1/episode-2.mkv", size=20)],
    ]

    previous_snapshot = {
        "/mon/show/Season 1/episode-1.mkv": {"size": 10, "modify_time": 100},
        "/mon/show/Season 1/episode-2.mkv": {"size": 20, "modify_time": 100},
    }

    snapshot = StorageBase.snapshot(
        storage,
        Path("/mon"),
        previous_snapshot=previous_snapshot,
    )

    assert snapshot == {
        "/mon/show/Season 1/episode-2.mkv": {"size": 20, "modify_time": 200, "fileid": None, "type": "file"}
    }
