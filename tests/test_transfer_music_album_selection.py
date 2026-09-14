from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from app.chain.media import MediaChain
from app.chain.transfer.facade import TransferChain
from app.domain.context import MusicAlbumInfo, MusicInfo
from app.domain.meta.metamusic import MetaMusic
from app.schemas.category import ClassificationResult, ClassificationSelection
from app.schemas.file import FileItem
from app.schemas.types import MUSIC_ENTITY_RECORDING, MediaSource, MediaType
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


def _directory_item(path: Path) -> FileItem:
    """构造手动整理使用的专辑目录项。"""
    return FileItem(
        storage="local",
        path=path.as_posix() + "/",
        type="dir",
        name=path.name,
    )


def _prepare_chain(monkeypatch, fileitems: list[FileItem]):
    """隔离文件规划外部依赖，返回可同步观察的整理链。"""
    chain = make_transfer_chain()
    bind_empty_history_repositories(chain)
    monkeypatch.setattr(
        chain,
        "_TransferChain__get_trans_fileitems",
        lambda _fileitem, predicate: [
            (item, False) for item in fileitems if predicate(item, False)
        ],
    )
    monkeypatch.setattr(chain, "_TransferChain__put_to_jobview", lambda _task: True)
    monkeypatch.setattr(chain, "_register_scrape_batch_task", lambda _task: None)
    monkeypatch.setattr(chain, "_close_scrape_batch", lambda _batch_id: None)
    monkeypatch.setattr(
        "app.chain.transfer.workflow.get_configured_system_config",
        lambda: SimpleNamespace(get=lambda _key: None),
    )
    return chain


def _shared_recording() -> MusicInfo:
    """构造会触发整批重新识别的共享单曲上下文。"""
    return MusicInfo(
        media_source=MediaSource.MusicBrainz,
        media_id="recording-shared",
        music_type=MUSIC_ENTITY_RECORDING,
        title="Shared recording",
        artists=["Taylor Swift"],
        album_type="Single",
    )


def _patch_local_music_reads(monkeypatch, local_metas: dict[Path, MetaMusic]) -> None:
    """把批次规划中的本地标签读取固定为测试证据。"""
    monkeypatch.setattr(
        "app.chain.transfer.filter.AudioMetadataHelper.read_tags",
        lambda path: deepcopy(local_metas[Path(path)]),
    )
    monkeypatch.setattr(
        MediaChain,
        "read_path_meta",
        staticmethod(lambda path: deepcopy(local_metas[Path(path)])),
    )


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
    monkeypatch.setattr(
        chain,
        "_TransferChain__handle_transfer",
        lambda task, callback=None: (
            planned.append((task.meta.title, task.mediainfo.title, task.mediainfo.library_category))
            or True,
            "",
        ),
    )

    state, message = TransferChain.do_transfer(
        chain,
        fileitem=_directory_item(album_dir),
        mediainfo=_album(),
        mtype=MediaType.MUSIC,
        media_source=MediaSource.MusicBrainz,
        media_id="release-group-1",
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
    monkeypatch.setattr(
        chain,
        "_TransferChain__handle_transfer",
        lambda task, callback=None: (
            planned.append((task.meta.title, task.mediainfo.library_category)) or True,
            "",
        ),
    )

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
    monkeypatch.setattr(
        chain,
        "_TransferChain__handle_transfer",
        lambda task, callback=None: (
            planned.append((task.meta.title, task.mediainfo.album, task.mediainfo.library_category))
            or True,
            "",
        ),
    )

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


def test_manual_single_track_directory_uses_local_single_and_directory_year(
        tmp_path, monkeypatch,
):
    """无远端命中时，手动单曲目录应按 Single 归档并采用目录年份。"""
    single_dir = tmp_path / "Taylor Swift" / "2010-Today Was a Fairytale"
    single_dir.mkdir(parents=True)
    audio_path = single_dir / "Taylor Swift - Today Was a Fairytale.flac"
    audio_path.write_bytes(b"audio")
    fileitem = make_fileitem(audio_path.as_posix())
    local_meta = MetaMusic(
        title="Today Was a Fairytale",
        artists=["Taylor Swift"],
        album="Today Was a Fairytale",
        year=2011,
    )
    chain = _prepare_chain(monkeypatch, [fileitem])
    monkeypatch.setattr(
        "app.chain.transfer.workflow.StorageChain.get_item",
        lambda _self, item: item,
    )
    monkeypatch.setattr(
        MediaChain,
        "read_path_meta",
        staticmethod(lambda _path: deepcopy(local_meta)),
    )
    monkeypatch.setattr(
        MediaChain,
        "recognize_music_album_directory",
        lambda _self, _path, **_kwargs: {},
    )
    monkeypatch.setattr(
        MediaChain,
        "recognize_music_by_path",
        lambda _self, _path, **_kwargs: (deepcopy(local_meta), None),
    )
    planned = []
    monkeypatch.setattr(
        chain,
        "_TransferChain__handle_transfer",
        lambda task, callback=None: (
            planned.append((task.mediainfo.library_category, task.mediainfo.year)) or True,
            "",
        ),
    )

    state, message = TransferChain._execute_transfer(
        chain,
        fileitem=fileitem,
        selected_fileitems=[fileitem],
        mtype=MediaType.MUSIC,
        background=False,
        manual=True,
    )

    assert state is True
    assert message == ""
    assert planned == [("Single", 2010)]


def test_same_directory_tagged_disc_groups_are_independent_albums(tmp_path, monkeypatch):
    """同一物理目录中的 CD1/CD2 标签分组都应获得独立且稳定的 Album 上下文。"""
    album_dir = tmp_path / "Taylor Swift" / "2012-Red Deluxe"
    album_dir.mkdir(parents=True)
    audio_paths = [album_dir / f"{index:02d}.flac" for index in range(1, 5)]
    for path in audio_paths:
        path.write_bytes(b"audio")
    fileitems = [make_fileitem(path.as_posix()) for path in audio_paths]
    local_metas = {
        path: MetaMusic(
            title=f"Track {index}",
            artists=["Taylor Swift"],
            album="Red Deluxe CD1" if index <= 2 else "Red Deluxe CD2",
            year=2012,
        )
        for index, path in enumerate(audio_paths, 1)
    }
    chain = _prepare_chain(monkeypatch, fileitems)
    monkeypatch.setattr(
        "app.chain.transfer.workflow.StorageChain.get_item",
        lambda _self, item: item,
    )
    _patch_local_music_reads(monkeypatch, local_metas)
    monkeypatch.setattr(
        MediaChain,
        "recognize_music_album_directory",
        lambda _self, _path, **_kwargs: {},
    )
    monkeypatch.setattr(
        MediaChain,
        "recognize_music_by_path",
        lambda _self, _path, **_kwargs: (None, None),
    )
    planned = []
    monkeypatch.setattr(
        chain,
        "_TransferChain__handle_transfer",
        lambda task, callback=None: (
            planned.append((task.mediainfo.album, task.mediainfo.library_category)) or True,
            "",
        ),
    )

    state, message = TransferChain._execute_transfer(
        chain,
        fileitem=fileitems[0],
        selected_fileitems=fileitems,
        mediainfo=_shared_recording(),
        mtype=MediaType.MUSIC,
        background=False,
    )

    assert state is True
    assert message == ""
    assert planned == [
        ("Red Deluxe CD1", "Album"),
        ("Red Deluxe CD1", "Album"),
        ("Red Deluxe CD2", "Album"),
        ("Red Deluxe CD2", "Album"),
    ]


def test_directory_year_is_reapplied_after_album_recognition(tmp_path, monkeypatch):
    """远端再版年份不能覆盖发行目录明确给出的年份。"""
    album_dir = tmp_path / "Taylor Swift" / "2020-evermore" / "CD1"
    album_dir.mkdir(parents=True)
    audio_paths = [album_dir / "01.flac", album_dir / "02.flac"]
    for path in audio_paths:
        path.write_bytes(b"audio")
    fileitems = [make_fileitem(path.as_posix()) for path in audio_paths]
    local_metas = {
        path: MetaMusic(
            title=f"Track {index}",
            artists=["Taylor Swift"],
            album="evermore",
            year=2021,
        )
        for index, path in enumerate(audio_paths, 1)
    }
    chain = _prepare_chain(monkeypatch, fileitems)
    monkeypatch.setattr(
        "app.chain.transfer.workflow.StorageChain.get_item",
        lambda _self, item: item,
    )
    _patch_local_music_reads(monkeypatch, local_metas)
    remote = {
        str(path.resolve()): MusicInfo(
            media_source=MediaSource.MusicBrainz,
            media_id=f"release-{index}",
            title=local_metas[path].title,
            artists=["Taylor Swift"],
            album_artist="Taylor Swift",
            album="evermore",
            album_type="Album",
            year=2021,
        )
        for index, path in enumerate(audio_paths, 1)
    }
    monkeypatch.setattr(
        MediaChain,
        "recognize_music_album_directory",
        lambda _self, _path, **_kwargs: remote,
    )
    monkeypatch.setattr(
        MediaChain,
        "recognize_music_by_path",
        lambda _self, _path, **_kwargs: (None, None),
    )
    planned = []
    monkeypatch.setattr(
        chain,
        "_TransferChain__handle_transfer",
        lambda task, callback=None: (
            planned.append(task.mediainfo.year) or True,
            "",
        ),
    )

    state, message = TransferChain._execute_transfer(
        chain,
        fileitem=fileitems[0],
        selected_fileitems=fileitems,
        mediainfo=_shared_recording(),
        mtype=MediaType.MUSIC,
        background=False,
    )

    assert state is True
    assert message == ""
    assert planned == [2020, 2020]


def test_album_directory_consensus_overrides_title_track_single_release(tmp_path, monkeypatch):
    """专辑目录中的同名单曲命中不能把该曲拆入 Single 分类。"""
    album_dir = tmp_path / "Taylor Swift" / "Speak Now (2010)"
    album_dir.mkdir(parents=True)
    audio_paths = [album_dir / "01 - Mine.flac", album_dir / "04 - Speak Now.flac"]
    for path in audio_paths:
        path.write_bytes(b"audio")
    fileitems = [make_fileitem(path.as_posix()) for path in audio_paths]
    local_metas = {
        path: MetaMusic(
            title=path.stem.split(" - ", 1)[1],
            artists=["Taylor Swift"],
            album="Speak Now",
            year=2010,
        )
        for path in audio_paths
    }
    chain = _prepare_chain(monkeypatch, fileitems)
    monkeypatch.setattr(
        "app.chain.transfer.workflow.StorageChain.get_item",
        lambda _self, item: item,
    )
    _patch_local_music_reads(monkeypatch, local_metas)
    monkeypatch.setattr(
        MediaChain,
        "recognize_music_album_directory",
        lambda _self, _path, **_kwargs: {},
    )

    def recognize_track(_self, path, **_kwargs):
        """构造把目录内曲目误识别为单曲发行的候选。"""
        meta = deepcopy(local_metas[Path(path)])
        info = MusicInfo(
            media_source=MediaSource.MusicBrainz,
            media_id=f"recording-{Path(path).stem}",
            title=meta.title,
            artists=list(meta.artists),
            album="Speak Now: Deluxe Edition",
            album_type="Single",
            year=2010,
        )
        return meta, info

    monkeypatch.setattr(MediaChain, "recognize_music_by_path", recognize_track)
    planned = []
    monkeypatch.setattr(
        chain,
        "_TransferChain__handle_transfer",
        lambda task, callback=None: (
            planned.append((
                task.mediainfo.album,
                task.mediainfo.album_type,
                task.mediainfo.library_category,
            ))
            or True,
            "",
        ),
    )

    state, message = TransferChain._execute_transfer(
        chain,
        fileitem=fileitems[0],
        selected_fileitems=fileitems,
        mediainfo=_shared_recording(),
        mtype=MediaType.MUSIC,
        background=False,
    )

    assert state is True
    assert message == ""
    assert planned == [("Speak Now", "Album", "Album")] * 2


def test_album_directory_consensus_preserves_explicit_ep_type(tmp_path, monkeypatch):
    """多音轨目录已由 MusicBrainz 明确识别为 EP 时不得强制改成 Album。"""
    album_dir = tmp_path / "Taylor Swift" / "The Taylor Swift Holiday Collection"
    album_dir.mkdir(parents=True)
    audio_paths = [album_dir / "01.flac", album_dir / "02.flac"]
    for path in audio_paths:
        path.write_bytes(b"audio")
    fileitems = [make_fileitem(path.as_posix()) for path in audio_paths]
    local_metas = {
        path: MetaMusic(
            title=f"Holiday {index}",
            artists=["Taylor Swift"],
            album="The Taylor Swift Holiday Collection",
            year=2007,
        )
        for index, path in enumerate(audio_paths, 1)
    }
    chain = _prepare_chain(monkeypatch, fileitems)
    monkeypatch.setattr(
        "app.chain.transfer.workflow.StorageChain.get_item",
        lambda _self, item: item,
    )
    _patch_local_music_reads(monkeypatch, local_metas)
    monkeypatch.setattr(
        MediaChain,
        "recognize_music_album_directory",
        lambda _self, _path, **_kwargs: {},
    )

    def recognize_ep(_self, path, **_kwargs):
        """构造保留发行类型的 EP 识别结果。"""
        meta = deepcopy(local_metas[Path(path)])
        info = MusicInfo(
            media_source=MediaSource.MusicBrainz,
            media_id=f"recording-{Path(path).stem}",
            title=meta.title,
            artists=list(meta.artists),
            album=meta.album,
            album_type="EP",
            year=2007,
            library_category="EP",
        )
        return meta, info

    monkeypatch.setattr(MediaChain, "recognize_music_by_path", recognize_ep)
    planned = []
    monkeypatch.setattr(
        chain,
        "_TransferChain__handle_transfer",
        lambda task, callback=None: (
            planned.append((task.mediainfo.album_type, task.mediainfo.library_category))
            or True,
            "",
        ),
    )

    state, message = TransferChain._execute_transfer(
        chain,
        fileitem=fileitems[0],
        selected_fileitems=fileitems,
        mediainfo=_shared_recording(),
        mtype=MediaType.MUSIC,
        background=False,
    )

    assert state is True
    assert message == ""
    assert planned == [("EP", "EP")] * 2


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

    state, message = TransferChain.do_transfer(
        chain,
        fileitem=_directory_item(album_dir),
        mediainfo=_album(),
        mtype=MediaType.MUSIC,
        media_source=MediaSource.MusicBrainz,
        media_id="release-group-1",
        background=False,
        preview=True,
    )

    assert state is False
    assert "只能对齐 2 / 4" in message
    assert "重复版本" in message
