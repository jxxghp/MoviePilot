"""媒体库文件系统扩展经宿主服务端口取用目录配置、存储配置与命名上下文的行为。"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.application.directory import DirectoryHelper
from app.application.messaging.message import NamingContextService
from app.application.storage import StorageHelper
from app.domain.mediapath import resolve_media_root_path
from app.domain.meta.metavideo import MetaVideo
from app.domain.context import MediaInfo
from app.modules import medialibrary
from app.modules.medialibrary import MediaLibraryModule, get_media_root_path
from app.modules.localstorage.local import LocalStorage
from app.application.transferhandler import TransHandler
from app.runtime.hostports.directories import directory_config_port
from app.runtime.hostports.naming import naming_context_port
from app.runtime.hostports.storages import storage_config_port
from app.schemas.system import StorageConf, TransferDirectoryConf
from app.schemas.types import MediaType
from app.startup.hostport_initializer import configure_host_ports


@pytest.fixture
def restore_host_ports():
    """用例可随意改写端口注册，结束后恢复组合根装配。"""
    yield
    configure_host_ports()


def test_ports_are_wired_by_composition_root():
    """组合根装配后，三个端口都应解析到应用服务实现。"""
    assert directory_config_port.registered
    assert storage_config_port.registered
    assert naming_context_port.registered
    assert isinstance(directory_config_port.resolve(), DirectoryHelper)
    assert isinstance(storage_config_port.resolve(), StorageHelper)
    assert isinstance(naming_context_port.resolve(), NamingContextService)


@pytest.mark.parametrize(
    "port, port_name",
    [
        (directory_config_port, "目录配置"),
        (storage_config_port, "存储配置"),
        (naming_context_port, "命名上下文"),
    ],
)
def test_unregistered_port_reports_which_service_is_missing(port, port_name, restore_host_ports):
    """未注册时报错须点名具体端口，而不是抛出难以定位的属性错误。"""
    port.reset()

    assert not port.registered
    with pytest.raises(RuntimeError) as err:
        port.resolve()
    assert port_name in str(err.value)


def test_module_test_reads_directories_through_port(restore_host_ports):
    """模块连通性检查须读取端口提供的目录配置。"""
    directory_config_port.register(lambda: SimpleNamespace(get_dirs=lambda: []))

    assert MediaLibraryModule().test() == (False, "未设置任何目录")


def test_media_files_reads_library_dirs_through_port(restore_host_ports):
    """媒体库文件检索须读取端口提供的媒体库目录。"""
    calls = []

    def _get_library_dirs():
        """记录一次媒体库目录查询并返回空配置。"""
        calls.append(True)
        return []

    directory_config_port.register(
        lambda: SimpleNamespace(get_library_dirs=_get_library_dirs)
    )
    media = MediaInfo()
    media.type = MediaType.MOVIE

    assert MediaLibraryModule().media_files(media) == []
    assert calls == [True]


def test_local_storage_usage_reads_local_dirs_through_port(restore_host_ports):
    """本地存储空间统计须读取端口提供的本地下载与媒体库目录。"""
    download_dir = TransferDirectoryConf(download_path="/downloads", storage="local")
    library_dir = TransferDirectoryConf(library_path="/library", library_storage="local")
    directory_config_port.register(lambda: SimpleNamespace(
        get_local_download_dirs=lambda: [download_dir],
        get_local_library_dirs=lambda: [library_dir],
    ))

    usage = LocalStorage().usage()

    assert usage is not None
    assert usage.total >= 0


def test_storage_config_is_read_and_written_through_port(restore_host_ports):
    """存储配置的读、写、重置均须落到端口提供的实现上。"""
    written = {}
    reset = []
    directory_config_port.register(lambda: SimpleNamespace())
    storage_config_port.register(lambda: SimpleNamespace(
        get_storage=lambda storage: StorageConf(type=storage, config={"token": "x"}),
        set_storage=lambda storage, conf: written.update({storage: conf}),
        reset_storage=lambda storage: reset.append(storage),
    ))
    storage = LocalStorage()

    assert storage.get_conf() == {"token": "x"}
    storage.set_config({"token": "y"})
    storage.reset_config()

    assert written == {"local": {"token": "y"}}
    assert reset == ["local"]


def test_storage_config_key_carries_the_instance_of_a_named_backend(restore_host_ports):
    """具名实例的配置读写键带实例名，承接裸令牌的那一份仍用裸存储标识。"""
    written = {}
    directory_config_port.register(lambda: SimpleNamespace())
    storage_config_port.register(lambda: SimpleNamespace(
        get_storage=lambda storage: StorageConf(type=storage, config={}),
        set_storage=lambda storage, conf: written.update({storage: conf}),
        reset_storage=lambda storage: None,
    ))
    named = LocalStorage(storage_instance="备份盘")
    default_named = LocalStorage(storage_instance="主盘")
    default_named.storage_is_bare_token = True

    named.set_config({"token": "y"})
    default_named.set_config({"token": "z"})

    assert written == {"local@备份盘": {"token": "y"}, "local": {"token": "z"}}


def test_naming_dict_is_built_through_port(restore_host_ports):
    """重命名变量须由端口提供的命名上下文构建，并继续隐藏统一媒体身份变量。"""
    captured = {}

    def _build_naming_context(**kwargs):
        """记录调用参数并返回含内部身份变量的上下文。"""
        captured.update(kwargs)
        return {"title": "Test Show", "media_source": "tmdb", "media_id": "1"}

    naming_context_port.register(
        lambda: SimpleNamespace(build_naming_context=_build_naming_context)
    )
    meta = MetaVideo("Test.Show.S01E01")
    media = MediaInfo()

    naming_dict = TransHandler.get_naming_dict(meta=meta, mediainfo=media, file_ext=".mkv")

    assert naming_dict == {"title": "Test Show"}
    assert captured["meta"] is meta
    assert captured["mediainfo"] is media
    assert captured["file_extension"] == ".mkv"


def test_naming_context_service_builds_rename_variables():
    """命名上下文服务须为重命名提供可用的媒体变量。"""
    meta = MetaVideo("Test.Show.S01E01")
    media = MediaInfo()
    media.type = MediaType.TV
    media.title = "Test Show"
    media.year = "2020"

    context = NamingContextService().build_naming_context(
        meta=meta,
        mediainfo=media,
        file_extension=".mkv",
    )

    assert context.get("title") == "Test Show"
    assert context.get("fileExt") == ".mkv"


def test_media_root_resolution_reports_problems_without_logging():
    """媒体根路径推导只返回结果与问题描述，不承担日志输出。"""
    empty = resolve_media_root_path("", Path("/library/Test Show/file.mkv"))
    assert empty.path is None
    assert empty.error == "重命名格式不能为空"

    mismatch = resolve_media_root_path(
        "{{title}}/Season {{season}}/{{title}} - {{season_episode}}{{fileExt}}",
        Path("/file.mkv"),
    )
    assert mismatch.path is None
    assert "不匹配重命名格式" in mismatch.error

    untitled = resolve_media_root_path("plain/{{fileExt}}", Path("/library/show/file.mkv"))
    assert untitled.path == Path("/library/show")
    assert untitled.warning == "重命名格式 plain/{{fileExt}} 缺少标题目录"


def test_media_root_helper_logs_reported_problems(monkeypatch):
    """整理侧取用媒体根路径时须把推导问题按级别写入日志。"""
    records = []
    monkeypatch.setattr(medialibrary, "logger", SimpleNamespace(
        warn=lambda text: records.append(("warn", text)),
        error=lambda text: records.append(("error", text)),
    ))

    assert medialibrary.get_media_root_path("", Path("/library/file.mkv")) is None
    assert records == [("error", "重命名格式不能为空")]

    records.clear()
    assert medialibrary.get_media_root_path(
        "plain/{{fileExt}}", Path("/library/show/file.mkv")
    ) == Path("/library/show")
    assert records == [("warn", "重命名格式 plain/{{fileExt}} 缺少标题目录")]


def test_media_root_helper_keeps_music_album_directory():
    """音乐媒体根路径须稳定落在专辑层。"""
    album_dir = Path("/library/Daft Punk/Random Access Memories (2013)")

    assert get_media_root_path(
        "{{title}}/{{album}}/{{track}}{{fileExt}}",
        album_dir / "Disc 1" / "08 - Get Lucky.flac",
        media_type=MediaType.MUSIC,
    ) == album_dir
