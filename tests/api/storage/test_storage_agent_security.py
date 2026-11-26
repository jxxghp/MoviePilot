"""
验证 Agent 存储浏览只能落在已配置的目录根下。
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.endpoints import storage as storage_endpoint
from app.schemas.file import FileItem


def _directory(*, download_path="/media/downloads", storage="local", library_path=None, library_storage=None):
    """
    构造最小的目录配置桩。
    """
    return SimpleNamespace(
        download_path=download_path,
        storage=storage,
        library_path=library_path,
        library_storage=library_storage,
    )


def test_agent_storage_path_allowlist_rejects_root_and_parent_escape(monkeypatch, tmp_path):
    """
    根目录、配置根外路径和符号链接逃逸都必须被拒绝。
    """
    media_root = tmp_path / "media"
    download_root = media_root / "downloads"
    secret_root = tmp_path / "secret"
    download_root.mkdir(parents=True)
    secret_root.mkdir()
    link = download_root / "escape"
    link.symlink_to(secret_root, target_is_directory=True)
    monkeypatch.setattr(
        storage_endpoint.DirectoryHelper,
        "get_dirs",
        staticmethod(lambda: [_directory(download_path=str(download_root))]),
    )

    assert storage_endpoint._is_agent_storage_path_allowed(FileItem(path=str(download_root)))
    assert storage_endpoint._is_agent_storage_path_allowed(FileItem(path=str(download_root / "movie")))
    assert not storage_endpoint._is_agent_storage_path_allowed(FileItem(path=str(media_root)))
    assert not storage_endpoint._is_agent_storage_path_allowed(FileItem(path=str(download_root / ".." / "secret")))
    assert not storage_endpoint._is_agent_storage_path_allowed(FileItem(path=str(link)))


def test_agent_storage_remote_path_uses_configured_storage_root(monkeypatch):
    """
    远程存储按存储标识和配置根比较，不能跨存储或穿越根目录。
    """
    monkeypatch.setattr(
        storage_endpoint.DirectoryHelper,
        "get_dirs",
        staticmethod(lambda: [_directory(download_path="/library", storage="u115")]),
    )

    assert storage_endpoint._is_agent_storage_path_allowed(FileItem(storage="u115", path="/library"))
    assert storage_endpoint._is_agent_storage_path_allowed(FileItem(storage="u115", path="/library/movie"))
    assert not storage_endpoint._is_agent_storage_path_allowed(FileItem(storage="u115", path="/other"))
    assert not storage_endpoint._is_agent_storage_path_allowed(FileItem(storage="alipan", path="/library"))


def test_agent_storage_endpoint_fails_closed_before_provider_call(monkeypatch, tmp_path):
    """
    不在配置范围内时直接返回 403，不触发底层存储访问。
    """
    called = False

    class _StorageChain:
        def list_files(self, _fileitem):
            nonlocal called
            called = True
            return []

    monkeypatch.setattr(storage_endpoint.StorageChain, "list_files", _StorageChain().list_files)
    monkeypatch.setattr(
        storage_endpoint.DirectoryHelper,
        "get_dirs",
        staticmethod(lambda: [_directory(download_path=str(tmp_path / "allowed"))]),
    )

    with pytest.raises(HTTPException) as exc_info:
        storage_endpoint.list_agent_files(fileitem=FileItem(path="/"), _=object())

    assert exc_info.value.status_code == 403
    assert called is False


def test_manage_storage_guard_blocks_non_superuser_outside_configured_roots(monkeypatch, tmp_path):
    """
    非超级管理员不能借助文件管理器读写配置目录或构造穿越目标。
    """
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setattr(
        storage_endpoint.DirectoryHelper,
        "get_dirs",
        staticmethod(lambda: [_directory(download_path=str(allowed))]),
    )
    manage_user = SimpleNamespace(is_superuser=False)
    superuser = SimpleNamespace(is_superuser=True)

    storage_endpoint._ensure_manage_storage_path(FileItem(path=str(allowed)), manage_user)
    storage_endpoint._ensure_manage_storage_path(FileItem(path="/config"), superuser)
    assert storage_endpoint._is_safe_storage_child_name("movie")
    assert not storage_endpoint._is_safe_storage_child_name("../config")
    assert not storage_endpoint._is_safe_storage_child_name("movie/secret")

    with pytest.raises(HTTPException) as exc_info:
        storage_endpoint._ensure_manage_storage_path(FileItem(path="/config"), manage_user)
    assert exc_info.value.status_code == 403

    escaped = storage_endpoint._storage_child_item(FileItem(path=str(allowed)), "../config")
    assert not storage_endpoint._is_agent_storage_path_allowed(escaped)


def test_remote_object_identity_must_match_path_before_delete(monkeypatch):
    """
    合法路径搭配云盘根目录或其他文件 ID 时必须拒绝，不能调用提供方。
    """
    monkeypatch.setattr(
        storage_endpoint.DirectoryHelper,
        "get_dirs",
        staticmethod(lambda: [_directory(download_path="/library", storage="u115")]),
    )
    monkeypatch.setattr(
        storage_endpoint.StorageChain,
        "get_item",
        lambda _chain, _fileitem: FileItem(
            storage="u115",
            path="/library/",
            fileid="42",
            type="dir",
        ),
    )
    deleted = False

    def _delete_file(_chain, _fileitem):
        nonlocal deleted
        deleted = True
        return True

    monkeypatch.setattr(storage_endpoint.StorageChain, "delete_file", _delete_file)

    request_item = FileItem(storage="u115", path="/library", fileid="0", type="dir")
    with pytest.raises(HTTPException) as exc_info:
        storage_endpoint.delete(request_item, SimpleNamespace(is_superuser=False))

    assert exc_info.value.status_code == 403
    assert deleted is False


def test_verified_remote_object_is_used_for_provider_operation(monkeypatch):
    """
    通过核验后，底层必须收到服务端返回的对象，而不是客户端原始对象。
    """
    monkeypatch.setattr(
        storage_endpoint.DirectoryHelper,
        "get_dirs",
        staticmethod(lambda: [_directory(download_path="/library", storage="u115")]),
    )
    actual = FileItem(
        storage="u115",
        path="/library/",
        fileid="42",
        drive_id="drive-a",
        type="dir",
    )
    monkeypatch.setattr(
        storage_endpoint.StorageChain,
        "get_item",
        lambda _chain, _fileitem: actual,
    )
    received = []
    monkeypatch.setattr(
        storage_endpoint.StorageChain,
        "delete_file",
        lambda _chain, fileitem: received.append(fileitem) or True,
    )

    request_item = FileItem(
        storage="u115",
        path="/library",
        fileid="42",
        drive_id="drive-a",
        type="dir",
    )
    result = storage_endpoint.delete(request_item, SimpleNamespace(is_superuser=False))

    assert result.success is True
    assert received == [actual]


def test_rename_target_is_checked_in_source_parent(monkeypatch, tmp_path):
    """
    重命名目标必须是源对象同级路径，配置根目录不能被移出允许范围。
    """
    allowed = tmp_path / "library"
    allowed.mkdir()
    monkeypatch.setattr(
        storage_endpoint.DirectoryHelper,
        "get_dirs",
        staticmethod(lambda: [_directory(download_path=str(allowed))]),
    )
    source = FileItem(path=str(allowed), type="dir")
    target = storage_endpoint._storage_sibling_item(source, "moved")

    assert target.path == str(tmp_path / "moved")
    with pytest.raises(HTTPException) as exc_info:
        storage_endpoint._ensure_manage_storage_path(
            target,
            SimpleNamespace(is_superuser=False),
        )
    assert exc_info.value.status_code == 403


def test_remote_download_and_image_cleanup_temp_copy_while_preserving_local_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """
    验证远端存储在 ``TEMP_PATH`` 生成的下载/图片临时副本会在响应完成后删除，而本地存储源文件保持不变。
    """
    import asyncio

    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    local_root = tmp_path / "local_media"
    local_root.mkdir()
    local_source = local_root / "poster.jpg"
    local_source.write_bytes(b"local-jpeg")

    remote_download_tmp = temp_root / "remote_movie.mkv"
    remote_download_tmp.write_bytes(b"remote-video")
    remote_image_tmp = temp_root / "remote_cover.jpg"
    remote_image_tmp.write_bytes(b"remote-jpeg")

    monkeypatch.setattr(
        storage_endpoint,
        "get_api_runtime_config_snapshot",
        lambda: SimpleNamespace(temp_path=temp_root),
    )
    monkeypatch.setattr(storage_endpoint, "_ensure_manage_storage_item", lambda item, _u: item)

    # 1. 远端 download 返回带后台清理任务的 FileResponse，执行后删除 TEMP_PATH 临时文件
    monkeypatch.setattr(storage_endpoint.StorageChain, "download_file", lambda _c, _i: remote_download_tmp)
    dl_resp = storage_endpoint.download(
        FileItem(storage="rclone", type="file", path="/movies/remote_movie.mkv", name="remote_movie.mkv"),
        SimpleNamespace(is_superuser=True),
    )
    assert dl_resp.background is not None
    asyncio.run(dl_resp.background())
    assert not remote_download_tmp.exists()

    # 2. 远端 image 读取字节后立即清理 TEMP_PATH 临时文件
    monkeypatch.setattr(storage_endpoint.StorageChain, "download_file", lambda _c, _i: remote_image_tmp)
    img_resp = storage_endpoint.image(
        FileItem(storage="u115", type="file", path="/movies/remote_cover.jpg", name="remote_cover.jpg"),
        SimpleNamespace(is_superuser=True),
    )
    assert img_resp.body == b"remote-jpeg"
    assert not remote_image_tmp.exists()

    # 3. 本地存储原文件绝不能被清理任务删除
    monkeypatch.setattr(storage_endpoint.StorageChain, "download_file", lambda _c, _i: local_source)
    local_dl = storage_endpoint.download(
        FileItem(storage="local", type="file", path=str(local_source), name="poster.jpg"),
        SimpleNamespace(is_superuser=True),
    )
    assert local_dl.background is None
    local_img = storage_endpoint.image(
        FileItem(storage="local", type="file", path=str(local_source), name="poster.jpg"),
        SimpleNamespace(is_superuser=True),
    )
    assert local_img.body == b"local-jpeg"
    assert local_source.exists()
