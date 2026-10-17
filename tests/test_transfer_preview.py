"""
验证整理预览对真实文件操作的隔离。
"""

from pathlib import Path

from app.domain.context import MediaInfo
from app.domain.meta.metabase import MetaBase
from app.modules.filemanager.module import FileManagerModule
from app.schemas.file import FileItem
from app.schemas.system import TransferDirectoryConf
from app.schemas.types import MediaType


class GuardedStorage:
    """
    用于验证预览模式不会访问可能有副作用的存储整理接口。
    """

    # 被调用即失败。
    def get_folder(self, path: Path):  # pragma: no cover
        """
        拒绝创建或读取目标目录，检测预览阶段越界访问存储。
        """
        raise AssertionError(f"预览不应创建或获取目标目录：{path}")

    # 被调用即失败。
    def get_item(self, path: Path):  # pragma: no cover
        """
        拒绝探测目标文件，保证预览只计算目标路径。
        """
        raise AssertionError(f"预览不应探测目标文件：{path}")

    # 被调用即失败。
    def copy(self, *args, **kwargs):  # pragma: no cover
        """
        拒绝复制操作，防止预览产生真实文件写入。
        """
        raise AssertionError("预览不应复制文件")

    # 被调用即失败。
    def move(self, *args, **kwargs):  # pragma: no cover
        """
        拒绝移动操作，防止预览改变源文件位置。
        """
        raise AssertionError("预览不应移动文件")

    # 被调用即失败。
    def rename(self, *args, **kwargs):  # pragma: no cover
        """
        拒绝重命名操作，防止预览修改现有文件。
        """
        raise AssertionError("预览不应重命名文件")

    # 被调用即失败。
    def delete(self, *args, **kwargs):  # pragma: no cover
        """
        拒绝删除操作，防止预览清理任何文件。
        """
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
    """
    网盘整理预览只计算目标路径，不访问或修改目标存储。
    """
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

    module = FileManagerModule()
    checkpoint = module.plan_transfer(
        fileitem=fileitem,
        meta=meta,
        mediainfo=mediainfo,
        target_directory=target_directory,
        source_oper=guarded_storage,
        preview=True,
    )
    transferinfo = module.execute_transfer_plan(
        checkpoint,
        meta=meta,
        mediainfo=mediainfo,
        source_oper=guarded_storage,
        target_oper=guarded_storage,
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
    """
    本地整理预览跳过目标冲突探测，保持路径计算无写入副作用。
    """
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

    module = FileManagerModule()
    checkpoint = module.plan_transfer(
        fileitem=fileitem,
        meta=meta,
        mediainfo=mediainfo,
        target_directory=target_directory,
        source_oper=guarded_storage,
        preview=True,
    )
    transferinfo = module.execute_transfer_plan(
        checkpoint,
        meta=meta,
        mediainfo=mediainfo,
        source_oper=guarded_storage,
        target_oper=guarded_storage,
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

    module = FileManagerModule()
    source_oper = GuardedStorage()
    target_oper = GuardedStorage()
    checkpoint = module.plan_transfer(
        fileitem=fileitem,
        meta=meta,
        mediainfo=mediainfo,
        target_directory=target_directory,
        source_oper=source_oper,
        preview=True,
    )
    return module.execute_transfer_plan(
        checkpoint,
        meta=meta,
        mediainfo=mediainfo,
        source_oper=source_oper,
        target_oper=target_oper,
    )


def test_tv_bluray_dir_preview_preserves_disk_folder_from_meta_part():
    """
    电视剧蓝光目录预览从元数据分段信息保留光盘子目录。
    """
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
    """
    元数据没有光盘编号时，从源目录名提取并保留光盘子目录。
    """
    transferinfo = _build_bluray_dir_preview(
        source_name="BREAKING_BAD_S01D01",
        media_type=MediaType.TV,
        season=1,
    )

    assert transferinfo.success is True
    assert transferinfo.target_item.path == "/library/Breaking Bad (2008)/Season 1/Disc 1"


def test_tv_bluray_dir_preview_falls_back_to_source_name_without_disc_number():
    """
    无法识别光盘编号时保留源目录名，避免丢失蓝光目录层级。
    """
    source_name = "Breaking Bad Season 3 Bonus Disc"
    transferinfo = _build_bluray_dir_preview(
        source_name=source_name,
        media_type=MediaType.TV,
        season=3,
    )

    assert transferinfo.success is True
    assert transferinfo.target_item.path == f"/library/Breaking Bad (2008)/Season 3/{source_name}"


def test_movie_bluray_dir_preview_keeps_movie_root_layout():
    """
    电影蓝光目录直接落在电影根目录，不额外创建光盘层级。
    """
    transferinfo = _build_bluray_dir_preview(
        source_name="Test Movie Disc 1",
        media_type=MediaType.MOVIE,
        part="Disc1",
    )

    assert transferinfo.success is True
    assert transferinfo.target_item.path == "/library/Test Movie (2026)"
