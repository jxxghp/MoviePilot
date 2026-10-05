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
from app.chain.media.cache import AlbumDirectoryCache
from app.chain.scraping import ScrapingChain
from app.chain.transfer.facade import TransferChain
from app.chain.transfer.music import append_cue_companions
from app.domain.context import MusicInfo
from app.domain.music import parse_music_cue
from app.modules.filemanager.storages.local import LocalStorage
from app.modules.filemanager.transhandler import TransHandler
from app.runtime.config import ConfigModel, settings
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


@pytest.mark.parametrize("bad_reference", ["../image.flac", "C:\\music\\image.flac"])
def test_cue_reference_errors_block_even_without_category_folders(tmp_path, bad_reference):
    """跨目录引用不能靠关闭分类目录绕过检查；同目录引用交给存在性判定。"""
    audio, _ = _image_pair(tmp_path, text=CUE.replace('"image.flac"', f'"{bad_reference}"'))
    meta = AudioMetadataHelper.read(audio)
    task = TransferTask(fileitem=make_fileitem(str(audio)), meta=meta, mediainfo=MusicInfo.from_meta(meta),
                        mtype=MediaType.MUSIC, library_category_folder=False)

    assert meta.music_layout == "cue_invalid"
    assert TransferChain._transfer_validation_error(task) == meta.organization_error
    assert meta.organization_error


@pytest.mark.parametrize("stale_reference", ["image.wav"])
def test_cue_reference_missing_from_directory_falls_back_to_tags(tmp_path, stale_reference):
    """引用文件已不存在的陈旧索引（转制未更新扩展名等）不得阻断整理，回退标签。"""
    audio, _ = _image_pair(tmp_path, text=CUE.replace('"image.flac"', f'"{stale_reference}"'))
    tags = FLAC(audio)
    tags.update(title=["独立单曲"], artist=["测试歌手"], album=["专辑示例"], tracknumber=["2"])
    tags.save()

    meta = AudioMetadataHelper.read(audio)

    assert not meta.organization_error
    assert meta.music_layout != "cue_invalid"
    assert meta.title == "独立单曲"
    assert meta.track_number == 2


def test_stale_wav_cue_does_not_block_flac_split_album(tmp_path):
    """WAV 时代留档 CUE 全部引用不存在的 .wav 时，FLAC 分轨按自带标签正常整理。"""
    source = tmp_path / "source"
    source.mkdir()
    for name in ("01 - 以父之名.flac", "02 - 懦夫.flac"):
        shutil.copyfile(Path(__file__).parent / "fixtures/audio/silence.flac", source / name)
    (source / "album.cue").write_text('''PERFORMER "周杰倫"
TITLE "葉惠美"
FILE "01 - 以父之名.wav" WAVE
  TRACK 01 AUDIO
    TITLE "以父之名"
    INDEX 01 00:00:00
FILE "02 - 懦夫.wav" WAVE
  TRACK 02 AUDIO
    TITLE "懦夫"
    INDEX 01 00:00:00
''')

    meta = AudioMetadataHelper.read(source / "01 - 以父之名.flac")

    assert not meta.organization_error
    assert meta.music_layout != "cue_invalid"
    assert not meta.cue_tracks


def test_broken_index_without_existing_references_is_ignored(tmp_path):
    """结构损坏且引用文件全部不存在的 CUE 同样按陈旧索引忽略，不再阻断。"""
    audio, _ = _image_pair(tmp_path, text=CUE.replace('"image.flac"', '"image.wav"')
                           .replace("    INDEX 01 00:01:00\n", ""))

    meta = AudioMetadataHelper.read(audio)

    assert not meta.organization_error
    assert meta.music_layout != "cue_invalid"


def test_single_audio_collects_nonmatching_stem_cue_companion(tmp_path, monkeypatch):
    """FILE 关联有效时，CUE 文件名无需与音频同主干，且应在音频后加入归档。"""
    audio, cue = _image_pair(tmp_path, cue_name="album-index.cue")
    owner = make_transfer_chain()
    storage = SimpleNamespace(get_item=lambda item: item)
    monkeypatch.setattr(owner, "_transfer_storage_chain", lambda: storage)

    items, inherited = append_cue_companions(owner, [(make_fileitem(str(audio)), False)], {}, False, None)

    assert [item.path for item, _ in items] == [str(audio), str(cue)]
    assert inherited[owner._get_file_key(items[-1][0])].music_layout == "image_cue"


@pytest.mark.parametrize("mode", ["copy", "link", "softlink"])
@pytest.mark.parametrize("scrape", [False, True])
def test_real_archive_preserves_cue_references_and_source_bytes(tmp_path, monkeypatch, mode, scrape):
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
            need_scrape=scrape, need_rename=True, need_notify=False, overwrite_mode="never",
            episodes_info=None, preview=False,
        )
        result = handler.execute_transfer_plan(checkpoint, meta=meta, mediainfo=info, source_oper=storage, target_oper=storage)
        assert result.success, result.message
        target = Path(checkpoint.final_target_path)
        assert target.name == source.name
        assert target.parent == tmp_path / "library" / "测试歌手" / "专辑示例 (2003)"
        assert target.read_bytes() == original[source]
        assert source.read_bytes() == original[source]
        if mode != "copy":
            assert source.samefile(target)
        targets.append(target)
    if scrape:
        chain = object.__new__(ScrapingChain)
        chain.storagechain = SimpleNamespace(download_file=storage.download)
        chain.scraping_policies = SimpleNamespace(
            option=lambda _target, kind: SimpleNamespace(is_skip=kind != "nfo", is_overwrite=True))
        success, message = chain.scrape_music_metadata(storage.get_item(targets[0]), mediainfo=info)
        assert success, message
        assert not audio.samefile(targets[0])
        assert AudioMetadataHelper.read_tags(targets[0]).album == "专辑示例"
        assert AudioMetadataHelper.read_tags(targets[0]).media_id is None
        assert all(source.read_bytes() == content for source, content in original.items())
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


@pytest.mark.parametrize("invalid", [False, True])
def test_disabled_cue_preserves_track_tags_across_readers(tmp_path, monkeypatch, invalid):
    """显式关闭后所有读取入口都忽略 CUE，坏索引不阻断分轨标签且原文件不变。"""
    audio, cue = _image_pair(tmp_path, text=CUE.replace("00:01:00", "00:00:00") if invalid else CUE)
    tags = FLAC(audio)
    tags.update(title=["独立单曲"], artist=["测试歌手"], album=["专辑示例"], tracknumber=["2"])
    tags.save()
    original = [path.read_bytes() for path in (audio, cue)]
    assert ConfigModel.model_fields["MUSIC_CUE_ENABLE"].default is True
    monkeypatch.setattr(settings, "MUSIC_CUE_ENABLE", False)

    evidence, raw_tags, _ = AudioMetadataHelper.read_evidence(audio)
    results = [evidence, AudioMetadataHelper.read(audio),
               AudioMetadataHelper.with_cue_context(audio, raw_tags)]

    for meta in results:
        assert meta.title == "独立单曲"
        assert meta.track_number == 2
        assert meta.music_type == "recording"
        assert meta.field_sources["title"] == "tag"
        assert meta.music_layout is None
        assert not meta.organization_error
        assert not meta.cue_tracks
    assert original == [path.read_bytes() for path in (audio, cue)]

    monkeypatch.setattr(settings, "MUSIC_CUE_ENABLE", True)
    assert AudioMetadataHelper.read(audio).music_layout == ("cue_invalid" if invalid else "image_cue")


@pytest.mark.parametrize("asynchronous", [False, True])
def test_album_cache_separates_cue_recognition_modes(tmp_path, monkeypatch, asynchronous):
    """同目录切换 CUE 开关后同步、异步识别均重新计算，原模式仍可复用自己的缓存。"""
    audio, _ = _image_pair(tmp_path)
    chain = MediaChain()
    monkeypatch.setattr(chain, "_album_dir_cache", AlbumDirectoryCache(capacity=4))
    match = AsyncMock(return_value={}) if asynchronous else Mock(return_value={})
    method = "_async_match_music_album_directory" if asynchronous else "_match_music_album_directory"
    monkeypatch.setattr(chain, method, match)

    for enabled in (True, True, False, False, True):
        monkeypatch.setattr(settings, "MUSIC_CUE_ENABLE", enabled)
        if asynchronous:
            asyncio.run(chain.async_recognize_music_album_directory(audio.parent))
        else:
            chain.recognize_music_album_directory(audio.parent)

    assert match.call_count == 2


@pytest.mark.parametrize("mode", ["copy", "link", "softlink"])
def test_disabled_cue_organizes_tagged_audio_without_copying_invalid_index(tmp_path, monkeypatch, mode):
    """复制及链接实际落盘按分轨标签命名，错误 CUE 与做种源内容保持原样。"""
    audio, cue = _image_pair(tmp_path, text=CUE.replace("00:01:00", "00:00:00"))
    tags = FLAC(audio)
    tags.update(title=["独立单曲"], artist=["测试歌手"], album=["专辑示例"], tracknumber=["2"])
    tags.save()
    original = [path.read_bytes() for path in (audio, cue)]
    monkeypatch.setattr(settings, "MUSIC_CUE_ENABLE", False)
    monkeypatch.setattr("app.modules.filemanager.transhandler.eventmanager.send_event", Mock(return_value=None))
    meta = AudioMetadataHelper.read(audio)
    info = MusicInfo.from_meta(meta)
    storage, handler = LocalStorage(), TransHandler()
    planning_input = TransferPlanningInput(
        source_fileitem=storage.get_item(audio).model_dump(mode="json"), target_storage="local",
        target_path=str(tmp_path / "library"), requested_transfer_type=mode, need_rename=True,
    )
    checkpoint = handler.plan_transfer(
        planning_input, meta=meta, mediainfo=info, source_oper=storage,
        target_storage="local", target_path=tmp_path / "library", transfer_type=mode,
        need_scrape=False, need_rename=True, need_notify=False, overwrite_mode="never",
        episodes_info=None, preview=False,
    )

    result = handler.execute_transfer_plan(checkpoint, meta=meta, mediainfo=info,
                                           source_oper=storage, target_oper=storage)

    assert result.success, result.message
    target = Path(checkpoint.final_target_path)
    assert target == tmp_path / "library" / "测试歌手" / "专辑示例" / "02 - 独立单曲.flac"
    assert target.read_bytes() == original[0]
    assert not (target.parent / cue.name).exists()
    assert original == [path.read_bytes() for path in (audio, cue)]


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
@pytest.mark.parametrize("cue_enabled", [False, True])
def test_transfer_entrypoint_respects_cue_setting(tmp_path, monkeypatch, mtype, invalid, cue_enabled):
    """完整入口默认保护整轨及索引，关闭 CUE 后改用分轨标签且不归档索引。"""
    audio, cue = _image_pair(tmp_path, text=CUE.replace("00:01:00", "00:10:00") if invalid else CUE)
    tags = FLAC(audio)
    tags.update(title=["独立单曲"], artist=["测试歌手"], album=["专辑示例"], tracknumber=["2"])
    tags.save()
    monkeypatch.setattr(settings, "MUSIC_CUE_ENABLE", cue_enabled)
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

    if invalid and cue_enabled:
        assert state is False
        assert plans == []
        assert "CUE" in str(result)
    elif cue_enabled:
        assert state is True
        assert [task.fileitem.path for task in plans] == [str(audio), str(cue)]
        assert all(task.mediainfo.music_type == "album" for task in plans)
    else:
        assert state is True
        assert [task.fileitem.path for task in plans] == [str(audio)]
        assert plans[0].mediainfo.music_type == "recording"
        assert plans[0].meta.title == "独立单曲"
        assert not plans[0].meta.organization_error
    assert not (tmp_path / "library").exists()
