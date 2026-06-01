from pathlib import Path


from app.core.context import MediaInfo
from app.core.meta import MetaBase
from app.modules.filemanager import FileManagerModule
from app.schemas import FileItem, TransferDirectoryConf
from app.schemas.types import MediaType


class GuardedStorage:
    """
    用于验证预览模式不会访问可能有副作用的存储整理接口。
    """

    def get_folder(self, path: Path):  # pragma: no cover - 被调用即失败
        raise AssertionError(f"预览不应创建或获取目标目录：{path}")

    def get_item(self, path: Path):  # pragma: no cover - 被调用即失败
        raise AssertionError(f"预览不应探测目标文件：{path}")

    def copy(self, *args, **kwargs):  # pragma: no cover - 被调用即失败
        raise AssertionError("预览不应复制文件")

    def move(self, *args, **kwargs):  # pragma: no cover - 被调用即失败
        raise AssertionError("预览不应移动文件")

    def rename(self, *args, **kwargs):  # pragma: no cover - 被调用即失败
        raise AssertionError("预览不应重命名文件")

    def delete(self, *args, **kwargs):  # pragma: no cover - 被调用即失败
        raise AssertionError("预览不应删除文件")


def _build_meta(
    title: str,
    media_type: MediaType = MediaType.TV,
    season: int = 1,
    episode: int = 1,
    part: str = None,
):
    meta = MetaBase(title)
    meta.type = media_type
    meta.name = "Breaking Bad" if media_type == MediaType.TV else "Test Movie"
    meta.year = "2008" if media_type == MediaType.TV else "2026"
    meta.begin_season = season
    meta.begin_episode = episode
    meta.part = part
    return meta


def test_cloud_storage_preview_only_calculates_target_path():
    fileitem = FileItem(
        storage="alist",
        path="/downloads/Test.Show.S01E01.mkv",
        type="file",
        name="Test.Show.S01E01.mkv",
        basename="Test.Show.S01E01",
        extension="mkv",
        size=1024,
    )
    meta = _build_meta("Test.Show.S01E01.mkv", season=1, episode=1)
    mediainfo = MediaInfo(
        type=MediaType.TV,
        title="Test Show",
        year="2026",
        tmdb_id=12345,
    )
    target_directory = TransferDirectoryConf(
        name="cloud-library",
        transfer_type="copy",
        overwrite_mode="latest",
        library_path="/library",
        library_storage="alist",
        renaming=True,
        scraping=True,
        notify=True,
    )
    guarded_storage = GuardedStorage()

    transferinfo = FileManagerModule().transfer(
        fileitem=fileitem,
        meta=meta,
        mediainfo=mediainfo,
        target_directory=target_directory,
        source_oper=guarded_storage,
        target_oper=guarded_storage,
        preview=True,
    )

    assert transferinfo.success is True
    assert transferinfo.need_notify is False
    assert transferinfo.need_scrape is True
    assert transferinfo.target_item.storage == "alist"
    assert transferinfo.target_item.path.endswith(".mkv")
    assert transferinfo.target_diritem.path.startswith("/library/")
    assert transferinfo.file_list == [fileitem.path]
    assert transferinfo.file_list_new == [transferinfo.target_item.path]


def test_local_storage_preview_skips_target_conflict_checks(tmp_path):
    source_file = tmp_path / "downloads" / "Test.Show.S01E02.mkv"
    source_file.parent.mkdir(parents=True)
    source_file.write_bytes(b"test video")
    library_path = tmp_path / "library"
    fileitem = FileItem(
        storage="local",
        path=source_file.as_posix(),
        type="file",
        name=source_file.name,
        basename=source_file.stem,
        extension="mkv",
        size=source_file.stat().st_size,
    )
    meta = _build_meta(source_file.name, season=1, episode=2)
    mediainfo = MediaInfo(
        type=MediaType.TV,
        title="Test Show",
        year="2026",
        tmdb_id=12345,
    )
    target_directory = TransferDirectoryConf(
        name="local-library",
        transfer_type="copy",
        overwrite_mode="latest",
        library_path=library_path.as_posix(),
        library_storage="local",
        renaming=True,
        scraping=True,
        notify=True,
    )
    guarded_storage = GuardedStorage()

    transferinfo = FileManagerModule().transfer(
        fileitem=fileitem,
        meta=meta,
        mediainfo=mediainfo,
        target_directory=target_directory,
        source_oper=guarded_storage,
        target_oper=guarded_storage,
        preview=True,
    )

    assert transferinfo.success is True
    assert transferinfo.need_notify is False
    assert transferinfo.need_scrape is True
    assert transferinfo.target_item.storage == "local"
    assert transferinfo.target_item.path.startswith(library_path.as_posix())
    assert transferinfo.target_item.path.endswith(".mkv")
    assert transferinfo.file_list == [fileitem.path]
    assert transferinfo.file_list_new == [transferinfo.target_item.path]


def _build_bluray_dir_preview(
    source_name: str,
    media_type: MediaType,
    season: int = None,
    part: str = None,
):
    fileitem = FileItem(
        storage="alist",
        path=f"/downloads/{source_name}",
        type="dir",
        name=source_name,
        basename=source_name,
    )
    meta = _build_meta(
        fileitem.name,
        media_type=media_type,
        season=season,
        episode=None,
        part=part,
    )
    mediainfo = MediaInfo(
        type=media_type,
        title="Breaking Bad" if media_type == MediaType.TV else "Test Movie",
        year="2008" if media_type == MediaType.TV else "2026",
        tmdb_id=1396,
    )
    target_directory = TransferDirectoryConf(
        name="cloud-library",
        transfer_type="copy",
        overwrite_mode="never",
        library_path="/library",
        library_storage="alist",
        renaming=True,
        scraping=True,
        notify=True,
    )

    return FileManagerModule().transfer(
        fileitem=fileitem,
        meta=meta,
        mediainfo=mediainfo,
        target_directory=target_directory,
        source_oper=GuardedStorage(),
        target_oper=GuardedStorage(),
        preview=True,
    )


def test_tv_bluray_dir_preview_preserves_disk_folder_from_meta_part():
    transferinfo = _build_bluray_dir_preview(
        source_name="Breaking Bad Season 2 - Disk 1",
        media_type=MediaType.TV,
        season=2,
        part="Disk1",
    )

    assert transferinfo.success is True
    assert transferinfo.target_item.path == "/library/Breaking Bad (2008)/Season 2/Disc 1"
    assert transferinfo.target_diritem.path == transferinfo.target_item.path
    assert transferinfo.file_list_new == [transferinfo.target_item.path]


def test_tv_bluray_dir_preview_preserves_disc_folder_from_source_name():
    transferinfo = _build_bluray_dir_preview(
        source_name="BREAKING_BAD_S01D01",
        media_type=MediaType.TV,
        season=1,
    )

    assert transferinfo.success is True
    assert transferinfo.target_item.path == "/library/Breaking Bad (2008)/Season 1/Disc 1"


def test_tv_bluray_dir_preview_falls_back_to_source_name_without_disc_number():
    source_name = "Breaking Bad Season 3 Bonus Disc"
    transferinfo = _build_bluray_dir_preview(
        source_name=source_name,
        media_type=MediaType.TV,
        season=3,
    )

    assert transferinfo.success is True
    assert transferinfo.target_item.path == f"/library/Breaking Bad (2008)/Season 3/{source_name}"


def test_movie_bluray_dir_preview_keeps_movie_root_layout():
    transferinfo = _build_bluray_dir_preview(
        source_name="Test Movie Disc 1",
        media_type=MediaType.MOVIE,
        part="Disc1",
    )

    assert transferinfo.success is True
    assert transferinfo.target_item.path == "/library/Test Movie (2026)"
