from pathlib import Path
from typing import Iterator, Union
from unittest.mock import PropertyMock, patch

import pytest

from app import schemas
from app.modules.alipan.alipan import AliPan
from app.modules.rclone.rclone import Rclone
from app.modules.u115.u115 import U115Pan


PAYLOAD = b"safe-download\n"


def _noop_progress(_percent: Union[int, float]) -> None:
    """忽略测试中的进度更新。"""
    return None


class _FakeAliPanStream:
    """模拟阿里云盘下载流。"""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        """模拟响应状态检查。"""
        return None

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        """返回下载内容分块。"""
        yield self._payload

    def __enter__(self) -> "_FakeAliPanStream":
        """进入上下文。"""
        return self

    def __exit__(self, *args: object) -> None:
        """退出上下文。"""
        return None


class _FakeU115Stream:
    """模拟 115 下载流。"""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        """模拟响应状态检查。"""
        return None

    def iter_bytes(self, chunk_size: int) -> Iterator[bytes]:
        """返回下载内容分块。"""
        yield self._payload

    def close(self) -> None:
        """模拟关闭响应流。"""
        return None

    def __enter__(self) -> "_FakeU115Stream":
        """进入上下文。"""
        return self

    def __exit__(self, *args: object) -> None:
        """退出上下文。"""
        return None


class _FakeU115Session:
    """模拟 115 HTTP 会话。"""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def stream(self, method: str, url: str) -> _FakeU115Stream:
        """返回伪造的下载流。"""
        return _FakeU115Stream(self._payload)


class _FakeRcloneProcess:
    """模拟 rclone 子进程。"""

    stdout: list[str] = []

    def wait(self) -> int:
        """返回成功退出码。"""
        return 0


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("../proof.txt", "proof.txt"),
        ("..\\proof.txt", "proof.txt"),
        ("/tmp/proof.txt", "proof.txt"),
    ],
)
def test_build_download_path_strips_remote_directory_segments(
    tmp_path: Path, name: str, expected: str
) -> None:
    """本地下载路径应剥离远端文件名中的目录片段。"""
    storage = Rclone.__new__(Rclone)
    fileitem = schemas.FileItem(path=f"/remote/{expected}", name=name)

    local_path = storage._build_download_path(fileitem, tmp_path)

    assert local_path == tmp_path / expected
    assert local_path.resolve().relative_to(tmp_path.resolve()) == Path(expected)


@pytest.mark.parametrize("name", ["", ".", "..", "subdir/.."])
def test_build_download_path_rejects_unsafe_filename(
    tmp_path: Path, name: str
) -> None:
    """本地下载路径应拒绝无法安全落盘的文件名。"""
    storage = Rclone.__new__(Rclone)
    fileitem = schemas.FileItem(path="/remote/proof.txt", name=name)

    assert storage._build_download_path(fileitem, tmp_path) is None


def test_alipan_download_writes_sanitized_filename(tmp_path: Path) -> None:
    """阿里云盘下载应将路径穿越文件名写入目标目录内。"""
    alipan = AliPan.__new__(AliPan)
    alipan.chunk_size = 8192
    fileitem = schemas.FileItem(
        storage="alipan",
        type="file",
        path="/remote/proof.txt",
        name="../proof.txt",
        size=len(PAYLOAD),
        fileid="file-id",
        drive_id="drive-id",
    )

    with (
        patch.object(
            alipan,
            "_request_api",
            return_value={"url": "https://example.invalid/proof.txt"},
        ),
        patch.object(AliPan, "access_token", new_callable=PropertyMock, return_value=None),
        patch(
            "app.modules.alipan.alipan.transfer_process",
            return_value=_noop_progress,
        ),
        patch(
            "app.modules.alipan.alipan.global_vars.is_transfer_stopped",
            return_value=False,
        ),
        patch("app.modules.alipan.alipan.RequestUtils") as request_utils,
    ):
        request_utils.return_value.get_stream.return_value = _FakeAliPanStream(PAYLOAD)
        result = alipan.download(fileitem, path=tmp_path)

    expected_path = tmp_path / "proof.txt"
    assert result == expected_path
    assert expected_path.read_bytes() == PAYLOAD
    assert not (tmp_path.parent / "proof.txt").exists()


def test_u115_download_writes_sanitized_filename(tmp_path: Path) -> None:
    """115 下载应将路径穿越文件名写入目标目录内。"""
    u115 = U115Pan.__new__(U115Pan)
    u115.chunk_size = 8192
    u115.session = _FakeU115Session(PAYLOAD)
    detail = schemas.FileItem(size=len(PAYLOAD), pickcode="pick-code")
    fileitem = schemas.FileItem(
        storage="u115",
        type="file",
        path="/remote/proof.txt",
        name="../proof.txt",
        size=len(PAYLOAD),
    )

    with (
        patch.object(u115, "get_item", return_value=detail),
        patch.object(
            u115,
            "_request_api",
            return_value={"file-id": {"url": {"url": "https://example.invalid/proof.txt"}}},
        ),
        patch(
            "app.modules.u115.u115.transfer_process",
            return_value=_noop_progress,
        ),
        patch(
            "app.modules.u115.u115.global_vars.is_transfer_stopped",
            return_value=False,
        ),
    ):
        result = u115.download(fileitem, path=tmp_path)

    expected_path = tmp_path / "proof.txt"
    assert result == expected_path
    assert expected_path.read_bytes() == PAYLOAD
    assert not (tmp_path.parent / "proof.txt").exists()


def test_rclone_download_uses_sanitized_target_path(tmp_path: Path) -> None:
    """rclone 下载应把清洗后的本地路径传给 copyto。"""
    storage = Rclone.__new__(Rclone)
    fileitem = schemas.FileItem(
        storage="rclone",
        type="file",
        path="/remote/proof.txt",
        name="../proof.txt",
        size=len(PAYLOAD),
    )
    captured_cmd: dict[str, list[str]] = {}

    def fake_popen(cmd: list[str], *args: object, **kwargs: object) -> _FakeRcloneProcess:
        captured_cmd["cmd"] = cmd
        return _FakeRcloneProcess()

    with (
        patch(
            "app.modules.rclone.rclone.transfer_process",
            return_value=_noop_progress,
        ),
        patch("app.modules.rclone.rclone.subprocess.Popen", side_effect=fake_popen),
    ):
        result = storage.download(fileitem, path=tmp_path)

    expected_path = tmp_path / "proof.txt"
    assert result == expected_path
    assert captured_cmd["cmd"][-1] == str(expected_path)
    assert expected_path.resolve().relative_to(tmp_path.resolve()) == Path("proof.txt")
