"""音乐整理的存储证据隔离、特殊包提示及显式影视上下文保护。"""

import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from mutagen.flac import FLAC

from app.application.audio import AudioMetadataHelper
from app.chain.media import MediaChain
from app.chain.transfer.facade import TransferChain
from app.domain.context import MediaInfo, MusicInfo
from app.domain.meta.metamusic import MetaMusic
from app.schemas.transfer import TransferInfo
from app.schemas.types import MediaSource, MediaType
from tests.test_music_resource_context import _history
from tests.test_transfer_sync_extra_files import bind_empty_history_repositories, make_fileitem, make_transfer_chain


def _tagged_file(tmp_path):
    """用真实 FLAC 构造与远端路径重名的本机干扰文件。"""
    path = tmp_path / "01 - Remote Song.flac"
    shutil.copyfile(Path(__file__).parent / "fixtures/audio/silence.flac", path)
    tags = FLAC(path)
    tags.update(title=["Local Song"], artist=["Local Artist"], album=["Local Album"])
    tags.save()
    return path


def _setup_transfer(monkeypatch, items):
    """隔离历史和存储枚举，保留实际候选规划及识别路径。"""
    owner = make_transfer_chain()
    bind_empty_history_repositories(owner)
    owner.eventmanager = SimpleNamespace(send_event=lambda *_args, **_kwargs: None)
    monkeypatch.setattr(owner, "_TransferChain__get_trans_fileitems", lambda *_args, **_kwargs: [(item, False) for item in items])
    monkeypatch.setattr(owner, "_TransferChain__put_to_jobview", lambda _task: True)
    monkeypatch.setattr(owner, "_register_scrape_batch_task", lambda _task: None)
    monkeypatch.setattr(owner, "_close_scrape_batch", lambda _batch: None)
    monkeypatch.setattr("app.chain.transfer.workflow.get_configured_system_config", lambda: SimpleNamespace(get=lambda _key: None))
    monkeypatch.setattr("app.chain.transfer.request._TransferCandidatePlanner._get_single_file_sibling_items", lambda *_args: (items, []))
    monkeypatch.setattr(MediaChain, "supplement_tmdb_info", lambda _self, media, _meta: media)
    return owner


@pytest.mark.parametrize("storage", ["u115", "smb", None])
def test_remote_path_never_reads_local_tags_or_cue(tmp_path, monkeypatch, storage):
    """远端路径恰好在本机存在也只能读取名称，标签与流信息不能串用。"""
    path = _tagged_file(tmp_path)
    read = Mock(side_effect=AssertionError("远端不能读取本机文件"))
    monkeypatch.setattr(AudioMetadataHelper, "read", read)

    meta = MediaChain.read_path_meta(path, storage=storage)

    assert meta.title == "Remote Song"
    assert meta.album != "Local Album"
    assert meta.duration is None
    assert "tag" not in meta.field_sources.values()
    read.assert_not_called()


@pytest.mark.parametrize("snapshot,discard", [(False, False), (True, False), (True, True)])
def test_remote_download_context_uses_torrent_and_never_local_content(tmp_path, snapshot, discard):
    """无快照、保留身份和丢弃身份三条路径均尊重存储来源。"""
    path = _tagged_file(tmp_path)
    info = MusicInfo(media_source="musicbrainz", media_id="saved", title="Saved Song", album="Saved Album")
    history = _history(tmp_path, note={"music": {"version": 1, "media": info.to_dict()}} if snapshot else None)

    meta, _ = TransferChain._restore_music_download_context(
        history, path, storage="u115", discard_saved_identity=discard,
    )

    assert meta.title == "Remote Song"
    assert meta.album == ("Saved Album" if snapshot and not discard else "Actual Album")
    assert meta.duration is None
    assert "Local Artist" not in meta.artists


@pytest.mark.parametrize("root_storage,item_storage", [("local", "u115"), ("u115", "local")])
def test_batch_uses_each_files_storage_not_root(tmp_path, monkeypatch, root_storage, item_storage):
    """混合存储的选择项按各自来源读取，根目录不能授予本地读取能力。"""
    path = _tagged_file(tmp_path)
    item = make_fileitem(str(path)).model_copy(update={"storage": item_storage})
    root = make_fileitem(str(tmp_path)).model_copy(update={"type": "dir", "storage": root_storage})
    owner = _setup_transfer(monkeypatch, [item])
    captured = []

    def collect(task, **_kwargs):
        """在执行前观察真实工作流构造的元数据。"""
        captured.append(task)
        return True, ""

    monkeypatch.setattr(owner, "_TransferChain__handle_transfer", collect)
    monkeypatch.setattr(MediaChain, "recognize_music_album_directory", Mock(return_value={}))
    state, _ = owner.do_transfer(fileitem=root, mtype=MediaType.MUSIC, force=True, background=False)

    assert state is True
    assert len(captured) == 1
    assert captured[0].meta.title == ("Local Song" if item_storage == "local" else "Remote Song")
    assert captured[0].meta.duration == (3 if item_storage == "local" else None)


@pytest.mark.parametrize("extension,message", [("iso", "音乐镜像"), ("BIN", "音乐镜像"), ("zip", "音乐压缩包"), ("rar", "音乐压缩包"), ("7z.001", "音乐压缩包")])
def test_music_package_has_actionable_failure_before_recognition(tmp_path, monkeypatch, extension, message):
    """音乐整包保留原文件，返回具体处理方式而不是静默跳过或发起歌曲查询。"""
    path = tmp_path / f"Artist - Album.{extension}"
    path.write_bytes(b"not unpacked")
    item = make_fileitem(str(path))
    owner = _setup_transfer(monkeypatch, [item])
    recognize = Mock(side_effect=AssertionError("整包不能进入歌曲识别"))
    monkeypatch.setattr(MediaChain, "recognize_by_meta", recognize)
    plan = Mock(side_effect=AssertionError("整包不能执行文件操作"))
    monkeypatch.setattr(owner, "_plan_checkpoint_and_execute", plan)

    state, result = owner.do_transfer(fileitem=item, mtype=MediaType.MUSIC, force=True, preview=True)

    assert state is False
    assert message in str(result)
    assert "重试" in str(result)
    assert result["summary"] == {"total": 1, "success": 0, "failed": 1}
    assert result["items"][0]["source"] == str(path)
    assert result["items"][0]["music"]["status"] == "unsupported"
    assert path.read_bytes() == b"not unpacked"
    recognize.assert_not_called()
    plan.assert_not_called()


@pytest.mark.parametrize("history_type", [None, MediaType.UNKNOWN.value, MediaType.MUSIC.value])
def test_explicit_video_context_ignores_old_music_snapshot_and_identity(tmp_path, monkeypatch, history_type):
    """影视附加音轨不能被旧音乐备注或身份改变类型，且不读取音频标签。"""
    path = tmp_path / "Show.S01E02.flac"
    item = make_fileitem(str(path))
    owner = _setup_transfer(monkeypatch, [item])
    info = MusicInfo(media_source="musicbrainz", media_id="saved", title="Wrong Song")
    history = _history(tmp_path, type=history_type, media_source="musicbrainz", media_id="saved", media_category="Album",
                       note={"music": {"version": 1, "media": info.to_dict()}})
    monkeypatch.setattr(owner, "_resolve_download_history", lambda **_kwargs: history)
    read = Mock(side_effect=AssertionError("影视音轨不应读取音乐标签"))
    monkeypatch.setattr(AudioMetadataHelper, "read", read)
    video = MediaInfo(title="Show", type=MediaType.TV, media_source=MediaSource.TMDB, media_id="123")
    video.set_library_category("TV")
    recognize = Mock(return_value=video)
    monkeypatch.setattr(MediaChain, "recognize_by_meta", recognize)
    identity = Mock(side_effect=AssertionError("旧音乐身份不能覆盖本次类型"))
    monkeypatch.setattr(MediaChain, "recognize_media", identity)
    plans = []

    def preview(task, **_kwargs):
        """记录实际识别后的影视任务，文件写入不属于这个边界用例。"""
        plans.append(task)
        return TransferInfo(success=True)

    monkeypatch.setattr(owner, "_plan_checkpoint_and_execute", preview)
    state, result = owner.do_transfer(fileitem=item, mtype=MediaType.TV, force=True, preview=True, library_category_folder=False)

    assert state is True, result
    assert len(plans) == 1
    assert not isinstance(plans[0].meta, MetaMusic)
    assert plans[0].meta.begin_episode == 2
    assert plans[0].mediainfo.library_category == "TV"
    assert recognize.call_args.kwargs["mtype"] == MediaType.TV
    identity.assert_not_called()
    read.assert_not_called()


def test_m4a_name_alone_does_not_claim_aac_or_lossless():
    """M4A 是容器，只有实际编码证据才能区分 AAC 和 ALAC。"""
    meta = AudioMetadataHelper.read_filename(Path("/music/Track.m4a"))

    assert meta.audio_format == "M4A"
    assert meta.audio_lossless is None
