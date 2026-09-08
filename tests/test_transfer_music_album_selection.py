from copy import deepcopy
from types import SimpleNamespace

from app.chain.media import MediaChain
from app.chain.transfer.facade import TransferChain
from app.domain.context import MusicAlbumInfo, MusicInfo
from app.domain.meta.metamusic import MetaMusic
from app.schemas.category import ClassificationResult, ClassificationSelection
from app.schemas.file import FileItem
from app.schemas.types import MediaSource, MediaType
from tests.test_transfer_sync_extra_files import (
    bind_empty_history_repositories,
    make_fileitem,
    make_transfer_chain,
)


def _album() -> MusicAlbumInfo:
    """构造已选择的简体中文发行版。"""
    classification = ClassificationResult(
        recommended=ClassificationSelection(
            category_id="music.album",
            category_path=["Album"],
            rule_id="music.album.default",
            source="automatic",
        ),
        effective=ClassificationSelection(
            category_id="music.album",
            category_path=["Album"],
            rule_id="music.album.default",
            source="automatic",
        ),
        policy_revision=2,
        state="complete",
    )
    return MusicAlbumInfo(
        media_source=MediaSource.MusicBrainz,
        media_id="release-group-1",
        title="七里香",
        artists=["周杰伦"],
        album_type="Album",
        release_date="2004-08-03",
        library_category="Album",
        classification=classification,
        tracks=[
            MusicInfo(
                media_source=MediaSource.MusicBrainz,
                media_id="recording-1",
                title="我的地盘",
                artists=["周杰伦"],
                album="七里香",
                album_id="release-group-1",
                album_type="Album",
                track_number=1,
                total_tracks=2,
            ),
            MusicInfo(
                media_source=MediaSource.MusicBrainz,
                media_id="recording-2",
                title="借口",
                artists=["周杰伦"],
                album="七里香",
                album_id="release-group-1",
                album_type="Album",
                track_number=2,
                total_tracks=2,
            ),
        ],
    )


def _directory_item(path) -> FileItem:
    """构造手动整理使用的专辑目录项。"""
    return FileItem(
        storage="local",
        path=path.as_posix() + "/",
        type="dir",
        name=path.name,
    )


def _prepare_chain(monkeypatch, fileitems):
    """隔离文件规划外部依赖，返回可同步观察的整理链。"""
    chain = make_transfer_chain()
    bind_empty_history_repositories(chain)
    monkeypatch.setattr(
        chain,
        "_TransferChain__get_trans_fileitems",
        lambda _fileitem, predicate: [(item, False) for item in fileitems],
    )
    monkeypatch.setattr(chain, "_TransferChain__put_to_jobview", lambda _task: True)
    monkeypatch.setattr(chain, "_register_scrape_batch_task", lambda _task: None)
    monkeypatch.setattr(chain, "_close_scrape_batch", lambda _batch_id: None)
    monkeypatch.setattr(
        "app.chain.transfer.workflow.get_configured_system_config",
        lambda: SimpleNamespace(get=lambda _key: None),
    )
    return chain


def test_selected_album_tracks_override_source_tag_names(tmp_path, monkeypatch):
    """手动选定专辑后应使用所选发行版的逐曲简体名称。"""
    album_dir = tmp_path / "周杰伦 - 七里香 (2004)"
    album_dir.mkdir()
    paths = [album_dir / "01.flac", album_dir / "02.flac"]
    for path in paths:
        path.write_bytes(b"audio")
    fileitems = [make_fileitem(path.as_posix()) for path in paths]
    local_metas = {
        paths[0]: MetaMusic(title="我的地盤", artists=["周杰倫"], track_number=1),
        paths[1]: MetaMusic(title="藉口", artists=["周杰倫"], track_number=2),
    }
    chain = _prepare_chain(monkeypatch, fileitems)
    planned = []
    monkeypatch.setattr(
        "app.chain.media.album.AudioMetadataHelper.read_many",
        lambda requested: [deepcopy(local_metas[path]) for path in requested],
    )
    monkeypatch.setattr(
        MediaChain,
        "read_path_meta",
        staticmethod(lambda path: deepcopy(local_metas[path])),
    )

    def handle_transfer(task, callback=None):
        """记录规划后的曲目与分类上下文。"""
        del callback
        planned.append((task.meta.title, task.mediainfo.title, task.mediainfo.library_category))
        return True, ""

    monkeypatch.setattr(chain, "_TransferChain__handle_transfer", handle_transfer)
    album = _album()

    state, message = TransferChain.do_transfer(
        chain,
        fileitem=_directory_item(album_dir),
        mediainfo=album,
        mtype=MediaType.MUSIC,
        media_source=MediaSource.MusicBrainz,
        media_id=album.media_id,
        background=False,
    )

    assert state is True
    assert message == ""
    assert planned == [
        ("我的地盘", "我的地盘", "Album"),
        ("借口", "借口", "Album"),
    ]


def test_selected_music_fileitems_keep_album_batch_context(tmp_path, monkeypatch):
    """显式多选音轨应在单个批次中应用专辑曲目和分类。"""
    album_dir = tmp_path / "周杰伦 - 七里香 (2004)"
    album_dir.mkdir()
    paths = [album_dir / "01.flac", album_dir / "02.flac"]
    for path in paths:
        path.write_bytes(b"audio")
    fileitems = [make_fileitem(path.as_posix()) for path in paths]
    local_metas = {
        paths[0]: MetaMusic(title="我的地盤", artists=["周杰倫"], track_number=1),
        paths[1]: MetaMusic(title="藉口", artists=["周杰倫"], track_number=2),
    }
    chain = _prepare_chain(monkeypatch, fileitems)
    monkeypatch.setattr(
        "app.chain.transfer.workflow.StorageChain.get_item",
        lambda _self, item: item,
    )
    monkeypatch.setattr(
        "app.chain.media.album.AudioMetadataHelper.read_many",
        lambda requested: [deepcopy(local_metas[path]) for path in requested],
    )
    monkeypatch.setattr(
        MediaChain,
        "read_path_meta",
        staticmethod(lambda path: deepcopy(local_metas[path])),
    )
    planned = []

    def handle_transfer(task, callback=None):
        del callback
        planned.append((task.meta.title, task.mediainfo.library_category))
        return True, ""

    monkeypatch.setattr(chain, "_TransferChain__handle_transfer", handle_transfer)

    state, message = TransferChain._execute_transfer(
        chain,
        fileitem=fileitems[0],
        selected_fileitems=fileitems,
        mediainfo=_album(),
        mtype=MediaType.MUSIC,
        media_source=MediaSource.MusicBrainz,
        media_id="release-group-1",
        background=False,
    )

    assert state is True
    assert message == ""
    assert planned == [("我的地盘", "Album"), ("借口", "Album")]


def test_automatic_music_fileitems_receive_album_identity_and_category(tmp_path, monkeypatch):
    """自动多选音轨应用目录级专辑识别结果覆盖本地繁体标签。"""
    album_dir = tmp_path / "周杰伦 - 七里香 (2004)"
    album_dir.mkdir()
    paths = [album_dir / "01.flac", album_dir / "02.flac"]
    for path in paths:
        path.write_bytes(b"audio")
    fileitems = [make_fileitem(path.as_posix()) for path in paths]
    local_metas = {
        paths[0]: MetaMusic(title="我的地盤", artists=["周杰倫"], track_number=1),
        paths[1]: MetaMusic(title="藉口", artists=["周杰倫"], track_number=2),
    }
    album = _album()
    matched = {}
    for path, track in zip(paths, album.tracks):
        matched_track = deepcopy(track)
        matched_track.set_library_category(album.library_category)
        matched_track.classification = deepcopy(album.classification)
        matched[str(path.resolve())] = matched_track
    chain = _prepare_chain(monkeypatch, fileitems)
    monkeypatch.setattr(
        "app.chain.transfer.workflow.StorageChain.get_item",
        lambda _self, item: item,
    )
    monkeypatch.setattr(
        MediaChain,
        "read_path_meta",
        staticmethod(lambda path: deepcopy(local_metas[path])),
    )
    monkeypatch.setattr(
        MediaChain,
        "recognize_music_album_directory",
        lambda _self, _path, **_kwargs: matched,
    )
    planned = []

    def handle_transfer(task, callback=None):
        del callback
        planned.append((task.meta.title, task.mediainfo.album, task.mediainfo.library_category))
        return True, ""

    monkeypatch.setattr(chain, "_TransferChain__handle_transfer", handle_transfer)

    state, message = TransferChain._execute_transfer(
        chain,
        fileitem=fileitems[0],
        selected_fileitems=fileitems,
        mtype=MediaType.MUSIC,
        background=False,
    )

    assert state is True
    assert message == ""
    assert planned == [
        ("我的地盘", "七里香", "Album"),
        ("借口", "七里香", "Album"),
    ]


def test_manual_album_identity_forwards_full_selected_album(monkeypatch):
    """手动指定专辑 ID 时不应在进入整理链前丢失曲目表。"""
    chain = make_transfer_chain()
    album = _album()
    captured = {}
    monkeypatch.setattr(
        "app.chain.transfer.history.MediaChain",
        lambda: SimpleNamespace(get_music_album=lambda **_kwargs: album),
    )

    def do_transfer(**kwargs):
        """记录手动入口传入的完整专辑上下文。"""
        captured.update(kwargs)
        return True, ""

    monkeypatch.setattr(chain, "do_transfer", do_transfer)

    state, message = TransferChain.manual_transfer(
        chain,
        fileitem=make_fileitem("/music/七里香/01.flac"),
        media_source=MediaSource.MusicBrainz,
        media_id="release-group-1",
        mtype=MediaType.MUSIC,
        music_type="album",
        preview=True,
    )

    assert state is True
    assert message == ""
    assert captured["mediainfo"] is album
    assert captured["mediainfo"].title == "七里香"
    assert [track.title for track in captured["mediainfo"].tracks] == [
        "我的地盘",
        "借口",
    ]


def test_selected_album_rejects_duplicate_local_editions(tmp_path, monkeypatch):
    """同一手动批次包含两套同曲序文件时应阻止目标覆盖。"""
    album_dir = tmp_path / "周杰伦 - 七里香 (2004)"
    extra_dir = album_dir / "附加原版"
    extra_dir.mkdir(parents=True)
    paths = [
        album_dir / "01.flac",
        album_dir / "02.flac",
        extra_dir / "01.flac",
        extra_dir / "02.flac",
    ]
    for path in paths:
        path.write_bytes(b"audio")
    fileitems = [make_fileitem(path.as_posix()) for path in paths]
    chain = _prepare_chain(monkeypatch, fileitems)
    monkeypatch.setattr(
        "app.chain.media.album.AudioMetadataHelper.read_many",
        lambda requested: [
            MetaMusic(title=path.stem, track_number=int(path.stem))
            for path in requested
        ],
    )
    album = _album()

    state, message = TransferChain.do_transfer(
        chain,
        fileitem=_directory_item(album_dir),
        mediainfo=album,
        mtype=MediaType.MUSIC,
        media_source=MediaSource.MusicBrainz,
        media_id=album.media_id,
        background=False,
        preview=True,
    )

    assert state is False
    assert "只能对齐 2 / 4" in message
    assert "重复版本" in message
