from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.application.transfer.workflow import TransferPlanningInput
from app.domain.context import MediaInfo
from app.domain.meta.metavideo import MetaVideo
from app.modules.filemanager.transhandler import TransHandler
from app.schemas.file import FileItem
from app.schemas.transfer import TransferInfo
from app.schemas.types import MediaType


def _transfer_without_episode(file_name: str) -> tuple[TransferInfo, MagicMock, MagicMock]:
    """整理一个未识别出集数的剧集视频文件"""
    fileitem = FileItem(
        storage="local",
        path=f"/downloads/{file_name}",
        type="file",
        name=file_name,
        basename=file_name.removesuffix(".mkv"),
        extension="mkv",
        size=1024,
    )
    meta = MetaVideo(file_name)
    meta.begin_episode = None
    mediainfo = MediaInfo()
    mediainfo.type = MediaType.TV
    source_oper = MagicMock()
    target_oper = MagicMock()

    handler = TransHandler()
    planning_input = TransferPlanningInput(
        source_fileitem=fileitem.model_dump(mode="json"),
        meta=meta.to_dict(),
        mediainfo=mediainfo.to_dict(),
        target_storage="local",
        target_path="/library",
        requested_transfer_type="copy",
        media_type=MediaType.TV.value,
        need_scrape=False,
        need_rename=True,
        need_notify=True,
        preview=False,
    )
    try:
        checkpoint = handler.plan_transfer(
            planning_input,
            meta=meta,
            mediainfo=mediainfo,
            source_oper=source_oper,
            target_storage="local",
            target_path=Path("/library"),
            transfer_type="copy",
            need_scrape=False,
            need_rename=True,
            need_notify=True,
            overwrite_mode=None,
            episodes_info=None,
            preview=False,
        )
    except ValueError as error:
        result = TransferInfo(
            success=False,
            message=str(error),
            fileitem=fileitem,
            fail_list=[fileitem.path],
            transfer_type="copy",
            need_notify=True,
        )
    else:
        result = handler.execute_transfer_plan(
            checkpoint,
            meta=meta,
            mediainfo=mediainfo,
            source_oper=source_oper,
            target_oper=target_oper,
        )
    return result, source_oper, target_oper


@pytest.mark.parametrize(
    "file_name",
    [
        "Test.Show.NCOP.mkv",
        "Test.Show.Menu.mkv",
        "Test.Show.Featurette.mkv",
    ],
)
def test_tv_special_extra_without_episode_skips_transfer(file_name: str) -> None:
    """未识别出集数的已知特典文件应静默跳过并返回成功"""
    result, source_oper, target_oper = _transfer_without_episode(file_name)

    assert result.success is True
    assert result.need_notify is False
    assert result.fileitem.path == f"/downloads/{file_name}"
    assert source_oper.mock_calls == []
    assert target_oper.mock_calls == []


def test_regular_tv_file_without_episode_remains_failure() -> None:
    """普通剧集视频未识别出集数时仍应返回失败并保留通知"""
    result, source_oper, target_oper = _transfer_without_episode("Test.Show.Bonus.mkv")

    assert result.success is False
    assert result.message == "未识别到文件集数"
    assert result.need_notify is True
    assert result.fail_list == ["/downloads/Test.Show.Bonus.mkv"]
    assert source_oper.mock_calls == []
    assert target_oper.mock_calls == []
