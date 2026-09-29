"""CUE 专辑识别、整轨旁挂归档和真实复制/硬链接的引用完整性。"""

import asyncio
import hashlib
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from mutagen.flac import FLAC

from app.application.audio import AudioMetadataHelper
from app.application.transfer.workflow import TransferPlanningInput, TransferTask
from app.chain.acoustid import AcoustIdChain
from app.chain.media import MediaChain
from app.chain.transfer.facade import TransferChain
from app.chain.transfer.music import append_cue_companions
from app.domain.context import MusicInfo
from app.domain.music import parse_music_cue
from app.modules.filemanager.storages.local import LocalStorage
from app.modules.filemanager.transhandler import TransHandler
from app.schemas.system import TransferDirectoryConf
from app.schemas.types import MediaType
from tests.test_transfer_sync_extra_files import bind_empty_history_repositories, make_fileitem, make_transfer_chain

CUE = '''REM DATE 2003
PERFORMER "测试歌手"
TITLE "专辑示例"
FILE "image.flac" WAVE
  TRACK 01 AUDIO
    TITLE "第一首歌曲"
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    TITLE "第二首歌曲"
    INDEX 00 00:00:70
    INDEX 01 00:01:00
'''


def _image_pair(tmp_path, *, encoding="utf-8", text=CUE, cue_name="album.cue"):
    """创建三秒实际整轨 FLAC 和两个逻辑曲目的 CUE。"""
    source = tmp_path / "source"
    source.mkdir()
    audio = source / "image.flac"
    cue = source / cue_name
    shutil.copyfile(Path(__file__).parent / "fixtures/audio/silence.flac", audio)
    cue.write_bytes(text.encode(encoding))
    return audio, cue


def test_cue_parser_separates_album_and_track_text():
    """全局标题/艺人不被 TRACK 标题覆盖，INDEX 00 不能替代 INDEX 01。"""
    parsed = parse_music_cue(CUE)

    assert parsed.title == "专辑示例"
    assert parsed.artist == "测试歌手"
    assert parsed.year == 2003
    assert [track.title for track in parsed.tracks] == ["第一首歌曲", "第二首歌曲"]
    assert [track.start_frame for track in parsed.tracks] == [0, 75]


@pytest.mark.parametrize("text", [
    CUE.replace("INDEX 01 00:01:00", "INDEX 01 00:00:00"),
    CUE.replace("INDEX 01 00:01:00", "INDEX 01 00:61:00"),
    CUE.replace("INDEX 01 00:01:00", "INDEX 01 00:01:75"),
    CUE.replace("TRACK 02", "TRACK 01"),
    CUE.replace("    INDEX 01 00:01:00\n", ""),
    CUE.replace("TRACK 02 AUDIO", "TRACK 02 MODE1/2352"),
])
def test_invalid_cue_structure_is_rejected(text):
    """无效编号、缺失和逆序索引不能进入专辑识别。"""
    with pytest.raises(ValueError):
        parse_music_cue(text)


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16", "gb18030"])
def test_image_cue_metadata_is_album_and_keeps_source_hashes(tmp_path, encoding):
    """常见编码的整轨按专辑读取，不能沿用文件内错误的第一首 Recording ID。"""
    audio, cue = _image_pair(tmp_path, encoding=encoding)
    tags = FLAC(audio)
    tags["title"] = ["第一首歌曲"]
    tags["musicbrainz_trackid"] = ["11111111-1111-4111-8111-111111111111"]
    tags.save()
    before = [hashlib.sha256(path.read_bytes()).digest() for path in (audio, cue)]

    meta, raw_tags, _ = AudioMetadataHelper.read_evidence(audio)

    assert meta.music_layout == "image_cue"
    assert meta.music_type == "album"
    assert meta.title == meta.album == "专辑示例"
    assert meta.album_artist == "测试歌手"
    assert meta.track_number is None
    assert meta.total_tracks == 2
    assert len(meta.cue_tracks) == 2
    assert meta.media_id is None
    assert raw_tags.media_id is not None
    assert before == [hashlib.sha256(path.read_bytes()).digest() for path in (audio, cue)]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_image_cue_never_runs_recording_fingerprint(tmp_path, monkeypatch, asynchronous):
    """整轨开头的指纹只代表其中一曲，路径识别不能把它当作整个专辑。"""
    audio, _ = _image_pair(tmp_path)
    sync_fingerprint = Mock(side_effect=AssertionError("整轨不应调用首曲指纹"))
    async_fingerprint = AsyncMock(side_effect=AssertionError("整轨不应调用首曲指纹"))
    monkeypatch.setattr(AcoustIdChain, "identify_music_by_fingerprint", sync_fingerprint)
    monkeypatch.setattr(AcoustIdChain, "async_identify_music_by_fingerprint", async_fingerprint)
    chain = MediaChain()

    if asynchronous:
        meta, info = asyncio.run(chain.async_recognize_music_by_path(audio))
    else:
        meta, info = chain.recognize_music_by_path(audio)

    assert meta.music_type == info.music_type == "album"
    assert info.title == "专辑示例"
    sync_fingerprint.assert_not_called()
    async_fingerprint.assert_not_called()


@pytest.mark.parametrize("bad_reference", ["../image.flac", "image.wav", "IMAGE.flac", "C:\\music\\image.flac"])
def test_cue_reference_errors_block_even_without_category_folders(tmp_path, bad_reference):
    """跨目录、错误后缀和大小写等引用不能靠关闭分类目录绕过检查。"""
    audio, _ = _image_pair(tmp_path, text=CUE.replace('"image.flac"', f'"{bad_reference}"'))
    meta = AudioMetadataHelper.read(audio)
    task = TransferTask(fileitem=make_fileitem(str(audio)), meta=meta, mediainfo=MusicInfo.from_meta(meta),
                        mtype=MediaType.MUSIC, library_category_folder=False)

    assert meta.music_layout == "cue_invalid"
    assert TransferChain._transfer_validation_error(task) == meta.organization_error
    assert meta.organization_error


def test_single_audio_collects_nonmatching_stem_cue_companion(tmp_path, monkeypatch):
    """FILE 关联有效时，CUE 文件名无需与音频同主干，且应在音频后加入归档。"""
    audio, cue = _image_pair(tmp_path, cue_name="album-index.cue")
    owner = make_transfer_chain()
    storage = SimpleNamespace(get_item=lambda item: item)
    monkeypatch.setattr(owner, "_transfer_storage_chain", lambda: storage)

    items, inherited = append_cue_companions(owner, [(make_fileitem(str(audio)), False)], {}, False, None)

    assert [item.path for item, _ in items] == [str(audio), str(cue)]
    assert inherited[owner._get_file_key(items[-1][0])].music_layout == "image_cue"


@pytest.mark.parametrize("mode", ["copy", "link"])
def test_real_archive_preserves_cue_references_and_source_bytes(tmp_path, monkeypatch, mode):
    """真实规划与存储执行可规范专辑目录，同时保留索引引用和原始做种内容。"""
    audio, cue = _image_pair(tmp_path)
    original = {path: path.read_bytes() for path in (audio, cue)}
    meta = AudioMetadataHelper.read(audio)
    info = MusicInfo.from_meta(meta)
    storage = LocalStorage()
    handler = TransHandler()
    monkeypatch.setattr("app.modules.filemanager.transhandler.eventmanager.send_event", Mock(return_value=None))
    targets = []
    for source in (audio, cue):
        item = storage.get_item(source)
        planning_input = TransferPlanningInput(
            source_fileitem=item.model_dump(mode="json"), target_storage="local",
            target_path=str(tmp_path / "library"), requested_transfer_type=mode, need_rename=True,
        )
        checkpoint = handler.plan_transfer(
            planning_input, meta=meta, mediainfo=info, source_oper=storage,
            target_storage="local", target_path=tmp_path / "library", transfer_type=mode,
            need_scrape=False, need_rename=True, need_notify=False, overwrite_mode="never",
            episodes_info=None, preview=False,
        )
        result = handler.execute_transfer_plan(checkpoint, meta=meta, mediainfo=info, source_oper=storage, target_oper=storage)
        assert result.success, result.message
        target = Path(checkpoint.final_target_path)
        assert target.name == source.name
        assert target.parent == tmp_path / "library" / "测试歌手" / "专辑示例 (2003)"
        assert target.read_bytes() == original[source]
        assert source.read_bytes() == original[source]
        if mode == "link":
            assert source.samefile(target)
        targets.append(target)
    assert (targets[1].parent / parse_music_cue(targets[1].read_text()).tracks[0].file_name).exists()


def test_split_cue_supplies_track_metadata_without_claiming_image(tmp_path):
    """分轨 CUE 可补标签，但其多个文件不是一个整轨文件。"""
    source = tmp_path / "source"
    source.mkdir()
    for name in ("one.flac", "two.flac"):
        shutil.copyfile(Path(__file__).parent / "fixtures/audio/silence.flac", source / name)
    (source / "album.cue").write_text('''PERFORMER "Artist"
TITLE "Album"
FILE "one.flac" WAVE
TRACK 01 AUDIO
TITLE "First"
INDEX 01 00:00:00
FILE "two.flac" WAVE
TRACK 02 AUDIO
TITLE "Second"
INDEX 01 00:00:00
''')

    meta = AudioMetadataHelper.read(source / "two.flac")

    assert meta.music_layout == "tracks_cue"
    assert meta.music_type == "recording"
    assert meta.title == "Second"
    assert meta.album == "Album"
    assert meta.track_number == 2


@pytest.mark.parametrize("text", [
    CUE.replace("    INDEX 01 00:01:00\n", ""),
    CUE.replace("00:01:00", "00:03:00"),
])
def test_broken_index_with_different_stem_is_not_ignored(tmp_path, text):
    """文件名不同但 FILE 明确关联的坏 CUE 也必须可见，不能误识别为首曲。"""
    audio, _ = _image_pair(tmp_path, text=text, cue_name="different-index.cue")

    meta = AudioMetadataHelper.read(audio)

    assert meta.organization_error
    assert meta.music_layout == "cue_invalid"


@pytest.mark.parametrize("mtype", [None, MediaType.MUSIC])
@pytest.mark.parametrize("invalid", [False, True])
def test_transfer_entrypoint_includes_valid_cue_and_rejects_invalid_before_planning(tmp_path, monkeypatch, mtype, invalid):
    """完整整理入口会加入有效旁挂，并在坏索引时先失败，不能只验证孤立解析器。"""
    audio, cue = _image_pair(tmp_path, text=CUE.replace("00:01:00", "00:10:00") if invalid else CUE)
    storage = LocalStorage()
    item = storage.get_item(audio)
    owner = make_transfer_chain()
    bind_empty_history_repositories(owner)
    owner.eventmanager = SimpleNamespace(send_event=lambda *_args, **_kwargs: None)
    monkeypatch.setattr(owner, "_TransferChain__get_trans_fileitems", lambda *_args, **_kwargs: [(item, False)])
    monkeypatch.setattr(owner, "_TransferChain__put_to_jobview", lambda _task: True)
    monkeypatch.setattr(owner, "_register_scrape_batch_task", lambda _task: None)
    monkeypatch.setattr(owner, "_close_scrape_batch", lambda _batch: None)
    monkeypatch.setattr(owner, "_transfer_storage_chain", lambda: SimpleNamespace(get_item=lambda source: storage.get_item(Path(source.path))))
    monkeypatch.setattr("app.chain.transfer.request._TransferCandidatePlanner._get_single_file_sibling_items", lambda *_args: ([item], []))
    monkeypatch.setattr("app.chain.transfer.workflow.get_configured_system_config", lambda: SimpleNamespace(get=lambda _key: None))
    monkeypatch.setattr("app.modules.filemanager.transhandler.eventmanager.send_event", Mock(return_value=None))
    plans = []

    def preview(task, **_kwargs):
        """使用真实规划与预览执行，不触发实际文件写入。"""
        plans.append(task)
        handler = TransHandler()
        checkpoint = handler.plan_transfer(
            task.planning_input, meta=task.meta, mediainfo=task.mediainfo, source_oper=storage,
            target_storage="local", target_path=tmp_path / "library", transfer_type="copy",
            need_scrape=False, need_rename=True, need_notify=False, overwrite_mode="never",
            episodes_info=None, preview=True,
        )
        return handler.execute_transfer_plan(checkpoint, meta=task.meta, mediainfo=task.mediainfo,
                                             source_oper=storage, target_oper=storage)

    monkeypatch.setattr(owner, "_plan_checkpoint_and_execute", preview)
    state, result = owner.do_transfer(
        fileitem=item, mtype=mtype, force=True, preview=True,
        target_directory=TransferDirectoryConf(library_path=str(tmp_path / "library"), library_storage="local", renaming=True),
    )

    if invalid:
        assert state is False
        assert plans == []
        assert "CUE" in str(result)
    else:
        assert state is True
        assert [task.fileitem.path for task in plans] == [str(audio), str(cue)]
        assert all(task.mediainfo.music_type == "album" for task in plans)
    assert not (tmp_path / "library").exists()
