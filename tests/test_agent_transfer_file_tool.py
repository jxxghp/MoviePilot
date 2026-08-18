from pathlib import Path
from unittest.mock import patch

from app.agent.tools.impl.transfer_file import TransferFileTool
from app.schemas.types import MediaType


def test_transfer_file_local_directory_without_trailing_slash_uses_dir(tmp_path):
    """本地目录路径即使没有尾斜杠，也应按目录交给整理链路。"""
    source_dir = tmp_path / "Movie Folder"
    source_dir.mkdir()
    captured = {}

    def _manual_transfer(self, **kwargs):
        """记录工具传给整理链路的参数。"""
        captured.update(kwargs)
        return True, None

    with patch(
        "app.application.orchestration.transfer.TransferChain.manual_transfer",
        new=_manual_transfer,
    ):
        result = TransferFileTool._transfer_file_sync(str(source_dir))

    assert result == f"整理成功：{source_dir}"
    assert captured["fileitem"].type == "dir"
    assert captured["fileitem"].path == str(source_dir)


def test_transfer_file_local_file_uses_file(tmp_path):
    """本地文件路径应继续按文件交给整理链路。"""
    source_file = tmp_path / "Movie.mkv"
    source_file.write_text("fake media", encoding="utf-8")
    captured = {}

    def _manual_transfer(self, **kwargs):
        """记录工具传给整理链路的参数。"""
        captured.update(kwargs)
        return True, None

    with patch(
        "app.application.orchestration.transfer.TransferChain.manual_transfer",
        new=_manual_transfer,
    ):
        result = TransferFileTool._transfer_file_sync(str(source_file))

    assert result == f"整理成功：{source_file}"
    assert captured["fileitem"].type == "file"
    assert captured["fileitem"].path == str(source_file)


def test_transfer_file_remote_directory_still_uses_trailing_slash():
    """远程存储无法用本地 stat 判断时，继续使用尾斜杠识别目录。"""
    captured = {}

    def _manual_transfer(self, **kwargs):
        """记录工具传给整理链路的参数。"""
        captured.update(kwargs)
        return True, None

    with patch(
        "app.application.orchestration.transfer.TransferChain.manual_transfer",
        new=_manual_transfer,
    ):
        result = TransferFileTool._transfer_file_sync(
            "downloads/Show/",
            storage="alist",
        )

    assert result == "整理成功：/downloads/Show/"
    assert captured["fileitem"].type == "dir"
    assert captured["fileitem"].path == "/downloads/Show/"


def test_transfer_file_remote_album_uses_entity_type_without_trailing_slash():
    """远程专辑路径应由显式 album 语义判定为目录。"""
    captured = {}

    def _manual_transfer(self, **kwargs):
        """记录工具传给整理链路的参数。"""
        captured.update(kwargs)
        return True, None

    with patch(
        "app.application.orchestration.transfer.TransferChain.manual_transfer",
        new=_manual_transfer,
    ):
        result = TransferFileTool._transfer_file_sync(
            "downloads/Jay/叶惠美",
            storage="alist",
            media_type="music",
            music_type="album",
            media_source="musicbrainz",
            media_id="release-group-1",
        )

    assert result == "整理成功：/downloads/Jay/叶惠美"
    assert captured["fileitem"].type == "dir"
    assert captured["mtype"] == MediaType.MUSIC
    assert captured["media_id"] == "release-group-1"


def test_transfer_file_rejects_recording_directory(tmp_path):
    """单曲实体不能覆盖一个目录，避免把整专误当成一首歌。"""
    source_dir = tmp_path / "Album"
    source_dir.mkdir()

    with patch("app.application.orchestration.transfer.TransferChain.manual_transfer") as manual_transfer:
        result = TransferFileTool._transfer_file_sync(
            str(source_dir),
            media_type="music",
            music_type="recording",
            media_source="musicbrainz",
            media_id="recording-1",
        )

    assert "单曲必须按一个音频文件整理" in result
    manual_transfer.assert_not_called()
