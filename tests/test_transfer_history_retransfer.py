from types import SimpleNamespace


from app.api.endpoints.transfer import (
    manual_transfer,
    match_manual_transfer_target_path,
    recommend_episode_format,
)
from app.schemas import EpisodeFormatRecommendItem, ManualTransferItem, TransferDirectoryConf


def test_manual_transfer_from_history_preserves_download_context(monkeypatch):
    history = SimpleNamespace(
        status=0,
        mode="copy",
        src_fileitem={"storage": "local", "path": "/downloads/test.mkv", "name": "test.mkv", "type": "file"},
        dest_fileitem=None,
        downloader="qbittorrent",
        download_hash="abc123",
        type="电视剧",
        tmdbid="100",
        doubanid="200",
        seasons="S01",
        episodes="E01-E02",
        episode_group="WEB-DL",
    )

    captured = {}

    def fake_get(_db, logid):
        assert logid == 1
        return history

    class FakeTransferChain:
        def manual_transfer(self, **kwargs):
            captured.update(kwargs)
            return True, ""

    monkeypatch.setattr("app.api.endpoints.transfer.TransferHistory.get", fake_get)
    monkeypatch.setattr("app.api.endpoints.transfer.TransferChain", FakeTransferChain)

    resp = manual_transfer(
        transer_item=ManualTransferItem(logid=1, from_history=True),
        background=True,
        db=object(),
        _="token",
    )

    assert resp.success is True
    assert captured["downloader"] == "qbittorrent"
    assert captured["download_hash"] == "abc123"
    assert captured["episode_group"] == "WEB-DL"
    assert captured["season"] == 1


def test_manual_transfer_preview_uses_explicit_fileitems_instead_of_directory(monkeypatch):
    dir_item = {
        "storage": "local",
        "path": "/downloads/Test Show/",
        "name": "Test Show",
        "type": "dir",
    }
    file_paths = [
        "/downloads/Test Show/Test.Show.S01E01.mkv",
        "/downloads/Test Show/Test.Show.S01E02.mkv",
        "/downloads/Test Show/Test.Show.S01E03.mkv",
    ]
    selected_fileitems = [
        {
            "storage": "local",
            "path": file_path,
            "name": file_path.rsplit("/", 1)[-1],
            "type": "file",
        }
        for file_path in file_paths
    ]
    captured = []

    class FakeTransferChain:
        def manual_transfer(self, **kwargs):
            captured.append(kwargs)
            fileitem = kwargs["fileitem"]
            return True, {
                "summary": {"total": 1, "success": 1, "failed": 0},
                "items": [
                    {
                        "source": fileitem.path,
                        "target": f"/library/{fileitem.name}",
                        "target_dir": "/library",
                        "success": True,
                        "message": "",
                        "type": "电视剧",
                        "title": "Test Show (2026)",
                        "season": 1,
                        "episode": 1,
                        "episode_end": None,
                        "part": None,
                    }
                ],
                "message": "",
            }

    monkeypatch.setattr("app.api.endpoints.transfer.TransferChain", FakeTransferChain)

    resp = manual_transfer(
        transer_item=ManualTransferItem(
            fileitem=dir_item,
            fileitems=selected_fileitems,
            preview=True,
        ),
        background=False,
        db=object(),
        _="token",
    )

    assert resp.success is True
    assert len(captured) == 3
    assert [item["fileitem"].path for item in captured] == file_paths
    assert all(item["sync_extra_files"] is False for item in captured)
    assert resp.data["summary"] == {"total": 3, "success": 3, "failed": 0}
    assert [item["source"] for item in resp.data["items"]] == file_paths


def test_manual_transfer_preview_multi_select_collects_failures(monkeypatch):
    file_paths = [
        "/downloads/Test Show/Test.Show.S01E01.mkv",
        "/downloads/Test Show/Test.Show.S01E02.mkv",
    ]
    selected_fileitems = [
        {
            "storage": "local",
            "path": file_path,
            "name": file_path.rsplit("/", 1)[-1],
            "type": "file",
        }
        for file_path in file_paths
    ]

    class FakeTransferChain:
        def manual_transfer(self, **kwargs):
            fileitem = kwargs["fileitem"]
            if fileitem.path.endswith("E02.mkv"):
                return False, f"{fileitem.name} 没有找到可整理的媒体文件"
            return True, {
                "summary": {"total": 1, "success": 1, "failed": 0},
                "items": [
                    {
                        "source": fileitem.path,
                        "target": f"/library/{fileitem.name}",
                        "target_dir": "/library",
                        "success": True,
                        "message": "",
                        "type": "电视剧",
                        "title": "Test Show (2026)",
                        "season": 1,
                        "episode": 1,
                        "episode_end": None,
                        "part": None,
                    }
                ],
                "message": "",
            }

    monkeypatch.setattr("app.api.endpoints.transfer.TransferChain", FakeTransferChain)

    resp = manual_transfer(
        transer_item=ManualTransferItem(
            fileitems=selected_fileitems,
            preview=True,
        ),
        background=False,
        db=object(),
        _="token",
    )

    assert resp.success is True
    assert resp.data["summary"] == {"total": 2, "success": 1, "failed": 1}
    assert [item["source"] for item in resp.data["items"]] == file_paths
    assert resp.data["items"][1]["success"] is False


def test_match_manual_transfer_target_path_returns_directory_match(monkeypatch):
    captured = {}

    class FakeDirectoryHelper:
        def get_dir(self, **kwargs):
            captured.update(kwargs)
            return TransferDirectoryConf(
                library_storage="rclone",
                library_path="/library/tv",
                transfer_type="copy",
                scraping=True,
                library_type_folder=True,
                library_category_folder=False,
            )

    monkeypatch.setattr("app.api.endpoints.transfer.DirectoryHelper", FakeDirectoryHelper)

    resp = match_manual_transfer_target_path(
        transer_item=ManualTransferItem(
            fileitem={
                "storage": "local",
                "path": "/downloads/Test Show/Test.Show.S01E01.mkv",
                "name": "Test.Show.S01E01.mkv",
                "type": "file",
            },
        ),
        db=object(),
        _="token",
    )

    assert resp.success is True
    assert captured["storage"] == "local"
    assert captured["src_path"].as_posix() == "/downloads/Test Show/Test.Show.S01E01.mkv"
    assert captured["target_storage"] is None
    assert resp.data == {
        "target_storage": "rclone",
        "target_path": "/library/tv",
        "transfer_type": "copy",
        "scrape": True,
        "library_type_folder": True,
        "library_category_folder": False,
    }


def test_match_manual_transfer_target_path_returns_null_for_ambiguous_matches(monkeypatch):
    class FakeDirectoryHelper:
        def get_dir(self, **kwargs):
            src_path = kwargs["src_path"].as_posix()
            return TransferDirectoryConf(
                library_storage="local",
                library_path="/library/tv" if "E01" in src_path else "/library/movie",
                transfer_type="copy",
            )

    monkeypatch.setattr("app.api.endpoints.transfer.DirectoryHelper", FakeDirectoryHelper)

    resp = match_manual_transfer_target_path(
        transer_item=ManualTransferItem(
            fileitems=[
                {
                    "storage": "local",
                    "path": "/downloads/Test Show/Test.Show.S01E01.mkv",
                    "name": "Test.Show.S01E01.mkv",
                    "type": "file",
                },
                {
                    "storage": "local",
                    "path": "/downloads/Test Show/Test.Show.S01E02.mkv",
                    "name": "Test.Show.S01E02.mkv",
                    "type": "file",
                },
            ],
        ),
        db=object(),
        _="token",
    )

    assert resp.success is True
    assert resp.data["target_path"] is None
    assert resp.data["target_storage"] is None


def test_match_manual_transfer_target_path_accepts_multiple_history_records(monkeypatch):
    histories = {
        1: SimpleNamespace(
            status=0,
            mode="copy",
            src_fileitem={
                "storage": "local",
                "path": "/downloads/Show/Show.S01E01.mkv",
                "name": "Show.S01E01.mkv",
                "type": "file",
            },
        ),
        2: SimpleNamespace(
            status=0,
            mode="copy",
            src_fileitem={
                "storage": "local",
                "path": "/downloads/Show/Show.S01E02.mkv",
                "name": "Show.S01E02.mkv",
                "type": "file",
            },
        ),
    }

    def fake_get(_db, logid):
        return histories.get(logid)

    class FakeDirectoryHelper:
        def get_dir(self, **_kwargs):
            return TransferDirectoryConf(
                library_storage="local",
                library_path="/library/tv",
                transfer_type="copy",
            )

    monkeypatch.setattr("app.api.endpoints.transfer.TransferHistory.get", fake_get)
    monkeypatch.setattr("app.api.endpoints.transfer.DirectoryHelper", FakeDirectoryHelper)

    resp = match_manual_transfer_target_path(
        transer_item=ManualTransferItem(logids=[1, 2]),
        db=object(),
        _="token",
    )

    assert resp.success is True
    assert resp.data["target_path"] == "/library/tv"


def test_recommend_episode_format_passes_selected_fileitems(monkeypatch):
    selected_fileitems = [
        {
            "storage": "local",
            "path": "/downloads/Test Show/Test.Show.S01E01.mkv",
            "name": "Test.Show.S01E01.mkv",
            "type": "file",
        },
        {
            "storage": "local",
            "path": "/downloads/Test Show/Test.Show.S01E02.mkv",
            "name": "Test.Show.S01E02.mkv",
            "type": "file",
        },
    ]
    captured = {}

    class FakeTransferChain:
        def recommend_episode_format(self, **kwargs):
            captured.update(kwargs)
            return True, "", {"episode_format": "Show.S01E{ep}.mkv"}

    monkeypatch.setattr("app.api.endpoints.transfer.TransferChain", FakeTransferChain)

    resp = recommend_episode_format(
        recommend_item=EpisodeFormatRecommendItem(
            fileitem=selected_fileitems[0],
            fileitems=selected_fileitems,
        ),
        _="token",
    )

    assert resp.success is True
    assert captured["fileitem"].path == selected_fileitems[0]["path"]
    assert [item.path for item in captured["fileitems"]] == [
        item["path"] for item in selected_fileitems
    ]


def test_recommend_episode_format_accepts_fileitems_without_fileitem(monkeypatch):
    selected_fileitems = [
        {
            "storage": "local",
            "path": "/downloads/Test Show/Test.Show.S01E01.mkv",
            "name": "Test.Show.S01E01.mkv",
            "type": "file",
        },
        {
            "storage": "local",
            "path": "/downloads/Test Show/Test.Show.S01E02.mkv",
            "name": "Test.Show.S01E02.mkv",
            "type": "file",
        },
    ]
    captured = {}

    class FakeTransferChain:
        def recommend_episode_format(self, **kwargs):
            captured.update(kwargs)
            return True, "", {"episode_format": "Show.S01E{ep}.mkv"}

    monkeypatch.setattr("app.api.endpoints.transfer.TransferChain", FakeTransferChain)

    resp = recommend_episode_format(
        recommend_item=EpisodeFormatRecommendItem(
            fileitems=selected_fileitems,
        ),
        _="token",
    )

    assert resp.success is True
    assert captured["fileitem"] is None
    assert [item.path for item in captured["fileitems"]] == [
        item["path"] for item in selected_fileitems
    ]
