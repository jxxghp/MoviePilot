"""
引用链接（reflink）传输管道规划、执行整理与步骤观察集成测试。
"""

import filecmp
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.adapters.system.host import SystemUtils
from app.application.transfer.execution import TransferOperationObservationState
from app.application.transfer.workflow import (
    TransferPlanCheckpoint,
    TransferPlanningInput,
)
from app.domain.context import MediaInfo
from app.domain.meta.metabase import MetaBase
from app.modules.filemanager.storages import StorageBase
from app.modules.filemanager.storages.local import LocalStorage
from app.modules.filemanager.transhandler import TransHandler
from app.schemas.file import FileItem
from app.schemas.types import MediaType


def _make_local_storage() -> LocalStorage:
    """
    构造绕过配置持久化初始化的本地存储适配器实例。

    :return: LocalStorage 实例
    """
    return object.__new__(LocalStorage)


def test_transhandler_plan_and_execute_reflink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    测试 TransHandler 使用 transfer_type="reflink" 规划并执行本地文件整理。

    验证整理执行成功，目标文件被创建，源文件与目标文件内容完全一致，
    且属于不同 Inode（非硬链接），也不是符号链接。

    :param tmp_path: pytest 临时测试目录
    :param monkeypatch: pytest 猴子补丁夹具
    """
    download_dir = tmp_path / "downloads"
    library_dir = tmp_path / "library"
    download_dir.mkdir(parents=True, exist_ok=True)
    library_dir.mkdir(parents=True, exist_ok=True)

    source_file = download_dir / "Test.Movie.2026.mkv"
    source_content = b"MoviePilot reflink test content - " + b"M" * 2048
    source_file.write_bytes(source_content)

    storage = _make_local_storage()
    source_item = storage.get_item_strict(source_file)
    assert source_item is not None

    planning_input = TransferPlanningInput(
        source_fileitem=source_item.model_dump(mode="json"),
    )

    handler = TransHandler()
    monkeypatch.setattr(handler, "_TransHandler__intercept_transfer", lambda **_kwargs: (True, ""))
    monkeypatch.setattr(LocalStorage, "_LocalStorage__should_show_progress", lambda *_args: False)

    meta = MetaBase("Test.Movie.2026.mkv")
    mediainfo = MediaInfo(type=MediaType.MOVIE, title="Test Movie", year="2026")

    # 规划引用链接整理
    checkpoint = handler.plan_transfer(
        planning_input,
        meta=meta,
        mediainfo=mediainfo,
        source_oper=storage,
        target_storage="local",
        target_path=library_dir,
        transfer_type="reflink",
        need_scrape=False,
        need_rename=False,
        need_notify=False,
        overwrite_mode=None,
        episodes_info=None,
        preview=False,
    )

    assert isinstance(checkpoint, TransferPlanCheckpoint)
    assert checkpoint.resolved_transfer_type == "reflink"
    assert checkpoint.skip_reason is None

    # 执行整理计划
    transfer_info = handler.execute_transfer_plan(
        checkpoint,
        meta=meta,
        mediainfo=mediainfo,
        source_oper=storage,
        target_oper=storage,
    )

    assert transfer_info.success is True

    target_file = Path(checkpoint.final_target_path)
    # 验证目标文件已创建且为常规文件
    assert target_file.exists()
    assert target_file.is_file()
    # 验证不是软链接
    assert not target_file.is_symlink()
    # 验证源文件和目标文件不同 Inode（独立引用）
    assert not target_file.samefile(source_file)
    # 验证内容完全一致
    assert target_file.read_bytes() == source_content
    assert filecmp.cmp(source_file, target_file, shallow=False)


def test_transhandler_reflink_overwrite_always(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    测试在覆盖模式 overwrite_mode="always" 下，reflink 能够安全覆盖目标文件。

    :param tmp_path: pytest 临时测试目录
    :param monkeypatch: pytest 猴子补丁夹具
    """
    download_dir = tmp_path / "downloads"
    library_dir = tmp_path / "library"
    download_dir.mkdir(parents=True, exist_ok=True)
    library_dir.mkdir(parents=True, exist_ok=True)

    source_file = download_dir / "Movie.2026.mkv"
    target_file = library_dir / "Movie.2026.mkv"

    new_content = b"new reflink movie version - " + b"N" * 1024
    old_content = b"old existing movie version - " + b"O" * 512

    source_file.write_bytes(new_content)
    target_file.write_bytes(old_content)

    storage = _make_local_storage()
    source_item = storage.get_item_strict(source_file)
    assert source_item is not None

    planning_input = TransferPlanningInput(
        source_fileitem=source_item.model_dump(mode="json"),
    )

    handler = TransHandler()
    monkeypatch.setattr(handler, "_TransHandler__intercept_transfer", lambda **_kwargs: (True, ""))
    monkeypatch.setattr(LocalStorage, "_LocalStorage__should_show_progress", lambda *_args: False)

    meta = MetaBase("Movie.2026.mkv")
    mediainfo = MediaInfo(type=MediaType.MOVIE, title="Movie", year="2026")

    # 规划覆盖整理计划
    checkpoint = handler.plan_transfer(
        planning_input,
        meta=meta,
        mediainfo=mediainfo,
        source_oper=storage,
        target_storage="local",
        target_path=library_dir,
        transfer_type="reflink",
        need_scrape=False,
        need_rename=False,
        need_notify=False,
        overwrite_mode="always",
        episodes_info=None,
        preview=False,
    )

    assert checkpoint.resolved_transfer_type == "reflink"
    assert checkpoint.overwrite_mode == "always"

    # 执行覆盖整理
    transfer_info = handler.execute_transfer_plan(
        checkpoint,
        meta=meta,
        mediainfo=mediainfo,
        source_oper=storage,
        target_oper=storage,
    )

    assert transfer_info.success is True

    # 验证旧目标已被新版本的 reflink 内容原子替换
    assert target_file.exists()
    assert target_file.is_file()
    assert not target_file.is_symlink()
    assert not target_file.samefile(source_file)
    assert target_file.read_bytes() == new_content
    assert filecmp.cmp(source_file, target_file, shallow=False)


def test_transhandler_observe_reflink_operation_states(
    tmp_path: Path,
) -> None:
    """
    测试步骤观察器 __observe_transfer_operation 对 reflink 整理方式的状态判定。

    验证：
    1. 目标正常独立且内容一致时返回 APPLIED。
    2. 目标文件缺失时返回 NOT_APPLIED。
    3. 目标文件内容不一致时返回 CONFLICT。
    4. 目标文件为同一 Inode（硬链接）时返回 CONFLICT。
    5. 目标文件为符号链接（软链接）时返回 CONFLICT。

    :param tmp_path: pytest 临时测试目录
    """
    source_dir = tmp_path / "src"
    target_dir = tmp_path / "dst"
    source_dir.mkdir(parents=True, exist_ok=True)
    target_dir.mkdir(parents=True, exist_ok=True)

    source_file = source_dir / "sample.mkv"
    source_content = b"reflink observation test data"
    source_file.write_bytes(source_content)

    storage = _make_local_storage()
    source_item = storage.get_item_strict(source_file)
    assert source_item is not None

    # 1. 成功创建的真实 reflink 文件 -> APPLIED
    reflink_target = target_dir / "sample_reflink.mkv"
    code, msg = SystemUtils.reflink(source_file, reflink_target)
    assert code == 0, f"创建 reflink 失败: {msg}"

    obs_applied = TransHandler._TransHandler__observe_transfer_operation(
        fileitem=source_item,
        target_storage="local",
        source_oper=storage,
        target_oper=storage,
        target_file=reflink_target,
        transfer_type="reflink",
    )
    assert obs_applied.state == TransferOperationObservationState.APPLIED

    # 2. 目标文件不存在 -> NOT_APPLIED
    missing_target = target_dir / "non_existent.mkv"
    obs_missing = TransHandler._TransHandler__observe_transfer_operation(
        fileitem=source_item,
        target_storage="local",
        source_oper=storage,
        target_oper=storage,
        target_file=missing_target,
        transfer_type="reflink",
    )
    assert obs_missing.state == TransferOperationObservationState.NOT_APPLIED

    # 3. 目标文件内容与源文件不一致 -> CONFLICT
    diff_target = target_dir / "diff.mkv"
    diff_target.write_bytes(b"different content")
    obs_diff = TransHandler._TransHandler__observe_transfer_operation(
        fileitem=source_item,
        target_storage="local",
        source_oper=storage,
        target_oper=storage,
        target_file=diff_target,
        transfer_type="reflink",
    )
    assert obs_diff.state == TransferOperationObservationState.CONFLICT

    # 4. 目标文件为硬链接（同一 Inode） -> CONFLICT
    hardlink_target = target_dir / "hardlink.mkv"
    os.link(source_file, hardlink_target)
    obs_hardlink = TransHandler._TransHandler__observe_transfer_operation(
        fileitem=source_item,
        target_storage="local",
        source_oper=storage,
        target_oper=storage,
        target_file=hardlink_target,
        transfer_type="reflink",
    )
    assert obs_hardlink.state == TransferOperationObservationState.CONFLICT

    # 5. 目标文件为符号链接 -> CONFLICT
    symlink_target = target_dir / "symlink.mkv"
    symlink_target.symlink_to(source_file)
    obs_symlink = TransHandler._TransHandler__observe_transfer_operation(
        fileitem=source_item,
        target_storage="local",
        source_oper=storage,
        target_oper=storage,
        target_file=symlink_target,
        transfer_type="reflink",
    )
    assert obs_symlink.state == TransferOperationObservationState.CONFLICT


def test_transhandler_same_storage_reflink_dispatch(
    tmp_path: Path,
) -> None:
    """
    测试同远端/自定义存储分支中 transfer_type="reflink" 的分发逻辑。

    验证：
    1. 当存储实现 reflink 并成功时，返回新目标项。
    2. 当存储实现 reflink 但返回失败时，返回错误信息。
    3. 当存储未声明支持 reflink 整理方式时，返回不支持提示。

    :param tmp_path: pytest 临时测试目录
    """
    file_path = "/remote/path/source.mkv"
    target_path = Path("/remote/path/target.mkv")
    fileitem = FileItem(storage="custom", path=file_path, name="source.mkv")

    # 创建模拟存储适配器
    mock_oper = MagicMock(spec=StorageBase)
    mock_oper.is_support_transtype.return_value = True

    # 1. reflink 成功
    mock_oper.reflink.return_value = True
    mock_oper.get_item.return_value = FileItem(
        storage="custom",
        path=target_path.as_posix(),
        name=target_path.name,
        type="file",
    )
    new_item, err = TransHandler._TransHandler__transfer_command(
        fileitem=fileitem,
        target_storage="custom",
        source_oper=mock_oper,
        target_oper=mock_oper,
        target_file=target_path,
        transfer_type="reflink",
    )
    assert err == ""
    assert new_item is not None
    assert new_item.path == target_path.as_posix()
    assert new_item.storage == "custom"
    mock_oper.reflink.assert_called_once_with(fileitem, target_path)

    # 2. reflink 失败
    mock_oper.reflink.reset_mock()
    mock_oper.reflink.return_value = False
    new_item_fail, err_fail = TransHandler._TransHandler__transfer_command(
        fileitem=fileitem,
        target_storage="custom",
        source_oper=mock_oper,
        target_oper=mock_oper,
        target_file=target_path,
        transfer_type="reflink",
    )
    assert new_item_fail is None
    assert "创建引用链接失败" in err_fail

    # 3. 存储未声明支持 reflink 整理方式
    mock_oper.is_support_transtype.return_value = False
    new_item_unsupported, err_unsupported = TransHandler._TransHandler__transfer_command(
        fileitem=fileitem,
        target_storage="custom",
        source_oper=mock_oper,
        target_oper=mock_oper,
        target_file=target_path,
        transfer_type="reflink",
    )
    assert new_item_unsupported is None
    assert "不支持 reflink 整理方式" in err_unsupported
