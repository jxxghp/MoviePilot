import pytest
from pydantic import ValidationError

from app.schemas.file import STORAGE_INSTANCE_MAX_LENGTH, FileURI


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


def test_is_storage_instance_rejects_delimiters_and_blanks() -> None:
    """实例名不得为空、含分隔符或含空白，超长同样不合法。"""
    assert FileURI.is_storage_instance("work") is True
    assert FileURI.is_storage_instance("我的网盘") is True
    assert FileURI.is_storage_instance("a.b-c_1") is True
    assert FileURI.is_storage_instance("") is False
    assert FileURI.is_storage_instance("wo rk") is False
    assert FileURI.is_storage_instance("wo:rk") is False
    assert FileURI.is_storage_instance("wo/rk") is False
    assert FileURI.is_storage_instance("wo\\rk") is False
    assert FileURI.is_storage_instance("wo@rk") is False
    assert FileURI.is_storage_instance("a" * (STORAGE_INSTANCE_MAX_LENGTH + 1)) is False


def test_bare_token_means_default_instance() -> None:
    """裸令牌解析出的实例名为空，语义即该存储类型的默认实例。"""
    assert FileURI.split_storage("u115") == ("u115", None)
    assert FileURI.split_storage("local") == ("local", None)
    assert FileURI.split_storage("") == ("", None)


def test_split_storage_separates_instance_name() -> None:
    """带实例名的令牌拆出存储标识与实例名。"""
    assert FileURI.split_storage("u115@work") == ("u115", "work")
    assert FileURI.split_storage("rclone@我的网盘") == ("rclone", "我的网盘")


def test_join_storage_round_trips_split_storage() -> None:
    """拼接与拆分互为逆运算，未具名实例拼回裸标识。"""
    assert FileURI.join_storage("u115", "work") == "u115@work"
    assert FileURI.join_storage("u115", None) == "u115"
    assert FileURI.join_storage("u115", "") == "u115"
    for token in ("u115", "u115@work", "rclone@我的网盘"):
        assert FileURI.join_storage(*FileURI.split_storage(token)) == token


def test_from_uri_parses_storage_instance() -> None:
    """带实例名的 URI 拆分出完整令牌、存储标识与实例名。"""
    file_uri = FileURI.from_uri("u115@work:/media/anime")

    assert file_uri.storage == "u115@work"
    assert file_uri.storage_id == "u115"
    assert file_uri.storage_instance == "work"
    assert file_uri.path == "/media/anime"


def test_bare_uri_keeps_todays_result_and_reports_no_instance() -> None:
    """裸 URI 的解析结果与今天完全一致，实例名为空。"""
    file_uri = FileURI.from_uri("u115:/media/anime")

    assert file_uri.storage == "u115"
    assert file_uri.storage_id == "u115"
    assert file_uri.storage_instance is None
    assert file_uri.uri == "u115:/media/anime"


@pytest.mark.parametrize(
    "uri",
    [
        "/media/movie",
        "Z:/Downloads/电视剧",
        "u115:/media/anime",
        "u115@work:/media/anime",
        "rclone@我的网盘:/media/anime",
        "local@second:/media/anime",
    ],
)
def test_uri_round_trips(uri: str) -> None:
    """解析后再序列化必须得回原串。"""
    assert FileURI.from_uri(uri).uri == uri


@pytest.mark.parametrize(
    "uri",
    [
        "u115@:/media",
        "@work:/media",
        "u115@@work:/media",
        "u115@work@spare:/media",
        "u115@wo rk:/media",
        "u115@wo\trk:/media",
        f"u115@{'a' * (STORAGE_INSTANCE_MAX_LENGTH + 1)}:/media",
    ],
)
def test_malformed_instance_name_is_rejected(uri: str) -> None:
    """畸形实例名一律报错，不静默退回按无前缀路径解析。"""
    with pytest.raises(ValueError):
        FileURI.from_uri(uri)


def test_first_colon_terminates_the_storage_token() -> None:
    """实例名到首个冒号为止，冒号之后一律是路径，不参与令牌识别。"""
    assert FileURI.split_uri("u115@work:/media:tag") == ("u115@work", "/media:tag")
    assert FileURI.split_uri("u115@work:more:/media") == ("u115@work", "more:/media")


def test_instance_separator_only_recognized_in_storage_position() -> None:
    """路径里的 @ 不参与令牌识别，首个冒号之前含路径分隔符即视为路径本身。"""
    assert FileURI.split_uri("/media/foo@bar/x") == (None, "/media/foo@bar/x")
    assert FileURI.split_uri("/media/foo@bar:x") == (None, "/media/foo@bar:x")
    assert FileURI.from_uri("u115:/media/foo@bar").path == "/media/foo@bar"


def test_model_rejects_malformed_storage_token() -> None:
    """畸形令牌在模型边界即被拒绝，不会带着歧义写法流进后续调用。"""
    with pytest.raises(ValidationError):
        FileURI(storage="u115@", path="/media")
    with pytest.raises(ValidationError):
        FileURI(storage="u115@wo rk", path="/media")
