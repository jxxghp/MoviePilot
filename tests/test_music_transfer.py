from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from jinja2 import Template

from app.application.history import DownloadHistorySnapshot
from app.application.messaging.message import TemplateHelper
from app.application.transfer.execution import (
    TransferExecutionCheckpoint,
    TransferSettlementResult,
)
from app.application.transfer.workflow import JobManager, TransferTask
from app.chain.media import MediaChain
from app.chain.transfer import TransferChain  # pylint: disable=no-name-in-module
from app.domain.context import MusicInfo
from app.domain.meta.metamusic import MetaMusic
from app.runtime.config import settings
from app.schemas.file import FileItem
from app.schemas.system import TransferDirectoryConf
from app.schemas.transfer import TransferInfo, TransferTorrent
from app.schemas.types import EventType, MediaType


def _music_context() -> tuple[MetaMusic, MusicInfo]:
    """构造整理测试使用的音乐元数据和媒体信息。"""
    info = MusicInfo(
        media_source="musicbrainz",
        media_id="recording-1",
        title="Get Lucky",
        artists=["Daft Punk", "Pharrell Williams"],
        album="Random Access Memories",
        album_artist="Daft Punk",
        year=2013,
        track_number=8,
        total_tracks=13,
        category="Album",
    )
    return MetaMusic.from_music_info(info), info


def _music_task(path: str, info: MusicInfo) -> TransferTask:
    """构造带完整音乐上下文的整理作业任务。"""
    file_path = Path(path)
    return TransferTask(
        fileitem=FileItem(
            storage="local",
            path=file_path.as_posix(),
            name=file_path.name,
            basename=file_path.stem,
            type="file",
            extension=file_path.suffix.lstrip("."),
        ),
        meta=MetaMusic.from_music_info(info),
        mediainfo=info,
        mtype=MediaType.MUSIC,
    )


def test_music_retry_restores_history_entity_namespace(tmp_path, monkeypatch):
    """重新整理按历史身份恢复音乐时必须保留单曲或专辑实体类型。"""
    history = SimpleNamespace(
        type=MediaType.MUSIC.value,
        media_source="musicbrainz",
        media_id="release-group-1",
        music_type="album",
    )
    media_chain = Mock()
    media_chain.recognize_media.return_value = MusicInfo(
        media_source="musicbrainz",
        media_id="release-group-1",
        music_type="album",
        title="叶惠美",
    )
    monkeypatch.setattr("app.chain.transfer.queue.MediaChain", lambda: media_chain)
    monkeypatch.setattr("app.chain.transfer.filter.MediaChain", lambda: media_chain)

    result = TransferChain()._recognize_music_retry_media(
        history,
        tmp_path / "叶惠美",
    )

    assert result and result.music_type == "album"
    assert media_chain.recognize_media.call_args.kwargs["music_type"] == "album"


def test_music_rename_context_contains_audio_fields():
    """重命名模板上下文应提供艺术家、专辑、盘号和曲序字段。"""
    meta, info = _music_context()

    context = TemplateHelper().builder.build(
        meta=meta,
        mediainfo=info,
        file_extension=".flac",
        include_raw_objects=False,
    )

    assert context["artist"] == "Daft Punk ／ Pharrell Williams"
    assert context["album"] == "Random Access Memories"
    assert context["track"] == "08"
    assert context["fileExt"] == ".flac"


def test_music_rename_prefers_track_meta_over_album_media():
    """专辑整理时应使用每个文件的曲名和曲序，不能把专辑名写成所有目标文件名。"""
    meta = MetaMusic(
        org_string="10. 明天晴天.m4a",
        title="明天晴天",
        artists=["孙燕姿"],
        album="完美的一天",
        album_artist="孙燕姿",
        year=2005,
        track_number=10,
        total_tracks=11,
    )
    album = MusicInfo(
        media_source="musicbrainz",
        media_id="album-1",
        music_type="album",
        title="完美的一天",
        artists=["孙燕姿"],
        album="完美的一天",
        album_artist="孙燕姿",
        year=2005,
        total_tracks=11,
    )

    context = TemplateHelper().builder.build(
        meta=meta,
        mediainfo=album,
        file_extension=".m4a",
        include_raw_objects=False,
    )
    rendered = Template(settings.MUSIC_RENAME_FORMAT).render(context)

    assert context["title"] == "明天晴天"
    assert context["track"] == "10"
    assert rendered == "孙燕姿/完美的一天 (2005)/10 - 明天晴天.m4a"


def test_music_rename_format_is_independent_from_movie_format():
    """音乐应使用独立重命名模板且保持影视模板不变。"""
    assert settings.RENAME_FORMAT(MediaType.MUSIC) == settings.MUSIC_RENAME_FORMAT
    assert settings.RENAME_FORMAT(MediaType.MOVIE) == settings.MOVIE_RENAME_FORMAT


def test_audio_is_primary_only_in_music_context():
    """音频文件只在音乐上下文中作为主要媒体文件。"""
    chain = TransferChain()
    audio = FileItem(
        storage="local",
        path="/music/track.flac",
        name="track.flac",
        basename="track",
        type="file",
        extension="flac",
    )

    assert chain._is_primary_media_file(audio, MusicInfo(title="Track")) is True
    assert chain._is_primary_media_file(audio, None) is False


def test_music_scrape_batch_event_preserves_each_track_context():
    """同一专辑目录的批次刮削事件应保留每个目标音轨自己的识别身份。"""
    chain = object.__new__(TransferChain)
    chain._audio_exts = settings.RMT_AUDIOEXT
    chain._media_exts = settings.RMT_MEDIAEXT
    chain._scrape_batches = {}
    chain.eventmanager = Mock()
    batch_id = "music-scrape-batch"
    target_dir = FileItem(storage="local", path="/library/Album", type="dir")
    tasks = []
    target_paths = []

    for number, title in ((1, "Track One"), (2, "Track Two")):
        source = FileItem(
            storage="local",
            path=f"/downloads/{number:02d}.flac",
            type="file",
            name=f"{number:02d}.flac",
            extension="flac",
        )
        target_path = f"/library/Album/{number:02d} - {title}.flac"
        task = TransferTask(
            fileitem=source,
            meta=MetaMusic(title=title, track_number=number),
            mediainfo=MusicInfo(
                media_source="musicbrainz",
                media_id=f"recording-{number}",
                title=title,
                track_number=number,
            ),
            transfer_batch_id=batch_id,
        )
        tasks.append(task)
        target_paths.append(target_path)
        chain._register_scrape_batch_task(task)
        chain._record_scrape_target(
            task,
            TransferInfo(
                success=True,
                target_diritem=target_dir,
                file_list_new=[target_path],
                need_scrape=True,
            ),
        )

    chain._close_scrape_batch(batch_id)
    for task in tasks:
        chain._finish_scrape_batch_task(task)

    scrape_calls = [
        call
        for call in chain.eventmanager.send_event.call_args_list
        if call.args[0] == EventType.MetadataScrape
    ]
    assert len(scrape_calls) == 1
    payload = scrape_calls[0].args[1]
    assert payload["file_list"] == target_paths
    assert [
        context["mediainfo"].media_id for context in payload["file_contexts"]
    ] == ["recording-1", "recording-2"]


def test_restore_music_context_from_download_history():
    """自动整理应从下载历史备注恢复标准音乐身份。"""
    meta, info = _music_context()
    history = SimpleNamespace(
        note={
            "music": {
                "version": 1,
                "meta": meta.to_dict(),
                "media": info.to_dict(),
            }
        }
    )

    restored_meta, restored_info = TransferChain._restore_music_download_context(
        history,
        Path("/remote/08 - Get Lucky.flac"),
    )

    assert restored_meta is not None
    assert restored_info is not None
    assert restored_meta.org_string == "08 - Get Lucky.flac"
    assert restored_info.media_source.value == "musicbrainz"
    assert restored_info.media_id == "recording-1"
    assert restored_info.album == "Random Access Memories"


def test_restore_music_context_discards_shared_recording_identity(tmp_path, monkeypatch):
    """多音轨批次不得把下载记录中的单曲身份恢复到每个音频文件。"""
    meta, info = _music_context()
    history = DownloadHistorySnapshot(
        id=1,
        path=tmp_path.as_posix(),
        type=MediaType.MUSIC.value,
        title="Random Access Memories",
        note={
            "music": {
                "version": 1,
                "meta": meta.to_dict(),
                "media": info.to_dict(),
            }
        },
    )
    audio_file = tmp_path / "01 - Give Life Back to Music.flac"
    audio_file.write_bytes(b"fake-flac")
    file_meta = MetaMusic(
        org_string=audio_file.name,
        title="Give Life Back to Music",
        artists=["Daft Punk"],
        album="Local Album",
        album_artist="Daft Punk",
        year=2020,
        track_number=1,
    )
    monkeypatch.setattr(MediaChain, "read_path_meta", Mock(return_value=file_meta))

    restored_meta, restored_info = TransferChain._restore_music_download_context(
        history,
        audio_file,
        discard_recording_identity=True,
    )

    assert restored_meta is not None
    assert restored_meta.title == "Give Life Back to Music"
    assert restored_meta.album == "Local Album"
    assert restored_meta.year == 2020
    assert restored_meta.media_source is None
    assert restored_meta.media_id is None
    assert restored_info is None


def test_download_history_music_type_falls_back_to_versioned_note():
    """旧下载记录缺少独立字段时应从版本化备注恢复实体类型。"""
    history = SimpleNamespace(
        music_type=None,
        note={
            "music": {
                "version": 1,
                "media": {"music_type": "album"},
            }
        },
    )

    assert TransferChain._download_history_music_type(history) == "album"


def test_restore_album_context_keeps_album_identity_and_track_specific_tags(tmp_path, monkeypatch):
    """整专整理应保留选中的专辑身份，同时使用每个文件自己的曲名、艺术家和曲序。"""
    album = MusicInfo(
        media_source="musicbrainz",
        media_id="release-group-1",
        music_type="album",
        title="叶惠美",
        artists=["周杰伦"],
        album="叶惠美",
        album_artist="周杰伦",
        year=2003,
        total_tracks=11,
    )
    meta = MetaMusic.from_music_info(album)
    history = SimpleNamespace(note={
        "music": {
            "version": 1,
            "meta": meta.to_dict(),
            "media": album.to_dict(),
        }
    })
    audio_file = tmp_path / "03. 晴天.flac"
    audio_file.write_bytes(b"fake-flac")

    from app.application.audio import AudioMetadataHelper

    monkeypatch.setattr(
        AudioMetadataHelper,
        "read",
        lambda path: MetaMusic(
            org_string=path.name,
            title="晴天",
            artists=["周杰伦"],
            album="错误专辑",
            album_artist="错误艺术家",
            year=1999,
            track_number=3,
            total_tracks=99,
        ),
    )

    restored_meta, restored_info = TransferChain._restore_music_download_context(history, audio_file)

    assert restored_meta.title == "晴天"
    assert restored_meta.track_number == 3
    assert restored_meta.album == "叶惠美"
    assert restored_meta.album_artist == "周杰伦"
    assert restored_meta.year == 2003
    assert restored_meta.total_tracks == 11
    assert restored_info.music_type == "album"
    assert restored_info.media_id == "release-group-1"


def test_restore_music_context_uses_file_title_over_subscription_title(tmp_path, monkeypatch):
    """曲目标题应优先取当前文件自身的标签/文件名，而非沿用订阅时的单曲标题。"""
    meta, info = _music_context()
    history = SimpleNamespace(
        note={
            "music": {
                "version": 1,
                "meta": meta.to_dict(),
                "media": info.to_dict(),
            }
        }
    )
    # 订阅标题被识别为单曲"Get Lucky"，而实际音轨标签曲目名 不同且无身份字段
    audio_file = tmp_path / "07.幸福.flac"
    audio_file.write_bytes(b"fake-flac")

    from app.application.audio import AudioMetadataHelper

    monkeypatch.setattr(
        AudioMetadataHelper,
        "read",
        lambda path: MetaMusic(org_string=path.name, title="流浪地图"),
    )

    restored_meta, _ = TransferChain._restore_music_download_context(history, audio_file)

    assert restored_meta is not None
    assert restored_meta.title == "流浪地图"


def test_restore_music_context_uses_filename_when_source_is_not_locally_accessible():
    """远端音频无法直接读取标签时也应按文件名区分曲目，避免整张专辑重名。"""
    meta, info = _music_context()
    meta.artists = ["孙燕姿"]
    meta.title = "完美的一天"
    meta.album = "完美的一天"
    meta.album_artist = "孙燕姿"
    meta.year = 2005
    meta.track_number = None
    meta.total_tracks = None
    info.music_type = "album"
    info.artists = ["孙燕姿"]
    info.title = "完美的一天"
    info.album = "完美的一天"
    info.album_artist = "孙燕姿"
    info.year = 2005
    info.track_number = None
    info.total_tracks = None
    history = SimpleNamespace(
        note={
            "music": {
                "version": 1,
                "meta": meta.to_dict(),
                "media": info.to_dict(),
            }
        }
    )

    restored_meta, restored_info = TransferChain._restore_music_download_context(
        history,
        Path("/remote/10. 明天晴天.m4a"),
    )

    assert restored_meta is not None
    assert restored_info is not None
    assert restored_meta.title == "明天晴天"
    assert restored_meta.track_number == 10
    assert restored_info.title == "明天晴天"
    assert restored_info.track_number == 10
    context = TemplateHelper().builder.build(
        meta=restored_meta,
        mediainfo=restored_info,
        file_extension=".m4a",
        include_raw_objects=False,
    )
    rendered = Template(settings.MUSIC_RENAME_FORMAT).render(context)
    assert rendered == "孙燕姿/完美的一天 (2005)/10 - 明天晴天.m4a"


def test_job_manager_serializes_music_queue_models():
    """整理队列应使用音乐专属 Schema 序列化任务。"""
    meta, info = _music_context()
    task = TransferTask(
        fileitem=FileItem(
            storage="local",
            path="/music/track.flac",
            name="track.flac",
            basename="track",
            type="file",
            extension="flac",
        ),
        meta=meta,
        mediainfo=info,
        mtype=MediaType.MUSIC,
    )
    manager = JobManager()

    assert manager.add_task(task) is True
    job = manager.list_jobs()[0]
    assert job.media.type == "音乐"
    assert job.media.album == "Random Access Memories"
    assert job.tasks[0].meta.type == "音乐"
    assert task.mtype == MediaType.MUSIC


def test_job_manager_separates_unidentified_music_recordings():
    """缺少远端 ID 的不同曲目必须进入不同作业，避免通知和完成状态串组。"""
    manager = JobManager()
    first = MusicInfo(title="Intro", artists=["Artist"], album="Album", track_number=1)
    second = MusicInfo(title="Finale", artists=["Artist"], album="Album", track_number=2)

    assert manager.add_task(_music_task("/music/01 - Intro.flac", first)) is True
    assert manager.add_task(_music_task("/music/02 - Finale.flac", second)) is True

    assert len(manager.list_jobs()) == 2


def test_job_manager_groups_unidentified_album_tracks_by_album_identity():
    """无远端 ID 的整专曲目应按专辑身份聚合成一个作业。"""
    manager = JobManager()
    first = MusicInfo(
        music_type="album",
        title="Intro",
        artists=["Artist"],
        album="Album",
        album_artist="Artist",
        year=2026,
        track_number=1,
    )
    second = MusicInfo(
        music_type="album",
        title="Finale",
        artists=["Artist"],
        album="Album",
        album_artist="Artist",
        year=2026,
        track_number=2,
    )

    assert manager.add_task(_music_task("/music/01 - Intro.flac", first)) is True
    assert manager.add_task(_music_task("/music/02 - Finale.flac", second)) is True

    jobs = manager.list_jobs()
    assert len(jobs) == 1
    assert len(jobs[0].tasks) == 2


def test_job_manager_separates_music_entity_namespaces_for_same_provider_id():
    """数据源 ID 相同但实体类型不同的单曲和专辑不能共享作业。"""
    manager = JobManager()
    recording = MusicInfo(
        media_source="musicbrainz",
        media_id="shared-id",
        music_type="recording",
        title="Track",
        artists=["Artist"],
    )
    album = MusicInfo(
        media_source="musicbrainz",
        media_id="shared-id",
        music_type="album",
        title="Album",
        album="Album",
        artists=["Artist"],
    )

    assert manager.add_task(_music_task("/music/Track.flac", recording)) is True
    assert manager.add_task(_music_task("/music/Album/01.flac", album)) is True

    assert len(manager.list_jobs()) == 2


def test_success_file_aggregation_is_isolated_between_music_jobs_in_same_directory(monkeypatch):
    """同目录内交错完成的整专与单曲作业不能互相取走通知文件清单。"""
    chain = object.__new__(TransferChain)
    chain.jobview = JobManager()
    chain._success_target_files = {}
    chain._scrape_batches = {}
    chain._media_exts = settings.RMT_MEDIAEXT
    chain._audio_exts = settings.RMT_AUDIOEXT
    chain.eventmanager = Mock()
    chain.transfer_completed = Mock()
    chain.send_transfer_message = Mock()

    def transfer_result(**kwargs):
        """执行测试历史暂存并返回 task-aware 原子结算回执。"""
        history = kwargs["stage_history"](SimpleNamespace())
        return TransferSettlementResult(
            history_id=history.id,
            settlement_revision=1,
            pending_deleted=True,
        )

    chain.durable_event_writer = Mock()
    chain.durable_event_writer.transfer_result.side_effect = transfer_result
    album_infos = [
        MusicInfo(
            music_type="album",
            title=title,
            artists=["Artist"],
            album="Album",
            album_artist="Artist",
            year=2026,
            track_number=number,
        )
        for number, title in ((1, "Intro"), (2, "Finale"))
    ]
    album_tasks = [
        _music_task(f"/downloads/{number:02d}.flac", info)
        for number, info in enumerate(album_infos, start=1)
    ]
    recording_task = _music_task(
        "/downloads/Single.flac",
        MusicInfo(title="Single", artists=["Artist"], album="Album"),
    )
    tasks = [album_tasks[0], recording_task, album_tasks[1]]
    for task in tasks:
        task.background = True
        assert chain.jobview.add_task(task) is True

    target_dir = FileItem(storage="local", path="/library/Artist/Album", type="dir")

    def transfer_info(task: TransferTask) -> TransferInfo:
        """为指定任务构造同目录下的成功整理结果。"""
        target = f"/library/Artist/Album/{task.fileitem.name}"
        return TransferInfo(
            success=True,
            fileitem=task.fileitem,
            target_diritem=target_dir,
            target_item=FileItem(storage="local", path=target, type="file"),
            file_list_new=[target],
            transfer_type="copy",
            need_notify=True,
        )

    chain.transfer_history_repository = SimpleNamespace()
    monkeypatch.setattr(
        "app.chain.transfer.settlement.add_transfer_success",
        lambda **kwargs: SimpleNamespace(id=1),
    )

    for sequence, task in enumerate(tasks):
        result = transfer_info(task)
        task.bind_admission_task_id(f"music-terminal-{sequence}")
        task.bind_execution_lease(
            owner_id="music-test-owner",
            lease_token=f"music-lease-{sequence}",
        )
        task.bind_execution_checkpoint(TransferExecutionCheckpoint.create(
            payload={
                "outcome": "succeeded",
                "transferinfo": result.model_dump(mode="json"),
            },
            operation_ids=(f"music-operation-{sequence}",),
        ))
        chain._TransferChain__default_callback(task, result)

    notified_lists = [
        call.kwargs["transferinfo"].file_list_new
        for call in chain.send_transfer_message.call_args_list
    ]
    assert notified_lists == [
        ["/library/Artist/Album/Single.flac"],
        [
            "/library/Artist/Album/01.flac",
            "/library/Artist/Album/02.flac",
        ],
    ]
    assert chain._success_target_files == {}


def test_automatic_audio_transfer_runs_music_recognition(tmp_path, monkeypatch):
    """无下载身份的音频应先走音乐识别，远端失败后再使用本地标签兜底。"""
    audio_path = tmp_path / "周杰伦 - 晴天.flac"
    audio_path.write_bytes(b"fake-flac")
    source_item = FileItem(
        storage="local",
        path=audio_path.as_posix(),
        name=audio_path.name,
        basename=audio_path.stem,
        type="file",
        extension="flac",
        size=audio_path.stat().st_size,
    )
    target_item = FileItem(
        storage="local",
        path=(tmp_path / "library" / audio_path.name).as_posix(),
        name=audio_path.name,
        basename=audio_path.stem,
        type="file",
        extension="flac",
    )
    recognized = MusicInfo(
        media_source="musicbrainz",
        media_id="recording-1",
        title="晴天",
        artists=["周杰伦"],
        album="叶惠美",
        year=2003,
    )
    recognize = Mock(return_value=recognized)
    chain = TransferChain()
    monkeypatch.setattr(MediaChain, "recognize_by_meta", recognize)
    monkeypatch.setattr(
        chain,
        "_TransferChain__get_trans_fileitems",
        Mock(side_effect=lambda *args, **kwargs: [(source_item, False)]),
    )
    monkeypatch.setattr(chain, "_resolve_download_history", Mock(return_value=None))
    monkeypatch.setattr(
        chain,
        "_plan_checkpoint_and_execute",
        Mock(
            return_value=TransferInfo(
                success=True,
                fileitem=source_item,
                target_item=target_item,
            )
        ),
    )

    state, preview = chain.do_transfer(
        fileitem=source_item,
        target_directory=TransferDirectoryConf(
            library_path=(tmp_path / "library").as_posix(),
            library_storage="local",
        ),
        mtype=MediaType.MUSIC,
        force=True,
        preview=True,
    )

    assert state is True
    recognize.assert_called_once()
    assert isinstance(recognize.call_args.args[0], MetaMusic)
    assert recognize.call_args.kwargs["mtype"] == MediaType.MUSIC
    assert preview["items"][0]["type"] == MediaType.MUSIC.value

    recognize.reset_mock()
    recognize.return_value = None
    state, preview = chain.do_transfer(
        fileitem=source_item,
        target_directory=TransferDirectoryConf(
            library_path=(tmp_path / "library").as_posix(),
            library_storage="local",
        ),
        mtype=MediaType.MUSIC,
        force=True,
        preview=True,
    )

    assert state is True
    recognize.assert_called_once()
    assert isinstance(recognize.call_args.args[0], MetaMusic)
    assert recognize.call_args.kwargs["mtype"] == MediaType.MUSIC
    assert preview["items"][0]["type"] == MediaType.MUSIC.value


def test_automatic_multi_track_recording_context_rematches_album(tmp_path, monkeypatch):
    """自动整专不得复用下载时误选的单曲身份，应按目录恢复各音轨身份。"""
    source_dir = tmp_path / "徐良 情话"
    source_dir.mkdir()
    audio_paths = [
        source_dir / "01 - 女骑士.flac",
        source_dir / "02 - 悲伤的李白.flac",
    ]
    for audio_path in audio_paths:
        audio_path.write_bytes(b"fake-flac")
    source_items = [
        FileItem(
            storage="local",
            path=audio_path.as_posix(),
            name=audio_path.name,
            basename=audio_path.stem,
            type="file",
            extension="flac",
            size=audio_path.stat().st_size,
        )
        for audio_path in audio_paths
    ]
    source_item = FileItem(
        storage="local",
        path=source_dir.as_posix(),
        name=source_dir.name,
        type="dir",
    )
    saved_recording = MusicInfo(
        media_source="musicbrainz",
        media_id="wrong-recording",
        music_type="recording",
        title="情话",
        artists=["徐良", "孙羽幽"],
        album="北京巷弄",
        album_artist="徐良",
        year=2013,
        cover_url="https://example.com/wrong-cover.jpg",
    )
    saved_meta = MetaMusic.from_music_info(saved_recording)
    history = DownloadHistorySnapshot(
        id=1,
        path=source_dir.as_posix(),
        type=MediaType.MUSIC.value,
        title="情话",
        note={
            "music": {
                "version": 1,
                "meta": saved_meta.to_dict(),
                "media": saved_recording.to_dict(),
            }
        },
        music_type="recording",
        downloader="qbittorrent",
        download_hash="hash-1",
    )
    file_metas = {
        audio_paths[0]: MetaMusic(
            org_string=audio_paths[0].name,
            title="女骑士",
            artists=["徐良"],
            album="情话",
            album_artist="徐良",
            year=2013,
            track_number=1,
            total_tracks=12,
        ),
        audio_paths[1]: MetaMusic(
            org_string=audio_paths[1].name,
            title="悲伤的李白",
            artists=["徐良"],
            album="情话",
            album_artist="徐良",
            year=2013,
            track_number=2,
            total_tracks=12,
        ),
    }
    matched_tracks = {
        str(path.resolve()): MusicInfo(
            media_source="musicbrainz",
            media_id=f"recording-{index}",
            music_type="recording",
            title=file_metas[path].title,
            artists=["徐良"],
            album="情话",
            album_artist="徐良",
            album_id="correct-release-group",
            year=2013,
            track_number=index,
            total_tracks=12,
            cover_url="https://example.com/correct-cover.jpg",
        )
        for index, path in enumerate(audio_paths, start=1)
    }
    chain = TransferChain()
    monkeypatch.setattr(
        chain,
        "_TransferChain__get_trans_fileitems",
        Mock(return_value=[(item, False) for item in source_items]),
    )
    monkeypatch.setattr(chain, "_resolve_download_history", Mock(return_value=history))
    monkeypatch.setattr(
        MediaChain,
        "read_path_meta",
        Mock(side_effect=lambda path: file_metas[Path(path)]),
    )
    album_match = Mock(return_value=matched_tracks)
    monkeypatch.setattr(MediaChain, "recognize_music_album_directory", album_match)
    captured_tasks = []

    def execute(task, **_kwargs):
        captured_tasks.append(task)
        target_dir = tmp_path / "library" / "徐良" / "情话 (2013)"
        target_item = target_dir / task.fileitem.name
        return TransferInfo(
            success=True,
            fileitem=task.fileitem,
            target_item=FileItem(storage="local", path=target_item.as_posix(), type="file"),
            target_diritem=FileItem(storage="local", path=target_dir.as_posix(), type="dir"),
        )

    monkeypatch.setattr(chain, "_plan_checkpoint_and_execute", execute)

    state, preview = chain.do_transfer(
        fileitem=source_item,
        mediainfo=saved_recording,
        mtype=MediaType.MUSIC,
        target_directory=TransferDirectoryConf(
            library_path=(tmp_path / "library").as_posix(),
            library_storage="local",
        ),
        force=True,
        preview=True,
    )

    assert state is True
    assert preview["summary"] == {"total": 2, "success": 2, "failed": 0}
    assert [task.mediainfo.media_id for task in captured_tasks] == [
        "recording-1",
        "recording-2",
    ]
    assert {task.mediainfo.album_id for task in captured_tasks} == {"correct-release-group"}
    assert {task.mediainfo.cover_url for task in captured_tasks} == {
        "https://example.com/correct-cover.jpg"
    }
    assert album_match.call_count == 2


def test_explicit_music_batch_excludes_video_from_mixed_directory(tmp_path, monkeypatch):
    """明确音乐上下文时只规划音频主文件，混合目录中的视频不得套用音乐身份。"""
    audio_path = tmp_path / "08 - Get Lucky.flac"
    video_path = tmp_path / "Get Lucky.mkv"
    audio_path.write_bytes(b"fake-flac")
    video_path.write_bytes(b"fake-video")
    audio_item = FileItem(
        storage="local",
        path=audio_path.as_posix(),
        name=audio_path.name,
        basename=audio_path.stem,
        type="file",
        extension="flac",
        size=audio_path.stat().st_size,
    )
    video_item = FileItem(
        storage="local",
        path=video_path.as_posix(),
        name=video_path.name,
        basename=video_path.stem,
        type="file",
        extension="mkv",
        size=video_path.stat().st_size,
    )
    source_dir = FileItem(
        storage="local",
        path=tmp_path.as_posix(),
        name=tmp_path.name,
        type="dir",
    )
    target_item = FileItem(
        storage="local",
        path=(tmp_path / "library" / audio_path.name).as_posix(),
        name=audio_path.name,
        basename=audio_path.stem,
        type="file",
        extension="flac",
    )
    recognized = MusicInfo(
        media_source="musicbrainz",
        media_id="recording-1",
        title="Get Lucky",
        artists=["Daft Punk"],
        album="Random Access Memories",
        track_number=8,
    )
    chain = TransferChain()
    monkeypatch.setattr(
        chain,
        "_TransferChain__get_trans_fileitems",
        Mock(return_value=[(audio_item, False), (video_item, False)]),
    )
    monkeypatch.setattr(chain, "_resolve_download_history", Mock(return_value=None))
    monkeypatch.setattr(MediaChain, "recognize_by_meta", Mock(return_value=recognized))
    monkeypatch.setattr(
        chain,
        "_plan_checkpoint_and_execute",
        Mock(
            return_value=TransferInfo(
                success=True,
                fileitem=audio_item,
                target_item=target_item,
            )
        ),
    )

    state, preview = chain.do_transfer(
        fileitem=source_dir,
        target_directory=TransferDirectoryConf(
            library_path=(tmp_path / "library").as_posix(),
            library_storage="local",
        ),
        mtype=MediaType.MUSIC,
        force=True,
        preview=True,
    )

    assert state is True
    assert [item["source"] for item in preview["items"]] == [audio_item.path]
    assert chain._plan_checkpoint_and_execute.call_count == 1


def test_downloader_process_forwards_music_history_type(tmp_path, monkeypatch):
    """下载器自动整理应把下载历史中的音乐类型传入文件规划，且不调用影视补图模块。"""
    audio_path = tmp_path / "晴天.flac"
    audio_path.write_bytes(b"fake-flac")
    recognized = MusicInfo(
        media_source="musicbrainz",
        media_id="recording-1",
        title="晴天",
        artists=["周杰伦"],
    )
    history = SimpleNamespace(
        type=MediaType.MUSIC.value,
        tmdbid=None,
        doubanid=None,
        bangumiid=None,
        anilistid=None,
        media_source="musicbrainz",
        media_id="recording-1",
        music_type="recording",
        episode_group=None,
        media_category=None,
    )
    chain = TransferChain()
    media_chain = Mock()
    media_chain.recognize_media.return_value = recognized
    run_module = Mock()
    monkeypatch.setattr(
        "app.chain.transfer.queue.DirectoryHelper.get_download_dirs",
        lambda _: [
            SimpleNamespace(
                monitor_type="downloader",
                storage="local",
                download_path=tmp_path.as_posix(),
            )
        ],
    )
    chain.download_history_repository = SimpleNamespace(
        get_by_hash=lambda download_hash: history
    )
    monkeypatch.setattr(
        chain,
        "list_torrents",
        Mock(
            return_value=[
                TransferTorrent(
                    downloader="qbittorrent",
                    hash="hash-1",
                    path=audio_path,
                )
            ]
        ),
    )
    monkeypatch.setattr("app.chain.transfer.queue.MediaChain", lambda: media_chain)
    monkeypatch.setattr("app.chain.transfer.filter.MediaChain", lambda: media_chain)
    monkeypatch.setattr(chain, "do_transfer", Mock(return_value=(True, "")))
    monkeypatch.setattr(chain, "run_module", run_module)

    state = chain.process()

    assert state is True
    assert media_chain.recognize_media.call_args.kwargs["mtype"] == MediaType.MUSIC
    assert media_chain.recognize_media.call_args.kwargs["music_type"] == "recording"
    assert chain.do_transfer.call_args.kwargs["mtype"] == MediaType.MUSIC
    assert chain.do_transfer.call_args.kwargs["mediainfo"] is recognized
    run_module.assert_not_called()
