from types import SimpleNamespace

from app.api.endpoints.transfer import (
    manual_transfer,
    match_manual_transfer_target_path,
    recommend_episode_format,
)
from app.schemas.file import FileItem
from app.schemas.system import TransferDirectoryConf
from app.schemas.transfer import EpisodeFormatRecommendItem, ManualTransferItem


def test_manual_music_transfer_forwards_entity_namespace(monkeypatch):
    """手动音乐整理应把请求选择的单曲或专辑命名空间传入整理链。"""
    captured = {}

    class FakeTransferChain:
        """记录手动整理 API 向整理链传入的参数。"""

        def manual_transfer(self, **kwargs):
            """保存整理参数并模拟成功。"""
            captured.update(kwargs)
            return True, ""

    monkeypatch.setattr("app.api.endpoints.transfer.TransferChain", FakeTransferChain)

    response = manual_transfer(
        transer_item=ManualTransferItem(
            fileitem=FileItem(
                storage="local",
                path="/downloads/叶惠美",
                name="叶惠美",
                type="dir",
            ),
            type_name="音乐",
            media_source="musicbrainz",
            media_id="977e6978-139d-425c-bb98-6b0c62d1e45e",
            music_type="album",
        ),
        background=True,
        history_query=SimpleNamespace(get=lambda _history_id: None),
        _="token",
    )

    assert response.success is True
    assert captured["music_type"] == "album"


def test_manual_transfer_failure_returns_path_stage_and_recovery_action(monkeypatch):
    """文件管理器手动整理失败时，应直接返回源路径、失败阶段和恢复动作。"""

    class FakeTransferChain:
        """模拟目标存储写入失败的整理链。"""

        @staticmethod
        def manual_transfer(**_kwargs):
            """返回稳定的目标写入失败原因。"""
            return False, "目标路径不可写"

    monkeypatch.setattr("app.api.endpoints.transfer.TransferChain", FakeTransferChain)
    source = "/downloads/manual-failed.mkv"

    response = manual_transfer(
        transer_item=ManualTransferItem(
            fileitem=FileItem(
                storage="local",
                path=source,
                name="manual-failed.mkv",
                type="file",
            ),
            target_path="/media/movies",
        ),
        background=False,
        history_query=SimpleNamespace(get=lambda _history_id: None),
        _="token",
    )

    assert response.success is False
    assert f"源文件：{source}" in response.message
    assert "目标路径：/media/movies" in response.message
    assert "失败阶段：目标存储访问" in response.message
    assert "下一步：检查目标存储连接" in response.message


def test_manual_music_directory_defaults_to_album_namespace(monkeypatch):
    """旧客户端只声明音乐目录时，后端应按整张专辑而不是单曲解释媒体 ID。"""
    captured = {}

    class FakeTransferChain:
        """记录手动整理调用参数。"""

        def manual_transfer(self, **kwargs):
            captured.update(kwargs)
            return True, ""

    monkeypatch.setattr("app.api.endpoints.transfer.TransferChain", FakeTransferChain)

    response = manual_transfer(
        transer_item=ManualTransferItem(
            fileitem=FileItem(
                storage="local",
                path="/downloads/叶惠美",
                name="叶惠美",
                type="dir",
            ),
            type_name="音乐",
            media_source="musicbrainz",
            media_id="977e6978-139d-425c-bb98-6b0c62d1e45e",
        ),
        background=True,
        history_query=SimpleNamespace(get=lambda _history_id: None),
        _="token",
    )

    assert response.success is True
    assert captured["music_type"] == "album"


def test_manual_music_file_defaults_to_recording_namespace(monkeypatch):
    """旧客户端只声明音乐文件时，后端应继续按单曲解释媒体 ID。"""
    captured = {}

    class FakeTransferChain:
        """记录手动整理调用参数。"""

        def manual_transfer(self, **kwargs):
            captured.update(kwargs)
            return True, ""

    monkeypatch.setattr("app.api.endpoints.transfer.TransferChain", FakeTransferChain)

    response = manual_transfer(
        transer_item=ManualTransferItem(
            fileitem=FileItem(
                storage="local",
                path="/downloads/晴天.flac",
                name="晴天.flac",
                type="file",
                extension="flac",
            ),
            type_name="音乐",
            media_source="musicbrainz",
            media_id="recording-1",
        ),
        background=True,
        history_query=SimpleNamespace(get=lambda _history_id: None),
        _="token",
    )

    assert response.success is True
    assert captured["music_type"] == "recording"


def test_manual_transfer_from_history_preserves_download_context(monkeypatch):
    """复用历史识别信息时应传递原下载上下文。"""
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
        bangumiid=None,
        anilistid=None,
        media_source="themoviedb",
        media_id="100",
        seasons="S01",
        episodes="E01-E02",
        episode_group="WEB-DL",
    )

    captured = {}

    class FakeTransferChain:
        def manual_transfer(self, **kwargs):
            captured.update(kwargs)
            return True, ""

    monkeypatch.setattr("app.api.endpoints.transfer.TransferChain", FakeTransferChain)

    resp = manual_transfer(
        transer_item=ManualTransferItem(logid=1, from_history=True),
        background=True,
        history_query=SimpleNamespace(get=lambda history_id: history if history_id == 1 else None),
        _="token",
    )

    assert resp.success is True
    assert captured["downloader"] == "qbittorrent"
    assert captured["download_hash"] == "abc123"
    assert captured["episode_group"] == "WEB-DL"
    assert captured["season"] == 1


def test_manual_transfer_without_history_recognition_ignores_old_hash(monkeypatch):
    """从历史重新识别时应忽略旧下载上下文。"""
    history = SimpleNamespace(
        status=0,
        mode="copy",
        src_fileitem={
            "storage": "local",
            "path": "/downloads/test.mkv",
            "name": "test.mkv",
            "type": "file",
        },
        dest_fileitem=None,
        downloader="qbittorrent",
        download_hash="polluted-hash",
        type="电视剧",
        tmdbid="100",
        doubanid="200",
        seasons="S01",
        episodes="E01",
        episode_group="WEB-DL",
    )
    captured = {}

    class FakeTransferChain:
        """记录 API 传入整理链的参数。"""

        def manual_transfer(self, **kwargs):
            """记录手动整理参数。"""
            captured.update(kwargs)
            return True, ""

    monkeypatch.setattr("app.api.endpoints.transfer.TransferChain", FakeTransferChain)

    resp = manual_transfer(
        transer_item=ManualTransferItem(logid=1, from_history=False),
        background=True,
        history_query=SimpleNamespace(get=lambda history_id: history if history_id == 1 else None),
        _="token",
    )

    assert resp.success is True
    assert captured["downloader"] is None
    assert captured["download_hash"] is None
    assert captured["media_source"] is None
    assert captured["media_id"] is None
    assert captured["episode_group"] is None


def test_manual_transfer_from_history_passes_old_dest_cleanup_to_chain(monkeypatch):
    history = SimpleNamespace(
        status=0,
        mode="copy",
        src_fileitem={
            "storage": "local",
            "path": "/downloads/test.mkv",
            "name": "test.mkv",
            "type": "file",
        },
        dest_fileitem={
            "storage": "local",
            "path": "/library/test.mkv",
            "name": "test.mkv",
            "type": "file",
        },
        downloader="qbittorrent",
        download_hash="abc123",
        type=None,
        tmdbid=None,
        doubanid=None,
        seasons=None,
        episodes=None,
        episode_group=None,
    )
    captured = {}

    class FakeTransferChain:
        def manual_transfer(self, **kwargs):
            captured.update(kwargs)
            return True, ""

    monkeypatch.setattr("app.api.endpoints.transfer.TransferChain", FakeTransferChain)

    resp = manual_transfer(
        transer_item=ManualTransferItem(logid=1),
        background=False,
        history_query=SimpleNamespace(get=lambda history_id: history if history_id == 1 else None),
        _="token",
    )

    assert resp.success is True
    assert captured["fileitem"].path == "/downloads/test.mkv"
    assert captured["cleanup_dest_fileitem"].path == "/library/test.mkv"


def test_manual_transfer_from_history_preview_does_not_cleanup_old_dest(monkeypatch):
    history = SimpleNamespace(
        status=0,
        mode="copy",
        src_fileitem={
            "storage": "local",
            "path": "/downloads/test.mkv",
            "name": "test.mkv",
            "type": "file",
        },
        dest_fileitem={
            "storage": "local",
            "path": "/library/test.mkv",
            "name": "test.mkv",
            "type": "file",
        },
        downloader="qbittorrent",
        download_hash="abc123",
        type=None,
        tmdbid=None,
        doubanid=None,
        seasons=None,
        episodes=None,
        episode_group=None,
    )
    captured = {}

    class FakeTransferChain:
        def manual_transfer(self, **kwargs):
            captured.update(kwargs)
            return True, {
                "summary": {"total": 0, "success": 0, "failed": 0},
                "items": [],
                "message": "",
            }

    monkeypatch.setattr("app.api.endpoints.transfer.TransferChain", FakeTransferChain)

    resp = manual_transfer(
        transer_item=ManualTransferItem(logid=1, preview=True),
        background=False,
        history_query=SimpleNamespace(get=lambda history_id: history if history_id == 1 else None),
        _="token",
    )

    assert resp.success is True
    assert captured["cleanup_dest_fileitem"] is None


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
        history_query=SimpleNamespace(get=lambda _history_id: None),
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
        history_query=SimpleNamespace(get=lambda _history_id: None),
        _="token",
    )

    assert resp.success is True
    assert resp.data["summary"] == {"total": 2, "success": 1, "failed": 1}
    assert [item["source"] for item in resp.data["items"]] == file_paths
    assert resp.data["items"][1]["success"] is False


def test_manual_transfer_music_files_share_one_album_batch(monkeypatch):
    """多选音轨应一次进入整理链，避免按单曲丢失专辑分类上下文。"""
    selected_fileitems = [
        {
            "storage": "local",
            "path": f"/downloads/七里香/{index:02d}.flac",
            "name": f"{index:02d}.flac",
            "extension": "flac",
            "type": "file",
        }
        for index in (1, 2)
    ]
    captured = []

    class FakeTransferChain:
        def manual_transfer(self, **kwargs):
            captured.append(kwargs)
            return True, {
                "summary": {"total": 2, "success": 2, "failed": 0},
                "items": [
                    {
                        "source": item.path,
                        "target": f"/library/Album/周杰伦/七里香/{item.name}",
                        "target_dir": "/library/Album/周杰伦/七里香",
                        "success": True,
                        "message": "",
                        "type": "音乐",
                        "title": "七里香 (2004)",
                    }
                    for item in kwargs["selected_fileitems"]
                ],
                "message": "",
            }

    monkeypatch.setattr("app.api.endpoints.transfer.TransferChain", FakeTransferChain)
    monkeypatch.setattr(
        "app.api.endpoints.transfer.get_api_runtime_config_snapshot",
        lambda: SimpleNamespace(audio_extensions=(".flac",)),
    )

    resp = manual_transfer(
        transer_item=ManualTransferItem(
            fileitems=selected_fileitems,
            preview=True,
            type_name="自动",
        ),
        background=False,
        history_query=SimpleNamespace(get=lambda _history_id: None),
        _="token",
    )

    assert resp.success is True
    assert resp.data["summary"] == {"total": 2, "success": 2, "failed": 0}
    assert len(captured) == 1
    assert captured[0]["mtype"].value == "音乐"
    assert captured[0]["music_type"] == "album"
    assert [item.path for item in captured[0]["selected_fileitems"]] == [
        item["path"] for item in selected_fileitems
    ]


def test_manual_transfer_music_batch_ignores_non_disc_alternate_directory(monkeypatch):
    """根目录已有完整专辑时，不得把“附加原版”等任意子目录当成第二张碟。"""
    selected_fileitems = [
        {
            "storage": "local",
            "path": path,
            "name": path.rsplit("/", 1)[-1],
            "extension": "flac",
            "type": "file",
        }
        for path in (
            "/downloads/七里香/01.flac",
            "/downloads/七里香/02.flac",
            "/downloads/七里香/附加原版/01.flac",
            "/downloads/七里香/附加原版/02.flac",
        )
    ]
    captured = []

    class FakeTransferChain:
        def manual_transfer(self, **kwargs):
            captured.append(kwargs)
            return True, {
                "summary": {"total": 2, "success": 2, "failed": 0},
                "items": [],
                "message": "",
            }

    monkeypatch.setattr("app.api.endpoints.transfer.TransferChain", FakeTransferChain)
    monkeypatch.setattr(
        "app.api.endpoints.transfer.get_api_runtime_config_snapshot",
        lambda: SimpleNamespace(audio_extensions=(".flac",)),
    )

    response = manual_transfer(
        transer_item=ManualTransferItem(
            fileitems=selected_fileitems,
            preview=True,
            type_name="自动",
        ),
        background=False,
        history_query=SimpleNamespace(get=lambda _history_id: None),
        _="token",
    )

    assert response.success is True
    assert len(captured) == 1
    assert [item.path for item in captured[0]["selected_fileitems"]] == [
        "/downloads/七里香/01.flac",
        "/downloads/七里香/02.flac",
    ]


def test_manual_transfer_history_ids_share_one_music_album_batch(monkeypatch):
    """多选整理历史应先还原文件集合，再以一个专辑批次进入整理链。"""
    paths = (
        "/downloads/七里香/01.flac",
        "/downloads/七里香/02.flac",
        "/downloads/七里香/附加原版/01.flac",
        "/downloads/七里香/附加原版/02.flac",
    )
    histories = {
        index: SimpleNamespace(
            status=1,
            mode="copy",
            src_fileitem={
                "storage": "local",
                "path": path,
                "name": path.rsplit("/", 1)[-1],
                "extension": "flac",
                "type": "file",
            },
            dest_fileitem=None,
        )
        for index, path in enumerate(paths, start=41)
    }
    captured = []

    class FakeTransferChain:
        def manual_transfer(self, **kwargs):
            captured.append(kwargs)
            selected = kwargs["selected_fileitems"]
            return True, {
                "summary": {"total": len(selected), "success": len(selected), "failed": 0},
                "items": [],
                "message": "",
            }

    monkeypatch.setattr("app.api.endpoints.transfer.TransferChain", FakeTransferChain)
    monkeypatch.setattr(
        "app.api.endpoints.transfer.get_api_runtime_config_snapshot",
        lambda: SimpleNamespace(audio_extensions=(".flac",)),
    )

    response = manual_transfer(
        transer_item=ManualTransferItem(
            logids=list(histories),
            preview=True,
            reorganize=True,
            type_name="自动",
        ),
        background=False,
        history_query=SimpleNamespace(get=histories.get),
        _="token",
    )

    assert response.success is True
    assert len(captured) == 1
    assert captured[0]["mtype"].value == "音乐"
    assert captured[0]["music_type"] == "album"
    assert [item.path for item in captured[0]["selected_fileitems"]] == [
        "/downloads/七里香/01.flac",
        "/downloads/七里香/02.flac",
    ]


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
        history_query=SimpleNamespace(get=lambda _history_id: None),
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
        history_query=SimpleNamespace(get=lambda _history_id: None),
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

    class FakeDirectoryHelper:
        def get_dir(self, **_kwargs):
            return TransferDirectoryConf(
                library_storage="local",
                library_path="/library/tv",
                transfer_type="copy",
            )

    monkeypatch.setattr("app.api.endpoints.transfer.DirectoryHelper", FakeDirectoryHelper)

    resp = match_manual_transfer_target_path(
        transer_item=ManualTransferItem(logids=[1, 2]),
        history_query=SimpleNamespace(get=histories.get),
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
