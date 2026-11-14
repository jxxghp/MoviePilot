"""
引用链接（reflink）存储能力、支持校验与目录配置同盘检查的单元测试。
"""

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional, Tuple

import pytest

from app.adapters.system.host import SystemUtils
from app.modules.filemanager.module import FileManagerModule
from app.modules.filemanager.storages import StorageBase
from app.modules.filemanager.storages.local import LocalStorage
from app.modules.filemanager.storages.rclone import Rclone
from app.modules.filemanager.storages.smb import SMB as SmbStorage
from app.schemas.file import FileItem


def _make_local_storage() -> LocalStorage:
    """
    构造绕过配置持久化初始化的本地存储实例。

    :return: 本地存储实例
    """
    return object.__new__(LocalStorage)


def test_local_storage_transtype_contains_reflink() -> None:
    """
    测试 LocalStorage.transtype 声明包含引用链接整理方式。
    """
    assert "reflink" in LocalStorage.transtype
    assert LocalStorage.transtype["reflink"] == "引用链接"


def test_local_storage_is_support_transtype_reflink() -> None:
    """
    测试 LocalStorage.is_support_transtype("reflink") 返回 True。
    """
    storage = _make_local_storage()
    assert storage.is_support_transtype("reflink") is True


def test_remote_storages_do_not_support_reflink() -> None:
    """
    测试非本地存储（如 SmbStorage 和 Rclone）的 is_support_transtype("reflink") 返回 False。
    """
    smb_storage = object.__new__(SmbStorage)
    assert smb_storage.is_support_transtype("reflink") is False

    rclone_storage = object.__new__(Rclone)
    assert rclone_storage.is_support_transtype("reflink") is False


def test_storage_base_reflink_defaults_to_false(tmp_path: Path) -> None:
    """
    测试 StorageBase 基类的 reflink 方法默认返回 False。
    """

    class _DummyStorage(StorageBase):
        """
        测试用的抽象基类存根实现。
        """

        def init_storage(self) -> None:
            pass

        def check(self) -> bool:
            return True

        def list(self, fileitem: FileItem) -> list[FileItem]:
            return []

        def create_folder(self, fileitem: FileItem, name: str) -> Optional[FileItem]:
            return None

        def get_folder(self, path: Path) -> Optional[FileItem]:
            return None

        def get_item(self, path: Path) -> Optional[FileItem]:
            return None

        def delete(self, fileitem: FileItem) -> bool:
            return False

        def rename(self, fileitem: FileItem, name: str) -> bool:
            return False

        def download(self, fileitem: FileItem, path: Optional[Path] = None) -> Optional[Path]:
            return None

        def upload(self, fileitem: FileItem, path: Path, new_name: Optional[str] = None) -> Optional[FileItem]:
            return None

        def detail(self, fileitem: FileItem) -> Optional[FileItem]:
            return None

        def copy(self, fileitem: FileItem, path: Path, new_name: str, **kwargs: Any) -> bool:
            return False

        def move(self, fileitem: FileItem, path: Path, new_name: str) -> bool:
            return False

        def link(self, fileitem: FileItem, target_file: Path) -> bool:
            return False

        def softlink(self, fileitem: FileItem, target_file: Path) -> bool:
            return False

        def usage(self) -> None:
            return None

    storage = object.__new__(_DummyStorage)
    fileitem = FileItem(path=str(tmp_path / "src.mkv"), name="src.mkv")
    target_path = tmp_path / "dst.mkv"
    result = storage.reflink(fileitem, target_path)
    assert result is False


def test_local_storage_reflink_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """
    测试 LocalStorage.reflink 成功时调用 SystemUtils.reflink 返回 True。
    """
    storage = _make_local_storage()
    source_file = tmp_path / "source.mkv"
    target_file = tmp_path / "target.mkv"
    fileitem = FileItem(path=str(source_file), name="source.mkv")

    called_args: dict[str, Path] = {}

    def fake_reflink(src: Path, dest: Path) -> Tuple[int, str]:
        called_args["src"] = src
        called_args["dest"] = dest
        return 0, ""

    monkeypatch.setattr(SystemUtils, "reflink", fake_reflink, raising=False)

    result = storage.reflink(fileitem, target_file)
    assert result is True
    assert called_args["src"] == source_file
    assert called_args["dest"] == target_file


def test_local_storage_reflink_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """
    测试 LocalStorage.reflink 失败时记录错误日志并返回 False。
    """
    storage = _make_local_storage()
    source_file = tmp_path / "source.mkv"
    target_file = tmp_path / "target.mkv"
    fileitem = FileItem(path=str(source_file), name="source.mkv")

    def fake_reflink_fail(src: Path, dest: Path) -> Tuple[int, str]:
        return -1, "Operation not supported on transport endpoint"

    monkeypatch.setattr(SystemUtils, "reflink", fake_reflink_fail, raising=False)

    logged_errors: list[str] = []
    monkeypatch.setattr(
        "app.modules.filemanager.storages.local.logger.error",
        lambda msg: logged_errors.append(msg),
    )

    result = storage.reflink(fileitem, target_file)
    assert result is False
    assert any("【本地】引用链接文件失败：Operation not supported on transport endpoint" in err for err in logged_errors)


def test_filemanager_check_reflink_same_disk(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """
    测试目录整理方式为 reflink 且同盘时校验通过。
    """
    module = FileManagerModule()
    download_dir = tmp_path / "downloads"
    library_dir = tmp_path / "library"
    download_dir.mkdir()
    library_dir.mkdir()

    dir_conf = SimpleNamespace(
        name="测试本地目录",
        download_path=str(download_dir),
        library_path=str(library_dir),
        storage="local",
        library_storage="local",
        transfer_type="reflink",
    )

    monkeypatch.setattr(module.directoryhelper, "get_dirs", lambda: [dir_conf])
    monkeypatch.setattr(SystemUtils, "is_same_disk", lambda p1, p2: True)
    storage_oper = _make_local_storage()
    monkeypatch.setattr(module, "_FileManagerModule__get_storage_oper", lambda storage: storage_oper)

    success, message = module.test()
    assert success is True
    assert message == ""


def test_filemanager_check_reflink_different_disk(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """
    测试目录整理方式为 reflink 但非同盘时校验失败并提示无法引用链接。
    """
    module = FileManagerModule()
    download_dir = tmp_path / "downloads"
    library_dir = tmp_path / "library"
    download_dir.mkdir()
    library_dir.mkdir()

    dir_conf = SimpleNamespace(
        name="测试本地目录",
        download_path=str(download_dir),
        library_path=str(library_dir),
        storage="local",
        library_storage="local",
        transfer_type="reflink",
    )

    monkeypatch.setattr(module.directoryhelper, "get_dirs", lambda: [dir_conf])
    monkeypatch.setattr(SystemUtils, "is_same_disk", lambda p1, p2: False)
    storage_oper = _make_local_storage()
    monkeypatch.setattr(module, "_FileManagerModule__get_storage_oper", lambda storage: storage_oper)

    success, message = module.test()
    assert success is False
    assert "不在同一磁盘，无法引用链接" in message


def test_filemanager_check_reflink_unsupported_storage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """
    测试非本地存储不支持 reflink 整理方式时校验失败。
    """
    module = FileManagerModule()
    download_dir = tmp_path / "downloads"
    library_dir = tmp_path / "library"
    download_dir.mkdir()
    library_dir.mkdir()

    dir_conf = SimpleNamespace(
        name="测试远端目录",
        download_path=str(download_dir),
        library_path=str(library_dir),
        storage="smb",
        library_storage="smb",
        transfer_type="reflink",
    )

    monkeypatch.setattr(module.directoryhelper, "get_dirs", lambda: [dir_conf])
    smb_storage = object.__new__(SmbStorage)
    monkeypatch.setattr(smb_storage, "check", lambda: True)
    monkeypatch.setattr(module, "_FileManagerModule__get_storage_oper", lambda storage: smb_storage)

    success, message = module.test()
    assert success is False
    assert "测试远端目录 的存储不支持 reflink 整理方式" in message


def test_local_storage_copy_prioritizes_reflink_on_same_disk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """
    测试本地复制在同文件系统且配置开启时，默认优先尝试引用链接。

    :param monkeypatch: pytest 猴子补丁夹具
    :param tmp_path: pytest 临时测试目录
    """
    storage = _make_local_storage()
    source = tmp_path / "src.bin"
    source.write_bytes(b"reflink copy content")
    target_dir = tmp_path / "dest"
    target_dir.mkdir()

    reflink_called = []

    def _mock_reflink(src: Path, dest: Path) -> Tuple[int, str]:
        reflink_called.append((src, dest))
        dest.write_bytes(src.read_bytes())
        return 0, ""

    monkeypatch.setattr(SystemUtils, "reflink", _mock_reflink)
    monkeypatch.setattr(SystemUtils, "is_same_disk", lambda _s, _d: True)
    monkeypatch.setattr(
        "app.modules.filemanager.storages.local.get_runtime_setting",
        lambda key, default=True: True if key == "TRANSFER_COPY_TRY_REFLINK" else default,
    )

    fileitem = FileItem(storage="local", path=source.as_posix(), name="src.bin")
    success = storage.copy(fileitem, target_dir, "dest.bin")

    assert success is True
    assert len(reflink_called) == 1
    assert (target_dir / "dest.bin").read_bytes() == b"reflink copy content"


def test_local_storage_copy_falls_back_when_reflink_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """
    测试本地复制在引用链接失败时，自动回退到常规文件复制。

    :param monkeypatch: pytest 猴子补丁夹具
    :param tmp_path: pytest 临时测试目录
    """
    storage = _make_local_storage()
    source = tmp_path / "src.bin"
    source.write_bytes(b"fallback copy content")
    target_dir = tmp_path / "dest"
    target_dir.mkdir()

    reflink_called = []

    def _failing_reflink(src: Path, dest: Path) -> Tuple[int, str]:
        reflink_called.append((src, dest))
        return -1, "Operation not supported"

    monkeypatch.setattr(SystemUtils, "reflink", _failing_reflink)
    monkeypatch.setattr(SystemUtils, "is_same_disk", lambda _s, _d: True)

    fileitem = FileItem(storage="local", path=source.as_posix(), name="src.bin")
    success = storage.copy(fileitem, target_dir, "dest.bin")

    assert success is True
    assert len(reflink_called) == 1
    assert (target_dir / "dest.bin").read_bytes() == b"fallback copy content"


def test_local_storage_copy_skips_reflink_when_disabled_via_argument(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """
    测试显式传入 try_reflink=False 时跳过引用链接尝试，直接走常规复制。

    :param monkeypatch: pytest 猴子补丁夹具
    :param tmp_path: pytest 临时测试目录
    """
    storage = _make_local_storage()
    source = tmp_path / "src.bin"
    source.write_bytes(b"explicit false copy content")
    target_dir = tmp_path / "dest"
    target_dir.mkdir()

    reflink_called = []

    def _mock_reflink(src: Path, dest: Path) -> Tuple[int, str]:
        reflink_called.append((src, dest))
        return 0, ""

    monkeypatch.setattr(SystemUtils, "reflink", _mock_reflink)
    monkeypatch.setattr(SystemUtils, "is_same_disk", lambda _s, _d: True)
    monkeypatch.setattr(
        "app.modules.filemanager.storages.local.get_runtime_setting",
        lambda key, default=True: True if key == "TRANSFER_COPY_TRY_REFLINK" else default,
    )

    fileitem = FileItem(storage="local", path=source.as_posix(), name="src.bin")
    success = storage.copy(fileitem, target_dir, "dest.bin", try_reflink=False)

    assert success is True
    assert len(reflink_called) == 0
    assert (target_dir / "dest.bin").read_bytes() == b"explicit false copy content"


def test_local_storage_copy_rejects_positional_try_reflink(tmp_path: Path) -> None:
    """
    测试 LocalStorage.copy 拒绝以位置参数形式传入 try_reflink，抛出 TypeError。

    :param tmp_path: pytest 临时测试目录
    """
    storage = _make_local_storage()
    fileitem = FileItem(storage="local", path=str(tmp_path / "src.bin"), name="src.bin")
    with pytest.raises(TypeError):
        storage.copy(fileitem, tmp_path, "dest.bin", False)  # type: ignore[misc]


def test_local_storage_copy_skips_reflink_when_disabled_via_runtime_setting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """
    测试运行期配置 TRANSFER_COPY_TRY_REFLINK 为 False 时，默认传参跳过引用链接探测。

    :param monkeypatch: pytest 猴子补丁夹具
    :param tmp_path: pytest 临时测试目录
    """
    storage = _make_local_storage()
    source = tmp_path / "src.bin"
    source.write_bytes(b"runtime setting false copy content")
    target_dir = tmp_path / "dest"
    target_dir.mkdir()

    reflink_called = []

    def _mock_reflink(src: Path, dest: Path) -> Tuple[int, str]:
        reflink_called.append((src, dest))
        return 0, ""

    monkeypatch.setattr(SystemUtils, "reflink", _mock_reflink)
    monkeypatch.setattr(SystemUtils, "is_same_disk", lambda _s, _d: True)
    monkeypatch.setattr(
        "app.modules.filemanager.storages.local.get_runtime_setting",
        lambda key, default=True: False if key == "TRANSFER_COPY_TRY_REFLINK" else default,
    )

    fileitem = FileItem(storage="local", path=source.as_posix(), name="src.bin")
    success = storage.copy(fileitem, target_dir, "dest.bin")

    assert success is True
    assert len(reflink_called) == 0
    assert (target_dir / "dest.bin").read_bytes() == b"runtime setting false copy content"


def test_local_storage_copy_skips_reflink_on_different_disks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """
    测试跨磁盘复制时不尝试引用链接，直接走常规复制。

    :param monkeypatch: pytest 猴子补丁夹具
    :param tmp_path: pytest 临时测试目录
    """
    storage = _make_local_storage()
    source = tmp_path / "src.bin"
    source.write_bytes(b"cross disk copy content")
    target_dir = tmp_path / "dest"
    target_dir.mkdir()

    reflink_called = []

    def _mock_reflink(src: Path, dest: Path) -> Tuple[int, str]:
        reflink_called.append((src, dest))
        return 0, ""

    monkeypatch.setattr(SystemUtils, "reflink", _mock_reflink)
    monkeypatch.setattr(SystemUtils, "is_same_disk", lambda _s, _d: False)

    fileitem = FileItem(storage="local", path=source.as_posix(), name="src.bin")
    success = storage.copy(fileitem, target_dir, "dest.bin")

    assert success is True
    assert len(reflink_called) == 0
    assert (target_dir / "dest.bin").read_bytes() == b"cross disk copy content"


def test_system_utils_copy_keyword_only_and_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """
    测试 SystemUtils.copy 强制仅限关键字参数、优先引用链接及回退常规复制。

    :param monkeypatch: pytest 猴子补丁夹具
    :param tmp_path: pytest 临时测试目录
    """
    source = tmp_path / "src.bin"
    source.write_bytes(b"system utils copy test")
    dest1 = tmp_path / "dest1.bin"
    dest2 = tmp_path / "dest2.bin"
    dest3 = tmp_path / "dest3.bin"

    # 1. 验证拒绝位置传参 try_reflink
    with pytest.raises(TypeError):
        SystemUtils.copy(source, dest1, False)  # type: ignore[misc]

    # 2. 验证显式 try_reflink=False 时跳过 reflink
    reflink_calls = []

    def _mock_reflink(src: Path, dest: Path) -> Tuple[int, str]:
        reflink_calls.append((src, dest))
        if dest == dest1:
            dest.write_bytes(src.read_bytes())
            return 0, ""
        return -1, "CoW not supported"

    monkeypatch.setattr(SystemUtils, "reflink", _mock_reflink)
    monkeypatch.setattr(SystemUtils, "is_same_disk", lambda _s, _d: True)

    code_skip, msg_skip = SystemUtils.copy(source, dest3, try_reflink=False)
    assert code_skip == 0
    assert msg_skip == ""
    assert dest3.read_bytes() == b"system utils copy test"
    assert len(reflink_calls) == 0

    # 3. 验证默认优先尝试 reflink 并成功
    code1, msg1 = SystemUtils.copy(source, dest1)
    assert code1 == 0
    assert msg1 == ""
    assert dest1.read_bytes() == b"system utils copy test"
    assert len(reflink_calls) == 1

    # 4. 验证 reflink 失败时自动回退常规复制
    code2, msg2 = SystemUtils.copy(source, dest2)
    assert code2 == 0
    assert msg2 == ""
    assert dest2.read_bytes() == b"system utils copy test"
    assert len(reflink_calls) == 2
