"""资源规范化三个入口共享的路径计划、qB 核验及事务同步回归测试。"""

import copy
import json
from pathlib import PurePosixPath
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.application.download import organization
from app.application.history import DownloadFileWrite, DownloadHistoryWrite
from app.db.adapters.history.download import TransactionalDownloadHistoryRepository
from app.db.base import Base
from app.db.models.downloadhistory import DownloadFiles, DownloadHistory
from app.db.models.outbox import OutboxMessage
from app.domain.context import MusicInfo
from app.schemas.download import DownloadSourceClassificationRequest, DownloadSourcePathRequest


@pytest.fixture
def case(monkeypatch):
    """用内存数据库和 qB 边界模拟完整执行，不替换生产模块或联网。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[DownloadHistory.__table__, DownloadFiles.__table__, OutboxMessage.__table__])
    sessions = sessionmaker(engine)
    repository = TransactionalDownloadHistoryRepository(sync_session=sessions, async_session=Mock())
    root = "/music/original"
    digest = "a" * 40
    history_id = repository.add(DownloadHistoryWrite(
        path=root, type="音乐", title="含情莫莫", downloader="qb", download_hash=digest,
        torrent_name="莫文蔚 - 含情莫莫 2002 FLAC", note={"untouched": "保留"},
    ), (DownloadFileWrite(
        downloader="qb", download_hash=digest, fullpath=root + "/01.flac",
        savepath=root, filepath="01.flac", torrentname="original", state=1,
    ),))
    torrent = NS(hash=digest, downloader="qb", title="莫文蔚 - 含情莫莫 2002 FLAC",
                 save_path="/music", content_path=root, path=root, raw_state="uploading")
    files = [NS(id=0, name="original/01.flac", size=123)]
    chain = Mock()
    chain.download_history_repository = repository
    chain.list_torrents.side_effect = lambda **kwargs: [torrent]
    chain.torrent_files.side_effect = lambda **kwargs: copy.deepcopy(files)

    def rename(_method, **kwargs):
        assert _method == "rename_source_root"
        assert kwargs["old_name"] == PurePosixPath(torrent.content_path).name
        old_name, new_name = kwargs["old_name"], kwargs["new_name"]
        for item in files:
            item.name = new_name + item.name[len(old_name):]
        torrent.content_path = str(PurePosixPath(torrent.save_path) / new_name)
        torrent.path = torrent.content_path
        return True

    def move(**kwargs):
        torrent.save_path = kwargs["save_path"]
        torrent.content_path = str(PurePosixPath(torrent.save_path) / PurePosixPath(files[0].name).parts[0])
        torrent.path = torrent.content_path
        return {"save_path": True}

    chain.run_module.side_effect = rename
    chain.update_torrent.side_effect = move
    media = MusicInfo(music_type="album", album_type="Album", secondary_types=["Compilation"],
                      album="含情莫莫", title="含情莫莫", album_artist="莫文蔚",
                      artists=["莫文蔚"], year="2002", media_source="musicbrainz", media_id="release-id")
    media_chain = Mock()
    media_chain.recognize_by_meta.return_value = media
    media_chain.recognize_media.return_value = media
    directory = NS(storage="local", download_path="/music", download_category_folder=True)
    helper = Mock()
    helper.get_download_dir_by_task_path.return_value = directory
    helper.get_dir.return_value = directory
    monkeypatch.setattr(organization, "DirectoryHelper", lambda: helper)
    monkeypatch.setattr(organization, "_downloader_kind", lambda name: "qbittorrent" if name == "qb" else "transmission")
    monkeypatch.setattr(organization, "validate_download_save_path", lambda value: value)
    monkeypatch.setattr(organization.Path, "exists", lambda _: False)
    request = DownloadSourceClassificationRequest(downloader="qb", type_name="音乐", music_type="album")
    ctx = NS(chain=chain, media_chain=media_chain, request=request, torrent=torrent, files=files,
             media=media, repository=repository, sessions=sessions, history_id=history_id, digest=digest,
             directory=directory)
    ctx.preview = lambda: organization.organize_existing_source(digest, ctx.request, chain, media_chain)

    def execute():
        plan = ctx.preview()
        ctx.request = ctx.request.model_copy(update={
            "execute": True, "expected_current_path": plan["current_save_path"],
            "expected_target_path": plan["target_save_path"],
            "expected_content_path": plan["current_content_path"],
            "expected_root_name": plan["proposed_root_name"],
        })
        return ctx.preview()

    ctx.execute = execute
    ctx.status = lambda: organization.reconcile_source_operation(digest, "qb", chain)
    yield ctx
    engine.dispose()


def test_preview_is_read_only_and_uses_primary_category_artist(case):
    result = case.preview()
    assert result["target_content_path"] == "/music/Album/莫文蔚/含情莫莫 (2002)"
    assert result["secondary_categories"] == ["Compilation"]
    case.chain.run_module.assert_not_called()
    assert case.repository.get_by_task(case.digest, "qb").note == {"untouched": "保留"}


def test_confirmed_execution_verifies_qb_and_atomically_updates_mp(case):
    result = case.execute()
    assert result["state"] == "complete" and result["executed"]
    history = case.repository.get_by_task(case.digest, "qb")
    assert history.path == result["target_content_path"]
    assert history.note["untouched"] == "保留"
    files = case.repository.get_files_by_hash(case.digest)
    assert files[0].fullpath == history.path + "/01.flac"
    assert files[0].filepath == "01.flac" and files[0].savepath == history.path
    assert files[0].torrentname == "original" and files[0].state == 1
    with case.sessions() as session:
        assert session.scalar(select(OutboxMessage)).payload["operation_id"] == result["operation_id"]


def test_same_hash_in_other_downloader_and_unrelated_paths_are_not_updated(case):
    other = case.repository.add(DownloadHistoryWrite(
        path="/music/original", type="音乐", title="另一个", downloader="other", download_hash=case.digest,
    ), (DownloadFileWrite(downloader="other", download_hash=case.digest, fullpath="/music/original/01.flac"),))
    result = case.execute()
    assert result["executed"] and other != case.history_id
    assert case.repository.get_by_task(case.digest, "other").path == "/music/original"
    files = case.repository.get_files_by_hash(case.digest)
    assert next(item for item in files if item.downloader == "other").fullpath == "/music/original/01.flac"


def test_completion_updates_preclassified_history_file_alias(case):
    """历史若已记录分类前缀，仍应随 qB 实际根目录计划精确同步到最终路径。"""
    legacy_root = "/music/Artist Collection/original"
    with case.sessions() as session:
        history = session.get(DownloadHistory, case.history_id)
        history.path = legacy_root
        file_record = session.scalar(select(DownloadFiles).where(
            DownloadFiles.downloader == "qb",
            DownloadFiles.download_hash == case.digest,
        ))
        file_record.fullpath = legacy_root + "/01.flac"
        file_record.savepath = legacy_root
        session.commit()

    result = case.execute()

    assert result["state"] == "complete"
    files = case.repository.get_files_by_hash(case.digest)
    file_record = next(item for item in files if item.downloader == "qb")
    assert file_record.fullpath == result["target_content_path"] + "/01.flac"
    assert file_record.savepath == result["target_content_path"]


def test_compare_exchange_rejects_stale_writer(case):
    snapshot = case.repository.get_by_task(case.digest, "qb")
    case.execute()
    assert not case.repository.save_source_operation(snapshot, {"state": "needs_attention", "id": "stale"})
    assert case.repository.get_by_task(case.digest, "qb").note["source_organization"]["state"] == "complete"


def test_api_acceptance_is_not_completion_and_no_duplicate_rename(case):
    case.chain.run_module.side_effect = None
    case.chain.run_module.return_value = True
    result = case.execute()
    assert result["state"] == "rename_requested" and not result["executed"]
    assert case.repository.get_by_task(case.digest, "qb").path == "/music/original"
    case.status()
    case.chain.run_module.assert_called_once()
    case.chain.update_torrent.assert_not_called()


def test_uncertain_request_never_blindly_retries_or_rolls_back(case):
    case.chain.update_torrent.side_effect = TimeoutError()
    result = case.execute()
    assert result["state"] == "move_requested" and not result["executed"]
    case.status()
    case.chain.update_torrent.assert_called_once()
    case.chain.run_module.assert_called_once()


def test_recovery_after_move_completed_while_browser_closed(case):
    case.chain.update_torrent.side_effect = None
    case.chain.update_torrent.return_value = {"save_path": True}
    result = case.execute()
    assert result["state"] == "move_requested"
    case.torrent.save_path = result["target_save_path"]
    case.torrent.content_path = result["target_content_path"]
    result = case.status()
    assert result["state"] == "complete"
    case.status()
    case.chain.update_torrent.assert_called_once()
    assert case.repository.get_by_task(case.digest, "qb").path == result["target_content_path"]


def test_non_atomic_qb_move_snapshot_waits_instead_of_raising_attention(case):
    """qB 分步更新保存路径与内容路径时属于合法中间态，不能立即误报冲突。"""
    def moving(**kwargs):
        case.torrent.save_path = kwargs["save_path"]
        return {"save_path": True}

    case.chain.update_torrent.side_effect = moving
    result = case.execute()

    assert result["state"] == "move_requested"
    assert "正在收敛" in result["message"]
    case.torrent.content_path = result["target_content_path"]
    case.torrent.path = result["target_content_path"]
    assert case.status()["state"] == "complete"


def test_needs_attention_recovers_from_verified_renamed_stage(case):
    """历史误报 needs_attention 后，确认仍处于已改名阶段即可继续移动且不重复改名。"""
    move = case.chain.update_torrent.side_effect
    case.chain.update_torrent.side_effect = None
    case.chain.update_torrent.return_value = {"save_path": True}
    result = case.execute()
    assert result["state"] == "move_requested"

    history = case.repository.get_by_task(case.digest, "qb")
    operation_state = json.loads(json.dumps(history.note["source_organization"]))
    operation_state["state"] = "needs_attention"
    operation_state["message"] = "旧版误报"
    assert case.repository.save_source_operation(history, operation_state)

    case.chain.update_torrent.side_effect = move
    result = case.status()
    assert result["state"] == "complete"
    case.chain.run_module.assert_called_once()


def test_moving_state_never_marks_complete(case):
    move = case.chain.update_torrent.side_effect
    def moving(**kwargs):
        result = move(**kwargs)
        case.torrent.raw_state = "moving"
        return result
    case.chain.update_torrent.side_effect = moving
    result = case.execute()
    assert not result["executed"]
    case.torrent.raw_state = "uploading"
    assert case.status()["executed"]


def test_changed_internal_file_is_not_silently_accepted(case):
    case.chain.run_module.side_effect = None
    case.chain.run_module.return_value = True
    case.execute()
    case.files[0].name = "unrelated/changed.flac"
    assert case.status()["state"] == "needs_attention"
    case.chain.update_torrent.assert_not_called()


def test_execution_requires_unchanged_preview(case):
    case.request.execute = True
    with pytest.raises(ValueError, match="重新预览"):
        case.preview()
    case.chain.run_module.assert_not_called()


def test_keep_mode_changes_only_root(case):
    case.request.mode = "keep"
    result = case.execute()
    assert result["target_content_path"] == "/music/莫文蔚 - 含情莫莫 (2002)"
    case.chain.update_torrent.assert_not_called()


def test_manual_destination_without_recognition(case):
    case.request = DownloadSourceClassificationRequest(
        downloader="qb", mode="manual", target_path="/music/Chosen", smart_rename=False,
    )
    result = case.execute()
    assert result["target_content_path"] == "/music/Chosen/original"
    case.media_chain.recognize_by_meta.assert_not_called()


@pytest.mark.parametrize("entity,category", [("album", "Album"), ("album", "Single"), ("album", "EP"), ("artist", "Artist Collection")])
def test_auto_download_and_history_share_plan(case, entity, category):
    case.media.music_type = entity
    case.media.album_type = category
    history = case.repository.get_by_task(case.digest, "qb")
    with case.sessions() as session:
        record = session.get(DownloadHistory, history.id)
        record.music_type = entity
        session.commit()
    case.request.music_type = entity
    preview = case.preview()
    result = organization.normalize_added_source(case.digest, "qb", case.chain, case.media_chain)
    assert result["target_content_path"] == preview["target_content_path"]
    assert result["target_save_path"] == f"/music/{category}/莫文蔚"


def test_disabled_source_category_does_not_move_auto_download(case):
    case.directory.download_category_folder = False
    result = organization.normalize_added_source(case.digest, "qb", case.chain, case.media_chain)
    assert result["target_save_path"] == "/music"
    case.chain.update_torrent.assert_not_called()


def test_file_management_delegates_to_same_plan(case):
    request = DownloadSourcePathRequest(source_path="/music/original", type_name="音乐", music_type="album")
    assert organization.organize_source_path(request, case.chain, case.media_chain) == case.preview()


@pytest.mark.parametrize("path", ["/music", "/music/original/01.flac", "/music/unrelated"])
def test_file_management_rejects_parent_child_or_untracked_path(case, path):
    with pytest.raises(ValueError, match="未唯一对应"):
        organization.organize_source_path(DownloadSourcePathRequest(source_path=path), case.chain, case.media_chain)


@pytest.mark.parametrize("names", [["one/a.flac", "two/b.flac"], ["a.flac", "b.flac"], ["../bad.flac"], ["/bad.flac"], []])
def test_multi_root_and_unsafe_lists_are_rejected(case, names):
    case.files[:] = [NS(id=i, name=name, size=100) for i, name in enumerate(names)]
    with pytest.raises(ValueError, match="单一根目录"):
        case.preview()
    case.chain.run_module.assert_not_called()


def test_dotted_folder_is_not_guessed_as_single_file(case):
    case.torrent.content_path = "/music/album.flac"
    case.files[0].name = "album.flac/01.flac"
    assert case.preview()["rename_kind"] == "folder"


def test_single_file_keeps_extension_and_uses_track_name(case):
    case.media.music_type = "recording"
    case.media.title = "我的地盘"
    case.media.album_type = ""
    case.request.music_type = "recording"
    case.torrent.content_path = "/music/song.dsf"
    case.files[0].name = "song.dsf"
    result = case.preview()
    assert result["rename_kind"] == "file"
    assert result["target_content_path"] == "/music/Single/莫文蔚/我的地盘.dsf"


def test_unknown_category_does_not_invent_album(case):
    case.media.album_type = ""
    with pytest.raises(ValueError, match="类别"):
        case.preview()
    case.request.mode = "keep"
    assert case.preview()["proposed_root_name"] == "莫文蔚 - 含情莫莫 (2002)"


def test_recognition_failure_has_no_side_effect(case):
    case.media_chain.recognize_by_meta.return_value = None
    with pytest.raises(ValueError, match="无法识别"):
        case.preview()
    case.chain.run_module.assert_not_called()


def test_destination_collision_never_overwrites(case, monkeypatch):
    monkeypatch.setattr(organization.Path, "exists", lambda _: True)
    with pytest.raises(ValueError, match="目标名称已存在"):
        case.preview()


def test_shared_task_directory_blocks_normalization(case):
    other = copy.copy(case.torrent)
    other.hash = "b" * 40
    case.chain.list_torrents.side_effect = lambda **kwargs: [case.torrent] if kwargs.get("hashs") else [case.torrent, other]
    with pytest.raises(ValueError, match="重叠"):
        case.preview()


def test_music_defaults_to_mb_and_does_not_reuse_other_provider_id(case):
    case.preview()
    assert case.media_chain.recognize_by_meta.call_args.kwargs["media_source"].value == "musicbrainz"
    assert case.media_chain.recognize_by_meta.call_args.kwargs["music_type"] == "album"


def test_schema_validates_manual_path_and_source_identity():
    with pytest.raises(ValueError):
        DownloadSourceClassificationRequest(mode="manual")
    with pytest.raises(ValueError):
        DownloadSourceClassificationRequest(media_id="id-without-source")


def test_windows_paths_remain_absolute():
    assert organization._local_path("D:/Musics/Album", label="测试").as_posix() == "D:/Musics/Album"
    with pytest.raises(ValueError):
        organization._local_path("D:/Musics/../other", label="测试")


def test_qb_module_uses_native_api_and_is_not_plugin_overridable(monkeypatch):
    import sys
    from types import ModuleType

    qbittorrentapi = sys.modules.setdefault("qbittorrentapi", ModuleType("qbittorrentapi"))
    monkeypatch.setattr(qbittorrentapi, "TorrentDictionary", dict, raising=False)
    monkeypatch.setattr(qbittorrentapi, "TorrentFilesList", list, raising=False)
    qbittorrentapi_client = sys.modules.setdefault(
        "qbittorrentapi.client", ModuleType("qbittorrentapi.client")
    )
    monkeypatch.setattr(qbittorrentapi_client, "Client", object, raising=False)
    qbittorrentapi_transfer = sys.modules.setdefault(
        "qbittorrentapi.transfer", ModuleType("qbittorrentapi.transfer")
    )
    monkeypatch.setattr(
        qbittorrentapi_transfer, "TransferInfoDictionary", dict, raising=False
    )

    from app.modules.qbittorrent import QbittorrentModule
    from app.runtime.extensions.module.contracts import get_module_method_contract

    module = object.__new__(QbittorrentModule)
    client = Mock()
    monkeypatch.setattr(module, "get_instance", lambda _name: NS(qbc=client))
    assert module.rename_source_root(downloader="qb", hash_string="a" * 40, old_name="old", new_name="new", kind="folder")
    client.torrents_rename_folder.assert_called_once_with(torrent_hash="a" * 40, old_path="old", new_path="new")
    assert not get_module_method_contract("rename_source_root").public_to_plugins
    with pytest.raises(ValueError):
        module.rename_source_root(downloader="qb", hash_string="a" * 40, old_name="old", new_name="../unsafe", kind="file")


def test_download_option_is_persisted_in_replay_payload():
    from pathlib import Path

    from app.application.chain.events import restore_download_processing, snapshot_download_processing
    from app.domain.context import Context, MediaInfo, TorrentInfo
    from app.domain.metainfo import MetaInfo

    context = Context(meta_info=MetaInfo("movie"), media_info=MediaInfo(title="movie"), torrent_info=TorrentInfo(title="movie"))
    payload = snapshot_download_processing(context=context, download_dir=Path("/music"), torrent_content="magnet:test",
                                          downloader="qb", download_hash="a" * 40, normalize_source=True)
    assert restore_download_processing(payload).normalize_source is True
    del payload["normalize_source"]
    assert restore_download_processing(payload).normalize_source is False


def test_qb_move_transient_path_pair_does_not_become_failure(case):
    case.chain.update_torrent.side_effect = None
    case.chain.update_torrent.return_value = {"save_path": True}
    result = case.execute()
    case.torrent.save_path = result["target_save_path"]
    case.torrent.raw_state = "moving"
    assert case.status()["state"] == "move_requested"
    case.torrent.content_path = result["target_content_path"]
    case.torrent.raw_state = "uploading"
    assert case.status()["state"] == "complete"


def test_music_display_category_cannot_override_primary_type(case):
    case.request.media_category = "Album/Compilation"
    with pytest.raises(ValueError, match="主类别"):
        case.preview()


def test_resource_setting_is_independent_of_library_renaming():
    from app.schemas.system import TransferDirectoryConf

    config = TransferDirectoryConf(renaming=True)
    assert config.source_normalization is False
    config.source_normalization = True
    config.renaming = False
    assert config.model_dump()["source_normalization"] is True


@pytest.mark.parametrize("configured,override,expected", [
    (True, None, True), (False, None, False), (True, False, False), (False, True, True),
])
def test_download_normalization_inherits_directory_unless_explicit(monkeypatch, configured, override, expected):
    from pathlib import Path

    import app.chain.download.submission as submission

    owner = Mock()
    owner._prepare_download_single.return_value = (NS(media=Mock(), download_dir=Path("/music")), None)
    owner._submit_prepared_download.return_value = ("a" * 40, None)
    directory = Mock()
    directory.get_download_dir_by_task_path.return_value = NS(source_normalization=configured)
    monkeypatch.setattr(submission, "DirectoryHelper", lambda: directory)
    result = submission.DownloadSubmissionOwner._execute_download_single(
        owner, context=Mock(), normalize_source=override,
    )
    assert result == "a" * 40
    assert owner._submit_prepared_download.call_args.kwargs["normalize_source"] is expected


def test_normalization_without_durable_worker_does_not_add_torrent():
    from pathlib import Path

    from app.chain.download.submission import DownloadSubmissionOwner

    owner = Mock(durable_event_writer=None)
    owner._prepare_download_single.return_value = (NS(media=Mock(), download_dir=Path("/music")), None)
    result, error = DownloadSubmissionOwner._execute_download_single(
        owner, context=Mock(), normalize_source=True, return_detail=True,
    )
    assert result is None and "未添加下载任务" in error
    owner._submit_prepared_download.assert_not_called()
