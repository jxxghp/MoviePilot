"""
整理目录未匹配时的自定义路径告警与自动匹配拒绝边界。
"""

from pathlib import Path
from typing import Optional
from unittest.mock import Mock

import pytest

from app.application.transfer.execution import TransferPlanningRejectedError
from app.domain.context import MediaInfo
from app.domain.meta.metabase import MetaBase
from app.domain.metainfo import MetaInfo
from app.modules.filemanager.module import FileManagerModule
from app.schemas.category import ClassificationResult, ClassificationSelection
from app.schemas.file import FileItem
from app.schemas.system import TransferDirectoryConf
from app.schemas.types import MediaType

# 源文件、识别信息和可观察边界共同组成一次隔离规划上下文。
TransferContext = tuple[FileItem, MetaBase, MediaInfo, Mock, Mock]


@pytest.fixture
def transfer_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TransferContext:
    """
    提供隔离源文件与分类快照，记录告警并禁止规划产生存储写入。
    """
    source = tmp_path / "downloads" / "Movie.2021.mkv"
    source.parent.mkdir()
    source.write_bytes(b"video")
    fileitem = FileItem(
        storage="local", path=source.as_posix(), type="file",
        name=source.name, basename=source.stem, extension="mkv", size=5,
    )
    media = MediaInfo(
        type=MediaType.MOVIE, title="乌海", year="2021",
        classification=ClassificationResult(effective=ClassificationSelection(
            category_path=["华语电影"], source="automatic",
        )),
    )
    warning = Mock()
    monkeypatch.setattr("app.modules.filemanager.module.logger.warning", warning)
    return fileitem, MetaInfo(source.name), media, Mock(), warning


@pytest.mark.parametrize("target_storage", [None, "smb"])
@pytest.mark.parametrize("preview", [False, True])
@pytest.mark.parametrize(
    ("type_folder", "category_folder", "relative_path"),
    [(True, True, "电影/华语电影"), (False, True, "华语电影"), (False, False, "")],
)
def test_unmatched_explicit_target_warns_and_preserves_requested_path(
    transfer_context: TransferContext, tmp_path: Path, target_storage: Optional[str],
    preview: bool, type_folder: bool, category_folder: bool, relative_path: str,
) -> None:
    """
    未匹配配置时保留自定义路径合同，并在预览和实际规划中明确告警。
    """
    fileitem, meta, media, source_oper, warning = transfer_context
    explicit_root = tmp_path / "media"
    expected_root = explicit_root / relative_path
    module = FileManagerModule()

    checkpoint = module.plan_transfer(
        fileitem=fileitem, meta=meta, mediainfo=media,
        target_storage=target_storage, target_path=explicit_root,
        transfer_type="copy", library_type_folder=type_folder,
        library_category_folder=category_folder, source_oper=source_oper,
        preview=preview,
    )

    assert checkpoint.root_target_path == expected_root.as_posix()
    assert Path(checkpoint.final_target_path).is_relative_to(expected_root)
    assert checkpoint.target_storage == (target_storage or "local")
    warning.assert_called_once()
    message = warning.call_args.args[0]
    assert "未匹配到媒体库目录配置" in message
    assert f"【{target_storage or 'local'}】{explicit_root}" in message
    assert f"计算后的目标路径：{expected_root}" in message
    assert f"源路径：【local】{fileitem.path}" in message
    assert "自动匹配" in message
    assert source_oper.mock_calls == []
    assert not explicit_root.exists()


def test_matched_directory_keeps_library_path_without_custom_target_warning(
    transfer_context: TransferContext, tmp_path: Path,
) -> None:
    """
    命中目录配置时应保留 links 层级，并且不能误报自定义路径告警。
    """
    fileitem, meta, media, source_oper, warning = transfer_context
    library_root = tmp_path / "media" / "links" / "电影"
    directory = TransferDirectoryConf(
        name="电影", library_path=library_root.as_posix(),
        library_storage="local", transfer_type="copy", media_type="电影",
        library_type_folder=False, library_category_folder=True,
    )

    checkpoint = FileManagerModule().plan_transfer(
        fileitem=fileitem, meta=meta, mediainfo=media,
        target_directory=directory, target_path=tmp_path / "media",
        source_oper=source_oper,
    )

    assert checkpoint.root_target_path == (library_root / "华语电影").as_posix()
    warning.assert_not_called()
    assert source_oper.mock_calls == []
    assert not library_root.exists()


def test_unmatched_automatic_target_rejects_without_storage_root_fallback(
    transfer_context: TransferContext,
) -> None:
    """
    没有显式目标路径时必须业务拒绝，不能自行生成存储根下的整理计划。
    """
    fileitem, meta, media, source_oper, warning = transfer_context

    with pytest.raises(TransferPlanningRejectedError, match="未找到有效的媒体库目录"):
        FileManagerModule().plan_transfer(
            fileitem=fileitem, meta=meta, mediainfo=media,
            transfer_type="copy", library_type_folder=True,
            library_category_folder=True, source_oper=source_oper,
        )

    warning.assert_not_called()
    assert source_oper.mock_calls == []
