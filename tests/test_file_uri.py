from app.schemas.file import FileURI


def test_from_uri_keeps_windows_drive_path() -> None:
    """Windows 盘符路径已是绝对路径，不能再补根斜杠，否则映射网络驱动器整理会报 WinError 123。"""
    file_uri = FileURI.from_uri("Z:/Downloads/电视剧/国产剧")

    assert file_uri.storage == "local"
    assert file_uri.path == "Z:/Downloads/电视剧/国产剧"
    assert file_uri.uri == "Z:/Downloads/电视剧/国产剧"


def test_from_uri_keeps_windows_drive_path_with_backslash() -> None:
    """反斜杠写法的盘符路径同样不能补根斜杠。"""
    file_uri = FileURI.from_uri("Z:\\Downloads\\电视剧")

    assert file_uri.storage == "local"
    assert not file_uri.path.startswith("/")


def test_from_uri_keeps_posix_absolute_path() -> None:
    """POSIX 绝对路径保持原样。"""
    file_uri = FileURI.from_uri("/downloads/movies")

    assert file_uri.storage == "local"
    assert file_uri.path == "/downloads/movies"


def test_from_uri_adds_root_for_relative_path() -> None:
    """无前导斜杠的相对路径仍补全为绝对路径。"""
    file_uri = FileURI.from_uri("downloads/movies")

    assert file_uri.path == "/downloads/movies"


def test_from_uri_parses_storage_prefix() -> None:
    """带存储前缀的 URI 应拆分出存储类型并保留 POSIX 路径。"""
    file_uri = FileURI.from_uri("u115:/media/anime")

    assert file_uri.storage == "u115"
    assert file_uri.path == "/media/anime"
    assert file_uri.uri == "u115:/media/anime"


def test_from_uri_storage_prefix_with_relative_path() -> None:
    """远端存储的无前导斜杠路径补全为绝对路径。"""
    file_uri = FileURI.from_uri("rclone:media/anime")

    assert file_uri.storage == "rclone"
    assert file_uri.path == "/media/anime"


def test_from_uri_keeps_path_of_unknown_storage_prefix() -> None:
    """未登记进 StorageSchema 的存储前缀同样拆分，路径不被吞进存储标识。"""
    file_uri = FileURI.from_uri("myfs:/media/anime")

    assert file_uri.storage == "myfs"
    assert file_uri.path == "/media/anime"
    assert file_uri.uri == "myfs:/media/anime"


def test_split_uri_reports_absent_storage_prefix() -> None:
    """无存储前缀时不臆造存储标识，路径原样返回。"""
    assert FileURI.split_uri("/downloads/movies") == (None, "/downloads/movies")
    assert FileURI.split_uri("downloads/movies") == (None, "downloads/movies")


def test_split_uri_does_not_take_windows_drive_as_storage() -> None:
    """单字母的 Windows 盘符不会被当成存储前缀。"""
    assert FileURI.split_uri("Z:/Downloads") == (None, "Z:/Downloads")
    assert FileURI.split_uri("Z:\\Downloads") == (None, "Z:\\Downloads")


def test_is_storage_scheme_requires_usable_identity() -> None:
    """存储标识须以字母开头且长度不小于 2，才能作为路径前缀。"""
    assert FileURI.is_storage_scheme("u115") is True
    assert FileURI.is_storage_scheme("my-fs.v2") is True
    assert FileURI.is_storage_scheme("z") is False
    assert FileURI.is_storage_scheme("1fs") is False
    assert FileURI.is_storage_scheme("my fs") is False
    assert FileURI.is_storage_scheme("") is False
