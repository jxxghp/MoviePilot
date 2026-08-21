"""存储令牌比较：裸令牌保持既有判断结果，具名实例按类型与实例两级区分。"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.application.directory import DirectoryHelper, _normalize_download_path
from app.application.transferhandler import TransHandler
from app.schemas.file import FileURI
from app.schemas.types import StorageSchema

# 存量数据里可能出现的全部存储令牌取值，旧的存储前缀正则不含 @，因此存量取值一律是裸令牌
BARE_TOKENS = tuple(schema.value for schema in StorageSchema) + ("unknown", "myplugin")


@pytest.mark.parametrize("token", BARE_TOKENS)
def test_is_local_matches_raw_equality_for_bare_tokens(token: str) -> None:
    """裸令牌上的本地判断结果必须与改动前的字符串相等比较逐一相同。"""
    assert FileURI.is_local(token) == (token == "local")


@pytest.mark.parametrize("left", BARE_TOKENS)
@pytest.mark.parametrize("right", BARE_TOKENS)
def test_is_same_storage_matches_raw_equality_for_bare_tokens(left: str, right: str) -> None:
    """裸令牌之间的同实例判断结果必须与改动前的字符串相等比较逐一相同。"""
    assert FileURI.is_same_storage(left, right) == (left == right)
    assert FileURI.is_same_storage_type(left, right) == (left == right)


@pytest.mark.parametrize("token", BARE_TOKENS)
def test_storage_type_of_bare_token_is_the_token_itself(token: str) -> None:
    """裸令牌的类型部分就是它本身，按标识直取的既有调用不受影响。"""
    assert FileURI.storage_type(token) == token


def test_named_instance_is_recognized_as_its_storage_type() -> None:
    """具名实例的类型判断只看类型部分，local@nas 仍然是本地存储。"""
    assert FileURI.is_local("local@nas")
    assert FileURI.storage_type("local@nas") == "local"
    assert FileURI.storage_type("u115@work") == "u115"


def test_bare_token_and_named_token_are_different_instances() -> None:
    """裸令牌指默认实例，与同类型的具名令牌是两个实例。"""
    assert not FileURI.is_same_storage("u115", "u115@work")
    assert FileURI.is_same_storage_type("u115", "u115@work")


def test_named_instances_of_same_type_are_different_instances() -> None:
    """同类型的两个具名实例互不相同，同类型判断仍成立。"""
    assert not FileURI.is_same_storage("u115@work", "u115@home")
    assert FileURI.is_same_storage_type("u115@work", "u115@home")
    assert FileURI.is_same_storage("u115@work", "u115@work")


def test_different_types_are_neither_same_instance_nor_same_type() -> None:
    """不同存储类型在两级判断上都不相等。"""
    assert not FileURI.is_same_storage("u115@work", "alist@work")
    assert not FileURI.is_same_storage_type("u115@work", "alist@work")


@pytest.mark.parametrize(
    "malformed",
    ["local@", "@work", "u115@a b", "u115@a/b", "u115@a:b", "u115@a@b", "u@work"],
)
def test_malformed_token_matches_nothing(malformed: str) -> None:
    """写法非法的令牌不退回任何合法取值，也不与自身相等。"""
    assert FileURI.storage_type(malformed) == ""
    assert not FileURI.is_local(malformed)
    assert not FileURI.is_same_storage(malformed, malformed)
    assert not FileURI.is_same_storage_type(malformed, malformed)
    assert not FileURI.is_same_storage(malformed, "local")
    assert not FileURI.is_same_storage_type(malformed, "local")


@pytest.mark.parametrize("empty", [None, ""])
def test_empty_token_matches_nothing(empty) -> None:
    """空令牌不指向任何存储，不与任何取值相等。"""
    assert FileURI.storage_type(empty) == ""
    assert not FileURI.is_local(empty)
    assert not FileURI.is_same_storage(empty, empty)
    assert not FileURI.is_same_storage(empty, "local")


def test_named_local_instance_keeps_storage_prefix_in_uri() -> None:
    """具名本地实例必须保留存储前缀，否则回解析时会丢掉实例名。"""
    file_uri = FileURI(storage="local@nas", path="/media/movie")

    assert file_uri.uri == "local@nas:/media/movie"
    assert FileURI.from_uri(file_uri.uri).storage == "local@nas"
    assert FileURI(storage="local", path="/media/movie").uri == "/media/movie"


def test_windows_drive_path_accepted_for_named_local_instance() -> None:
    """具名本地实例同样走 Windows 盘符处理，不再被当成远端 POSIX 路径拒绝。"""
    assert _normalize_download_path("Z:/Downloads", "local@nas")[0] == "windows"
    assert _normalize_download_path("Z:/Downloads", "local")[0] == "windows"
    with pytest.raises(ValueError):
        _normalize_download_path("Z:/Downloads", "u115")


def test_same_source_requires_same_instance_for_remote_storage() -> None:
    """同盘优先只认同一个远端实例，同类型的不同实例不算同一存储盘。"""
    src = Path("/downloads/movie")
    tar = Path("/library/movie")

    assert DirectoryHelper._is_same_source((src, "u115@work"), (tar, "u115@work"))
    assert not DirectoryHelper._is_same_source((src, "u115@work"), (tar, "u115@home"))
    assert not DirectoryHelper._is_same_source((src, "u115"), (tar, "u115@work"))
    assert DirectoryHelper._is_same_source((src, "u115"), (tar, "u115"))


def _remote_opers(target_folder):
    """构造同一网盘整理分支所需的源、目标存储操作对象。"""
    source_oper = SimpleNamespace(
        is_support_transtype=lambda transfer_type: True,
        move=lambda fileitem, path, name: True,
    )
    target_oper = SimpleNamespace(
        get_folder=lambda path: target_folder,
        get_item=lambda path: None,
        get_item_strict=lambda path: None,
    )
    return source_oper, target_oper


def test_cross_instance_transfer_is_rejected() -> None:
    """同类型的跨实例转移不被支持，明确报错好过按同实例路径静默执行。"""
    source_item = FileURI(storage="u115@work", path="/downloads/Show.S01E01.mkv")
    new_item, errmsg = TransHandler._TransHandler__transfer_command(
        fileitem=SimpleNamespace(
            storage=source_item.storage, path=source_item.path, type="file", size=1024
        ),
        target_storage="u115@home",
        source_oper=SimpleNamespace(),
        target_oper=SimpleNamespace(),
        target_file=Path("/library/Show/Season 1/Show.S01E01.mkv"),
        transfer_type="move",
    )

    assert new_item is None
    assert errmsg == "不支持 u115@work 到 u115@home 的文件整理"


def test_same_named_instance_transfer_uses_same_storage_branch() -> None:
    """源与目标是同一个具名实例时走同一网盘分支，不因带实例名而被判为不支持。"""
    target_path = Path("/library/Show (2026)/Season 1/Show.S01E01.mkv")
    target_folder = SimpleNamespace(path=f"{target_path.parent.as_posix()}/")
    source_oper, target_oper = _remote_opers(target_folder)
    source_item = SimpleNamespace(
        storage="u115@work",
        path="/downloads/Show.S01E01.mkv",
        type="file",
        size=1024,
        extension="mkv",
        modify_time=1715939275.0,
        thumbnail=None,
    )

    new_item, errmsg = TransHandler._TransHandler__transfer_command(
        fileitem=source_item,
        target_storage="u115@work",
        source_oper=source_oper,
        target_oper=target_oper,
        target_file=target_path,
        transfer_type="move",
    )

    assert errmsg == ""
    assert new_item is not None
    assert new_item.storage == "u115@work"
    assert new_item.path == target_path.as_posix()


def test_bare_token_same_storage_transfer_still_supported() -> None:
    """裸令牌的同存储整理结果与改动前一致。"""
    target_path = Path("/library/Show (2026)/Season 1/Show.S01E01.mkv")
    target_folder = SimpleNamespace(path=f"{target_path.parent.as_posix()}/")
    source_oper, target_oper = _remote_opers(target_folder)
    source_item = SimpleNamespace(
        storage="alist",
        path="/downloads/Show.S01E01.mkv",
        type="file",
        size=1024,
        extension="mkv",
        modify_time=1715939275.0,
        thumbnail=None,
    )

    new_item, errmsg = TransHandler._TransHandler__transfer_command(
        fileitem=source_item,
        target_storage="alist",
        source_oper=source_oper,
        target_oper=target_oper,
        target_file=target_path,
        transfer_type="move",
    )

    assert errmsg == ""
    assert new_item is not None
    assert new_item.storage == "alist"


def test_named_local_instance_to_remote_takes_upload_branch() -> None:
    """具名本地实例到网盘走本地到网盘分支，报错内容证明已进入该分支而非被判为不支持。"""
    missing_path = "/nonexistent-local-instance/Show.S01E01.mkv"
    new_item, errmsg = TransHandler._TransHandler__transfer_command(
        fileitem=SimpleNamespace(
            storage="local@nas", path=missing_path, type="file", size=1024
        ),
        target_storage="alist",
        source_oper=SimpleNamespace(),
        target_oper=SimpleNamespace(),
        target_file=Path("/library/Show/Season 1/Show.S01E01.mkv"),
        transfer_type="copy",
    )

    assert new_item is None
    assert errmsg == f"文件 {missing_path} 不存在"
