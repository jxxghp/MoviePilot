from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from app.application.history import DownloadHistorySnapshot
from app.chain.media import MediaChain
from app.chain.transfer.facade import TransferChain
from app.chain.transfer.request import _should_discard_batch_music_identity
from app.domain.context import MusicAlbumInfo, MusicInfo
from app.domain.meta.metamusic import MetaMusic
from app.schemas.category import ClassificationResult, ClassificationSelection
from app.schemas.file import FileItem
from app.schemas.types import (
    MUSIC_ENTITY_ARTIST,
    MediaSource,
    MediaType,
)
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


def test_artist_collection_batch_discards_shared_artist_identity() -> None:
    """艺术家合集身份只属于下载任务，不能继承给内部音轨。"""
    artist = MusicInfo(
        title="许嵩",
        artists=["许嵩"],
        music_type=MUSIC_ENTITY_ARTIST,
        album_type="Artist Collection",
        library_category="Artist Collection",
    )

    assert _should_discard_batch_music_identity(
        manual=False,
        multi_track_music_batch=True,
        media_source=None,
        media_id=None,
        mediainfo=artist,
        history_music_type=None,
    ) is True
    assert _should_discard_batch_music_identity(
        manual=False,
        multi_track_music_batch=True,
        media_source=None,
        media_id=None,
        mediainfo=None,
        history_music_type=MUSIC_ENTITY_ARTIST,
    ) is True
    assert _should_discard_batch_music_identity(
        manual=True,
        multi_track_music_batch=False,
        media_source=None,
        media_id=None,
        mediainfo=None,
        history_music_type=None,
    ) is True
    assert _should_discard_batch_music_identity(
        manual=False,
        multi_track_music_batch=False,
        media_source=None,
        media_id=None,
        mediainfo=artist,
        history_music_type=MUSIC_ENTITY_ARTIST,
    ) is True


def test_download_history_recovers_artist_collection_entity() -> None:
    """下载监控逐文件触发时也必须从历史恢复艺术家合集任务类型。"""
    current = SimpleNamespace(
        music_type=MUSIC_ENTITY_ARTIST,
        note=None,
    )
    legacy = SimpleNamespace(
        music_type=None,
        note={"music": {"media": {"music_type": MUSIC_ENTITY_ARTIST}}},
    )

    assert TransferChain._download_history_music_type(current) == MUSIC_ENTITY_ARTIST
    assert TransferChain._download_history_music_type(legacy) == MUSIC_ENTITY_ARTIST


def test_artist_collection_single_monitor_event_rematches_child_release(
        tmp_path, monkeypatch,
) -> None:
    """下载监控逐文件触发时，子作品不得继承父合集分类。"""
    release_dir = tmp_path / "Taylor Swift" / "willow (2021)"
    release_dir.mkdir(parents=True)
    audio_path = release_dir / "01 - willow.flac"
    audio_path.write_bytes(b"audio")
    fileitem = make_fileitem(audio_path.as_posix())
    local_meta = MetaMusic(
        title="willow",
        artists=["Taylor Swift"],
        album="willow",
        year=2021,
        track_number=1,
    )
    matched_info = MusicInfo(
        media_source=MediaSource.MusicBrainz,
        media_id="recording-willow",
        music_type="recording",
        title="willow",
        artists=["Taylor Swift"],
        album="willow",
        album_id="release-group-willow",
        album_type="Single",
        library_category="Single",
        year=2021,
        track_number=1,
    )
    chain = _prepare_chain(monkeypatch, [fileitem])
    history = DownloadHistorySnapshot(
        id=1,
        path=release_dir.as_posix(),
        type=MediaType.MUSIC.value,
        title="Taylor Swift 艺术家合集",
        music_type=MUSIC_ENTITY_ARTIST,
        note=None,
        custom_words=None,
        downloader="qbittorrent",
        download_hash="artist-collection-hash",
    )
    monkeypatch.setattr(
        chain,
        "_resolve_download_history",
        lambda **_kwargs: history,
    )
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
        lambda _self, _path, **_kwargs: {
            str(audio_path.resolve()): deepcopy(matched_info),
        },
    )
    planned = []
    monkeypatch.setattr(
        chain,
        "_TransferChain__handle_transfer",
        lambda task, callback=None: (
            planned.append(
                (
                    task.mediainfo.media_id,
                    task.mediainfo.album_type,
                    task.mediainfo.library_category,
                )
            )
            or True,
            "",
        ),
    )

    state, message = TransferChain._execute_transfer(
        chain,
        fileitem=fileitem,
        selected_fileitems=[fileitem],
        mtype=MediaType.MUSIC,
        background=False,
    )

    assert state is True
    assert message == ""
    assert planned == [("recording-willow", "Single", "Single")]


def test_artist_collection_children_are_rematched_as_album(tmp_path, monkeypatch):
    """自动整理艺术家合集时，内部音轨与歌词应按专辑重新识别和分类。"""
    album_dir = tmp_path / "许嵩" / "自定义 (2009)"
    album_dir.mkdir(parents=True)
    audio_paths = [album_dir / "01.flac", album_dir / "02.flac"]
    paths = [
        audio_paths[0],
        album_dir / "01.lrc",
        audio_paths[1],
        album_dir / "02.lrc",
    ]
    for path in paths:
        path.write_bytes(b"audio")
    fileitems = [make_fileitem(path.as_posix()) for path in paths]
    local_metas = {
        audio_paths[0]: MetaMusic(title="如果当时", artists=["许嵩"], track_number=1),
        audio_paths[1]: MetaMusic(title="多余的解释", artists=["许嵩"], track_number=2),
    }
    album = MusicAlbumInfo(
        media_source=MediaSource.MusicBrainz,
        media_id="release-group-custom",
        title="自定义",
        artists=["许嵩"],
        album_type="Album",
        release_date="2009-01-10",
        library_category="Album",
        tracks=[
            MusicInfo(
                media_source=MediaSource.MusicBrainz,
                media_id=f"recording-{index}",
                title=meta.title,
                artists=["许嵩"],
                album="自定义",
                album_id="release-group-custom",
                album_type="Album",
                library_category="Album",
                track_number=index,
            )
            for index, meta in enumerate(local_metas.values(), start=1)
        ],
    )
    matched = {
        str(path.resolve()): deepcopy(track)
        for path, track in zip(audio_paths, album.tracks)
    }
    artist = MusicInfo(
        media_source=MediaSource.MusicBrainz,
        media_id="artist-xusong",
        title="许嵩",
        artists=["许嵩"],
        music_type=MUSIC_ENTITY_ARTIST,
        album_type="Artist Collection",
        library_category="Artist Collection",
    )
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
        planned.append(
            (
                Path(task.fileitem.path).suffix,
                task.mediainfo.album,
                task.mediainfo.album_type,
                task.mediainfo.library_category,
            )
        )
        return True, ""

    monkeypatch.setattr(chain, "_TransferChain__handle_transfer", handle_transfer)

    state, message = TransferChain._execute_transfer(
        chain,
        fileitem=fileitems[0],
        selected_fileitems=fileitems,
        mediainfo=artist,
        mtype=MediaType.MUSIC,
        background=False,
    )

    assert state is True
    assert message == ""
    assert planned == [
        (".flac", "自定义", "Album", "Album"),
        (".lrc", "自定义", "Album", "Album"),
        (".flac", "自定义", "Album", "Album"),
        (".lrc", "自定义", "Album", "Album"),
    ]


def test_artist_collection_album_miss_falls_back_to_recording_evidence(
        tmp_path, monkeypatch,
):
    """目录级专辑未命中时，缺发行类型的 MB Recording 按单曲归档。"""
    single_dir = tmp_path / "许嵩" / "绝代风华"
    single_dir.mkdir(parents=True)
    paths = [single_dir / "绝代风华.flac", single_dir / "绝代风华伴奏.flac"]
    for path in paths:
        path.write_bytes(b"audio")
    fileitems = [make_fileitem(path.as_posix()) for path in paths]
    local_metas = {
        path: MetaMusic(title=path.stem, artists=["许嵩"], track_number=index)
        for index, path in enumerate(paths, start=1)
    }
    artist = MusicInfo(
        media_source=MediaSource.MusicBrainz,
        media_id="artist-xusong",
        title="许嵩",
        artists=["许嵩"],
        music_type=MUSIC_ENTITY_ARTIST,
        album_type="Artist Collection",
        library_category="Artist Collection",
    )
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
        lambda _self, _path, **_kwargs: {},
    )

    def recognize_track(_self, path, media_source=None, contextual_meta=None):
        del media_source, contextual_meta
        meta = deepcopy(local_metas[path])
        return meta, MusicInfo(
            media_source=MediaSource.MusicBrainz,
            media_id=f"recording-{path.stem}",
            title=meta.title,
            artists=["许嵩"],
            album=meta.title,
            album_type="Single",
            library_category="Single",
            track_number=1,
        )

    monkeypatch.setattr(MediaChain, "recognize_music_by_path", recognize_track)
    planned = []
    monkeypatch.setattr(
        chain,
        "_TransferChain__handle_transfer",
        lambda task, callback=None: (
            planned.append(task.mediainfo.library_category) or True,
            "",
        ),
    )

    state, message = TransferChain._execute_transfer(
        chain,
        fileitem=fileitems[0],
        selected_fileitems=fileitems,
        mediainfo=artist,
        mtype=MediaType.MUSIC,
        background=False,
    )

    assert state is True
    assert message == ""
    assert planned == ["Single", "Single"]


def test_artist_collection_directory_consensus_constrains_untagged_track(
        tmp_path, monkeypatch,
) -> None:
    """同一专辑其他音轨的一致艺人标签应约束缺标签的同名曲。"""
    album_dir = tmp_path / "Taylor Swift" / "Speak Now (2010)"
    album_dir.mkdir(parents=True)
    paths = [
        album_dir / "01 - Mine.flac",
        album_dir / "02 - Sparks Fly.flac",
        album_dir / "05 - Dear John.flac",
    ]
    for path in paths:
        path.write_bytes(b"audio")
    fileitems = [make_fileitem(path.as_posix()) for path in paths]
    local_metas = {
        paths[0]: MetaMusic(title="Mine", artists=["Taylor Swift"], album="Speak Now"),
        paths[1]: MetaMusic(title="Sparks Fly", artists=["Taylor Swift"], album="Speak Now"),
        paths[2]: MetaMusic(title="Dear John"),
    }
    chain = _prepare_chain(monkeypatch, fileitems)
    monkeypatch.setattr(
        "app.chain.transfer.workflow.StorageChain.get_item",
        lambda _self, item: item,
    )
    monkeypatch.setattr(
        "app.chain.transfer.filter.AudioMetadataHelper.read_tags",
        lambda path: deepcopy(local_metas[path]),
    )
    monkeypatch.setattr(
        MediaChain,
        "read_path_meta",
        staticmethod(lambda path: deepcopy(local_metas[path])),
    )
    monkeypatch.setattr(
        MediaChain,
        "recognize_music_album_directory",
        lambda _self, _path, **_kwargs: {},
    )
    observed = []

    def recognize_track(_self, path, media_source=None, contextual_meta=None):
        del media_source
        observed.append((path.name, list(contextual_meta.artists), contextual_meta.album))
        return deepcopy(contextual_meta), MusicInfo(
            media_source=MediaSource.MusicBrainz,
            media_id=f"recording-{path.stem}",
            title=contextual_meta.title,
            artists=list(contextual_meta.artists),
            album=contextual_meta.album,
            album_type="Album",
            library_category="Album",
        )

    monkeypatch.setattr(MediaChain, "recognize_music_by_path", recognize_track)
    monkeypatch.setattr(
        chain,
        "_TransferChain__handle_transfer",
        lambda task, callback=None: (True, ""),
    )
    artist = MusicInfo(
        music_type=MUSIC_ENTITY_ARTIST,
        title="Taylor Swift",
        artists=["Taylor Swift"],
        album_type="Artist Collection",
    )

    state, message = TransferChain._execute_transfer(
        chain,
        fileitem=fileitems[0],
        selected_fileitems=fileitems,
        mediainfo=artist,
        mtype=MediaType.MUSIC,
        background=False,
    )

    assert state is True
    assert message == ""
    assert observed[-1] == ("05 - Dear John.flac", ["Taylor Swift"], "Speak Now")


def test_artist_collection_single_track_directory_has_local_single_fallback(
        tmp_path, monkeypatch,
):
    """艺术家合集单音轨目录远端未命中时，只在该受限结构下按 Single 归档。"""
    single_dir = tmp_path / "许嵩" / "天知道"
    single_dir.mkdir(parents=True)
    audio_path = single_dir / "许嵩 - 天知道.flac"
    lyric_path = single_dir / "许嵩 - 天知道.lrc"
    second_dir = tmp_path / "许嵩" / "合拍"
    second_dir.mkdir(parents=True)
    second_audio_path = second_dir / "许嵩 - 合拍.flac"
    second_lyric_path = second_dir / "许嵩 - 合拍.lrc"
    audio_path.write_bytes(b"audio")
    lyric_path.write_text("lyrics", encoding="utf-8")
    second_audio_path.write_bytes(b"audio")
    second_lyric_path.write_text("lyrics", encoding="utf-8")
    fileitems = [
        make_fileitem(path.as_posix())
        for path in (audio_path, lyric_path, second_audio_path, second_lyric_path)
    ]
    local_meta = MetaMusic(title="天知道", artists=["许嵩"], album="天知道")
    artist = MusicInfo(
        media_source=MediaSource.MusicBrainz,
        media_id="artist-xusong",
        title="许嵩",
        artists=["许嵩"],
        music_type=MUSIC_ENTITY_ARTIST,
        album_type="Artist Collection",
        library_category="Artist Collection",
    )
    chain = _prepare_chain(monkeypatch, fileitems)
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
        lambda _self, _path: (deepcopy(local_meta), MusicInfo.from_meta(local_meta)),
    )
    planned = []
    monkeypatch.setattr(
        chain,
        "_TransferChain__handle_transfer",
        lambda task, callback=None: (
            planned.append((Path(task.fileitem.path).suffix, task.mediainfo.library_category))
            or True,
            "",
        ),
    )

    state, message = TransferChain._execute_transfer(
        chain,
        fileitem=fileitems[0],
        selected_fileitems=fileitems,
        mediainfo=artist,
        mtype=MediaType.MUSIC,
        background=False,
    )

    assert state is True
    assert message == ""
    assert planned == [
        (".flac", "Single"),
        (".lrc", "Single"),
        (".flac", "Single"),
        (".lrc", "Single"),
    ]


def test_manual_single_track_directory_has_local_single_fallback(
        tmp_path, monkeypatch,
):
    """无显式媒体 ID 的手动单曲目录在远端不可用时仍可归入 Single。"""
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
        lambda _self, _path, **_kwargs: (
            deepcopy(local_meta), MusicInfo.from_meta(local_meta),
        ),
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


def test_artist_collection_multi_track_directory_has_consensus_album_fallback(
        tmp_path, monkeypatch,
):
    """远端不可用时，同目录多首且艺人/专辑标签高度一致可安全按 Album 归档。"""
    album_dir = tmp_path / "Taylor Swift" / "Speak Now"
    album_dir.mkdir(parents=True)
    audio_paths = [
        album_dir / "01 - Mine.flac",
        album_dir / "02 - Sparks Fly.flac",
    ]
    for path in audio_paths:
        path.write_bytes(b"audio")
    fileitems = [make_fileitem(path.as_posix()) for path in audio_paths]
    local_metas = {
        audio_paths[0]: MetaMusic(
            title="Mine", artists=["Taylor Swift"], album="Speak Now", year=2010,
        ),
        audio_paths[1]: MetaMusic(
            title="Sparks Fly", artists=["Taylor Swift"], album="Speak Now", year=2010,
        ),
    }
    artist = MusicInfo(
        media_source=MediaSource.MusicBrainz,
        media_id="artist-taylor-swift",
        title="Taylor Swift",
        artists=["Taylor Swift"],
        music_type=MUSIC_ENTITY_ARTIST,
        album_type="Artist Collection",
        library_category="Artist Collection",
    )
    chain = _prepare_chain(monkeypatch, fileitems)
    monkeypatch.setattr(
        "app.chain.transfer.workflow.StorageChain.get_item",
        lambda _self, item: item,
    )
    monkeypatch.setattr(
        "app.chain.transfer.filter.AudioMetadataHelper.read_tags",
        lambda path: deepcopy(local_metas[path]),
    )
    monkeypatch.setattr(
        MediaChain,
        "read_path_meta",
        staticmethod(lambda path: deepcopy(local_metas[Path(path)])),
    )
    monkeypatch.setattr(
        MediaChain,
        "recognize_music_album_directory",
        lambda _self, _path, **_kwargs: {},
    )
    monkeypatch.setattr(
        MediaChain,
        "recognize_music_by_path",
        lambda _self, path, **_kwargs: (
            deepcopy(local_metas[Path(path)]),
            MusicInfo.from_meta(local_metas[Path(path)]),
        ),
    )
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
        mediainfo=artist,
        mtype=MediaType.MUSIC,
        background=False,
    )

    assert state is True
    assert message == ""
    assert planned == [("Album", "Album"), ("Album", "Album")]


def test_same_directory_tagged_disc_groups_are_independent_albums(
        tmp_path, monkeypatch,
):
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
    artist = MusicInfo(
        media_source=MediaSource.MusicBrainz,
        media_id="artist-taylor-swift",
        title="Taylor Swift",
        artists=["Taylor Swift"],
        music_type=MUSIC_ENTITY_ARTIST,
        album_type="Artist Collection",
        library_category="Artist Collection",
    )
    chain = _prepare_chain(monkeypatch, fileitems)
    monkeypatch.setattr(
        "app.chain.transfer.workflow.StorageChain.get_item",
        lambda _self, item: item,
    )
    monkeypatch.setattr(
        "app.chain.transfer.filter.AudioMetadataHelper.read_tags",
        lambda path: deepcopy(local_metas[path]),
    )
    monkeypatch.setattr(
        MediaChain,
        "read_path_meta",
        staticmethod(lambda path: deepcopy(local_metas[Path(path)])),
    )
    monkeypatch.setattr(
        MediaChain,
        "recognize_music_album_directory",
        lambda _self, _path, **_kwargs: {},
    )
    monkeypatch.setattr(
        MediaChain,
        "recognize_music_by_path",
        lambda _self, path, **_kwargs: (
            deepcopy(local_metas[Path(path)]),
            MusicInfo.from_meta(local_metas[Path(path)]),
        ),
    )
    planned = []
    monkeypatch.setattr(
        chain,
        "_TransferChain__handle_transfer",
        lambda task, callback=None: (
            planned.append((task.mediainfo.album, task.mediainfo.library_category))
            or True,
            "",
        ),
    )

    state, message = TransferChain._execute_transfer(
        chain,
        fileitem=fileitems[0],
        selected_fileitems=fileitems,
        mediainfo=artist,
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


def test_directory_year_is_reapplied_after_album_recognition(
        tmp_path, monkeypatch,
):
    """远端再版年份不能覆盖手动合集目录明确给出的发行年份。"""
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
    artist = MusicInfo(
        media_source=MediaSource.MusicBrainz,
        media_id="artist-taylor-swift",
        title="Taylor Swift",
        artists=["Taylor Swift"],
        music_type=MUSIC_ENTITY_ARTIST,
        album_type="Artist Collection",
        library_category="Artist Collection",
    )
    chain = _prepare_chain(monkeypatch, fileitems)
    monkeypatch.setattr(
        "app.chain.transfer.workflow.StorageChain.get_item",
        lambda _self, item: item,
    )
    monkeypatch.setattr(
        "app.chain.transfer.filter.AudioMetadataHelper.read_tags",
        lambda path: deepcopy(local_metas[path]),
    )
    monkeypatch.setattr(
        MediaChain,
        "read_path_meta",
        staticmethod(lambda path: deepcopy(local_metas[Path(path)])),
    )

    def recognize_album(_self, _path, **_kwargs):
        return {
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
        recognize_album,
    )
    monkeypatch.setattr(
        MediaChain,
        "recognize_music_by_path",
        lambda _self, path, **_kwargs: (
            deepcopy(local_metas[Path(path)]),
            MusicInfo.from_meta(local_metas[Path(path)]),
        ),
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
        mediainfo=artist,
        mtype=MediaType.MUSIC,
        background=False,
    )

    assert state is True
    assert message == ""
    assert planned == [2020, 2020]


def test_album_directory_consensus_overrides_title_track_single_release(
        tmp_path, monkeypatch,
):
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
    artist = MusicInfo(
        media_source=MediaSource.MusicBrainz,
        media_id="artist-taylor-swift",
        title="Taylor Swift",
        artists=["Taylor Swift"],
        music_type=MUSIC_ENTITY_ARTIST,
        album_type="Artist Collection",
        library_category="Artist Collection",
    )
    chain = _prepare_chain(monkeypatch, fileitems)
    monkeypatch.setattr(
        "app.chain.transfer.workflow.StorageChain.get_item",
        lambda _self, item: item,
    )
    monkeypatch.setattr(
        "app.chain.transfer.filter.AudioMetadataHelper.read_tags",
        lambda path: deepcopy(local_metas[path]),
    )
    monkeypatch.setattr(
        MediaChain,
        "read_path_meta",
        staticmethod(lambda path: deepcopy(local_metas[Path(path)])),
    )
    monkeypatch.setattr(
        MediaChain,
        "recognize_music_album_directory",
        lambda _self, _path, **_kwargs: {},
    )

    def recognize_track(_self, path, **_kwargs):
        meta = deepcopy(local_metas[Path(path)])
        info = MusicInfo.from_meta(meta)
        info.media_source = MediaSource.MusicBrainz
        info.media_id = f"recording-{Path(path).stem}"
        if "Speak Now" in Path(path).stem:
            info.album = "Speak Now: Deluxe Edition"
            info.album_type = "Single"
            info.set_library_category("Single")
        else:
            info.album_type = "Album"
            info.set_library_category("Album")
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
        mediainfo=artist,
        mtype=MediaType.MUSIC,
        background=False,
    )

    assert state is True
    assert message == ""
    assert planned == [
        ("Speak Now", "Album", "Album"),
        ("Speak Now", "Album", "Album"),
    ]


def test_album_directory_consensus_preserves_explicit_ep_type(
        tmp_path, monkeypatch,
):
    """多音轨目录已由 MusicBrainz 明确识别为 EP 时不得强制改成 Album。"""
    ep_dir = tmp_path / "Taylor Swift" / "The Taylor Swift Holiday Collection"
    ep_dir.mkdir(parents=True)
    audio_paths = [ep_dir / "01.flac", ep_dir / "02.flac"]
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
    artist = MusicInfo(
        media_source=MediaSource.MusicBrainz,
        media_id="artist-taylor-swift",
        title="Taylor Swift",
        artists=["Taylor Swift"],
        music_type=MUSIC_ENTITY_ARTIST,
        album_type="Artist Collection",
        library_category="Artist Collection",
    )
    chain = _prepare_chain(monkeypatch, fileitems)
    monkeypatch.setattr(
        "app.chain.transfer.workflow.StorageChain.get_item",
        lambda _self, item: item,
    )
    monkeypatch.setattr(
        "app.chain.transfer.filter.AudioMetadataHelper.read_tags",
        lambda path: deepcopy(local_metas[path]),
    )
    monkeypatch.setattr(
        MediaChain,
        "read_path_meta",
        staticmethod(lambda path: deepcopy(local_metas[Path(path)])),
    )
    monkeypatch.setattr(
        MediaChain,
        "recognize_music_album_directory",
        lambda _self, _path, **_kwargs: {},
    )

    def recognize_ep(_self, path, **_kwargs):
        meta = deepcopy(local_metas[Path(path)])
        info = MusicInfo.from_meta(meta)
        info.media_source = MediaSource.MusicBrainz
        info.media_id = f"recording-{Path(path).stem}"
        info.album_type = "EP"
        info.set_library_category("EP")
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
        mediainfo=artist,
        mtype=MediaType.MUSIC,
        background=False,
    )

    assert state is True
    assert message == ""
    assert planned == [("EP", "EP"), ("EP", "EP")]


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
