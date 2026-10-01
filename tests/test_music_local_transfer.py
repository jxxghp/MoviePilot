"""真实标签经过整理批次、用户分类和目标命名后无需在线识别。"""

import hashlib
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from mutagen.flac import FLAC

from app.application.classification.execution import ClassificationExecutionService
from app.application.messaging.message import TemplateHelper
from app.chain.media import MediaChain
from app.domain.meta.metamusic import MetaMusic
from app.domain.music import music_tags_are_usable
from app.modules.filemanager.transhandler import TransHandler
from app.runtime.config import ConfigModel
from app.schemas.category import CategoryConfig, ClassificationPolicy
from app.schemas.file import FileItem
from app.schemas.system import TransferDirectoryConf
from app.schemas.transfer import ManualTransferResultData, TransferInfo
from app.schemas.types import MediaType
from tests.test_transfer_sync_extra_files import bind_empty_history_repositories, make_fileitem, make_transfer_chain


def _tagged_files(directory: Path) -> list[Path]:
    """生成名字无意义且父目录年份错误的实际双碟 FLAC，身份由标签提供。"""
    directory.mkdir()
    files = []
    for disc, title in ((1, "晴天"), (2, "以父之名")):
        path = directory / f"file-{disc}.flac"
        shutil.copyfile(Path(__file__).parent / "fixtures/audio/silence.flac", path)
        audio = FLAC(path)
        audio.update({
            "title": [title], "artist": ["周杰伦"], "album": ["叶惠美"],
            "albumartist": ["周杰伦"], "date": ["2003"],
            "tracknumber": ["1"], "tracktotal": ["1"],
            "discnumber": [str(disc)], "disctotal": ["2"],
        })
        audio.save()
        files.append(path)
    return files


def _classification_service():
    """提供自定义未分类去向和禁止调用的外部补充端口。"""
    policy = ClassificationPolicy.model_validate({
        "schema_version": 2, "revision": 1, "enrichment_mode": "enrich_missing",
        "categories": [{"id": "music.local", "media_type": "音乐", "name": "收藏", "path": ["收藏"]}],
        "rules": [], "fallbacks": {"音乐": "music.local"},
    })
    runtime = SimpleNamespace(active_policy=lambda: policy, legacy_config=lambda: CategoryConfig())
    enrichment = Mock()
    enrichment.async_enrich = AsyncMock()
    return ClassificationExecutionService(runtime, enrichment=enrichment), enrichment


@pytest.mark.parametrize("mtype", [None, MediaType.MUSIC])
def test_complete_tags_preview_offline_and_preserve_custom_category(tmp_path, monkeypatch, mtype):
    """自动与显式音乐批次均以真实标签完成目标命名，双碟不碰撞且源内容不变。"""
    source = tmp_path / "2022-wrong-name"
    paths = _tagged_files(source)
    hashes = [hashlib.sha256(path.read_bytes()).digest() for path in paths]
    items = [make_fileitem(str(path)) for path in paths]
    chain = make_transfer_chain()
    bind_empty_history_repositories(chain)
    chain.eventmanager = SimpleNamespace(send_event=lambda *_args, **_kwargs: None)
    service, enrichment = _classification_service()
    chain.classification_service = service
    monkeypatch.setattr(chain, "_TransferChain__get_trans_fileitems", lambda *_args, **_kwargs: [(item, False) for item in items])
    monkeypatch.setattr(chain, "_TransferChain__put_to_jobview", lambda _task: True)
    monkeypatch.setattr(chain, "_register_scrape_batch_task", lambda _task: None)
    monkeypatch.setattr(chain, "_close_scrape_batch", lambda _batch: None)
    monkeypatch.setattr("app.chain.transfer.workflow.StorageChain.get_item", lambda _self, item: item)
    monkeypatch.setattr("app.chain.transfer.workflow.get_configured_system_config", lambda: SimpleNamespace(get=lambda _key: None))
    monkeypatch.setattr("app.modules.filemanager.transhandler.eventmanager.send_event", Mock(return_value=None))
    online = []
    for name in ("recognize_music_album_directory", "recognize_music_by_path", "recognize_by_meta", "recognize_media"):
        operation = Mock(side_effect=AssertionError("完整标签整理不应请求在线识别"))
        monkeypatch.setattr(MediaChain, name, operation)
        online.append(operation)
    destinations = []

    def preview_plan(task, **_kwargs):
        """执行真实模板渲染而不写库或复制文件，检查批次传入的最终音乐上下文。"""
        assert task.mediainfo.library_category == "收藏"
        assert task.mediainfo.media_id is None
        context = TemplateHelper().builder.build(
            meta=task.meta, mediainfo=task.mediainfo, file_extension=".flac", include_raw_objects=False,
        )
        target = TransHandler.get_rename_path(
            ConfigModel().MUSIC_RENAME_FORMAT, context, path=tmp_path / "library" / "收藏",
        )
        destinations.append(target.relative_to(tmp_path / "library").as_posix())
        return TransferInfo(success=True, fileitem=task.fileitem, target_item=make_fileitem(str(target)))

    monkeypatch.setattr(chain, "_plan_checkpoint_and_execute", preview_plan)
    state, preview = chain.do_transfer(
        fileitem=FileItem(storage="local", path=str(source) + "/", type="dir", name=source.name),
        target_directory=TransferDirectoryConf(
            library_path=str(tmp_path / "library"), library_storage="local", library_category_folder=True,
        ),
        mtype=mtype, force=True, preview=True,
    )

    assert state is True
    assert preview["summary"] == {"total": 2, "success": 2, "failed": 0}
    projected = ManualTransferResultData.model_validate(preview)
    assert {item.music.status for item in projected.items} == {"local_tags"}
    assert {item.music.read_status for item in projected.items} == {"tags"}
    assert len({item.music.group_id for item in projected.items}) == 1
    assert {item.music.group_size for item in projected.items} == {2}
    assert all(not item.music.online_confirmed and item.source_storage == "local" for item in projected.items)
    assert [item.music.disc_number for item in projected.items] == [1, 2]
    assert [item.source_item.path for item in projected.items] == [str(path) for path in paths]
    assert destinations == [
        "收藏/周杰伦/叶惠美 (2003)/Disc 1/01 - 晴天.flac",
        "收藏/周杰伦/叶惠美 (2003)/Disc 2/01 - 以父之名.flac",
    ]
    assert [hashlib.sha256(path.read_bytes()).digest() for path in paths] == hashes
    for operation in online:
        operation.assert_not_called()
    enrichment.enrich.assert_not_called()
    enrichment.async_enrich.assert_not_called()


@pytest.mark.parametrize("field,value", [
    ("title", "Track 01"), ("title", "01"), ("title", "Unknown"), ("title", "坏\ufffd名"),
    ("title", "www.example.test"), ("album", None), ("artists", []), ("album", "Unknown Album"),
])
def test_incomplete_or_placeholder_tags_require_more_evidence(field, value):
    """路径可补字段不能使不完整或占位标签获得直接整理资格。"""
    meta = MetaMusic(title="晴天", artists=["周杰伦"], album="叶惠美")
    setattr(meta, field, value)

    assert music_tags_are_usable(meta) is False


def test_complete_tags_do_not_require_year_or_remote_id():
    """真实名称足以本地整理，数字专辑名与未知年份不构成失败。"""
    meta = MetaMusic(title="Welcome to New York", artists=["Taylor Swift"], album="1989")

    assert music_tags_are_usable(meta) is True
