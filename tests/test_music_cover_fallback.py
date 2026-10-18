"""
音乐封面随附加文件整理及在线失败后的本地图片回退回归。
"""

import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from mutagen.flac import FLAC, Picture
from PIL import Image

from app.application.settings.contract import ALL_SETTING_SPECS, build_value_schema
from app.application.transfer.workflow import TransferPlanningInput, TransferTask
from app.chain.media import MediaChain
from app.chain.scraping import ScrapingChain
from app.chain.transfer.request import _TransferCandidatePlanner
from app.domain.context import MusicInfo
from app.domain.meta.metamusic import MetaMusic
from app.domain.music import music_cover_priority
from app.modules.filemanager.storages.local import LocalStorage
from app.modules.filemanager.transhandler import TransHandler
from app.runtime.config import ConfigModel, Settings, settings
from app.schemas.file import FileItem
from app.schemas.system import TransferDirectoryConf
from app.schemas.transfer import TransferInfo
from app.schemas.types import EventType, MediaType
from tests.test_transfer_sync_extra_files import bind_empty_history_repositories, make_fileitem, make_transfer_chain


def _tagged_audio(directory, name="01.flac", album="Album"):
    """
    用真实标签建立可离线整理的音轨和唯一发行证据。
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    shutil.copyfile(Path(__file__).parent / "fixtures/audio/silence.flac", path)
    audio = FLAC(path)
    audio.update(title=[f"Song {path.stem}"], artist=["Artist"], album=[album], albumartist=["Artist"])
    audio.save()
    return path


def _planner(chain, item, sync=True, mtype=MediaType.MUSIC, automatic=False):
    """
    构造真实候选规划器，区分通用附件同步和自动音乐封面同步。
    """
    return _TransferCandidatePlanner(
        chain, meta=None, season=None, formater=None, batch_mtype=mtype, mediainfo=None,
        continue_callback=None, has_episode_format_template=False, transfer_exclude_words=[],
        download_hash=None, sync_extra_files=sync, fileitem=item, sync_music_covers=automatic,
    )


@pytest.mark.parametrize("single", [False, True])
@pytest.mark.parametrize("sync", [False, True])
@pytest.mark.parametrize("mtype", [None, MediaType.MUSIC])
def test_music_cover_enters_directory_and_single_transfer_only_when_sync_enabled(tmp_path, monkeypatch, single, sync, mtype):
    """
    目录与单音轨使用相同附加文件开关，图片继承音轨身份并且只规划一次。
    """
    path = _tagged_audio(tmp_path / "source")
    cover = make_fileitem(str(path.parent / "COVER.JPG"))
    audio = make_fileitem(str(path))
    root = audio if single else FileItem(storage="local", path=str(path.parent), type="dir")
    chain = make_transfer_chain()
    bind_empty_history_repositories(chain)
    storage = Mock()
    storage.get_parent_item.return_value = root
    storage.list_files.return_value = [audio, cover]
    monkeypatch.setattr("app.chain.transfer.request.StorageChain", lambda: storage)
    planner = _planner(chain, root, sync, mtype)
    items, inherited = planner._plan_file_items([(audio, False)] if single else [(cover, False), (audio, False)])

    assert [item.path for item, _ in items] == ([audio.path, cover.path] if sync else [audio.path])
    assert (chain._get_file_key(cover) in inherited) is sync
    if sync:
        context = chain._prepare_music_batch_context(items, mtype)
        assert context.related_main_keys[chain._get_file_key(cover)] == chain._get_file_key(audio)


@pytest.mark.parametrize("automatic", [False, True])
def test_music_cover_does_not_borrow_identity_from_different_albums_or_video(tmp_path, automatic):
    """
    同目录多发行存在歧义时不关联封面，影视批次也不会吸收图片。
    """
    first = make_fileitem(str(_tagged_audio(tmp_path / "source", "01.flac", "First")))
    second = make_fileitem(str(_tagged_audio(tmp_path / "source", "02.flac", "Second")))
    cover = make_fileitem(str(tmp_path / "source/cover.jpg"))
    chain = make_transfer_chain()
    bind_empty_history_repositories(chain)
    context = chain._prepare_music_batch_context([(first, False), (second, False), (cover, False)], MediaType.MUSIC)
    assert chain._get_file_key(cover) not in context.related_main_keys
    assert not _planner(chain, first, mtype=MediaType.TV, automatic=automatic)._is_music_cover_candidate(cover)
    assert _planner(chain, cover, automatic=automatic)._plan_file_items([(cover, False)])[0] == []


@pytest.mark.parametrize("single", [False, True])
@pytest.mark.parametrize("mtype", [None, MediaType.MUSIC])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("manual,sync,selected", [
    (False, False, False), (True, True, False), (True, False, False),
    (False, False, True), (True, False, True),
])
def test_music_cover_reaches_public_transfer_entrypoint(tmp_path, monkeypatch, single, mtype, enabled, manual, sync, selected):
    """
    自动目录与单文件请求默认带入封面，手动请求保留同步选项且均遵守全局开关。
    """
    monkeypatch.setattr(settings, "MUSIC_LOCAL_COVER_ENABLE", enabled)
    audio = _tagged_audio(tmp_path / "source")
    cover = audio.parent / "cover.jpg"
    Image.new("RGB", (2, 2)).save(cover)
    storage = LocalStorage()
    port = SimpleNamespace(
        get_item=lambda item: storage.get_item(Path(item.path)),
        get_parent_item=lambda item: storage.get_item(Path(item.path).parent),
        list_files=lambda item, **_kwargs: [storage.get_item(path) for path in Path(item.path).iterdir()],
        contains_bluray_subdirectories=lambda _items: False,
    )
    monkeypatch.setattr("app.chain.transfer.workflow.StorageChain", lambda: port)
    monkeypatch.setattr("app.chain.transfer.request.StorageChain", lambda: port)
    monkeypatch.setattr("app.chain.transfer.workflow.get_configured_system_config", lambda: SimpleNamespace(get=lambda _key: None))
    chain = make_transfer_chain()
    bind_empty_history_repositories(chain)
    chain.eventmanager = SimpleNamespace(send_event=lambda *_args, **_kwargs: None)
    monkeypatch.setattr(chain, "_finalize_recognition_result", lambda info, **_kwargs: info)
    monkeypatch.setattr(chain, "_TransferChain__put_to_jobview", lambda _task: True)
    monkeypatch.setattr(chain, "_register_scrape_batch_task", lambda _task: None)
    monkeypatch.setattr(chain, "_close_scrape_batch", lambda _batch: None)
    for name in ("recognize_music_album_directory", "recognize_music_by_path", "recognize_by_meta", "recognize_media"):
        monkeypatch.setattr(MediaChain, name, Mock(side_effect=AssertionError("完整标签不得在线识别")))
    tasks = []

    def preview(task, **_kwargs):
        """
        检查完整请求给规划执行器传入的主文件和封面上下文。
        """
        tasks.append(task)
        return TransferInfo(success=True, fileitem=task.fileitem, target_item=task.fileitem)

    monkeypatch.setattr(chain, "_plan_checkpoint_and_execute", preview)
    transfer = chain._run_transfer_workflow if selected else chain.do_transfer
    success, result = transfer(
        fileitem=storage.get_item(audio if single else audio.parent),
        target_directory=TransferDirectoryConf(library_storage="local", library_path=str(tmp_path / "library")),
        mtype=mtype, manual=manual, sync_extra_files=sync, force=True, preview=True,
        **({"selected_fileitems": [storage.get_item(audio)]} if selected else {}),
    )
    assert success, result
    with_cover = enabled and (sync or not manual and not selected)
    assert [task.fileitem.path for task in tasks] == ([str(audio), str(cover)] if with_cover else [str(audio)])
    assert all(task.mediainfo.album == "Album" for task in tasks)
    assert result["summary"] == {"total": 2 if with_cover else 1, "success": 2 if with_cover else 1, "failed": 0}


def test_music_scrape_waits_for_cover_transfer_completion():
    """
    音轨先完成时仍等待封面任务，图片就绪后才发出只含音轨的刮削事件。
    """
    chain = make_transfer_chain()
    chain.eventmanager = Mock()
    info = MusicInfo(title="Song", album="Album", artists=["Artist"])
    audio = TransferTask(fileitem=make_fileitem("/source/01.flac"), mediainfo=info,
                         meta=MetaMusic(title="Song", album="Album"), transfer_batch_id="album")
    cover = audio.model_copy(update={"fileitem": make_fileitem("/source/cover.jpg")})
    for task in (audio, cover):
        chain._register_scrape_batch_task(task)
    chain._record_scrape_target(audio, TransferInfo(
        success=True, need_scrape=True, target_diritem=FileItem(storage="local", path="/library/Album", type="dir"),
        file_list_new=["/library/Album/01.flac"],
    ))
    chain._close_scrape_batch("album")
    chain._finish_scrape_batch_task(audio)
    chain.eventmanager.send_event.assert_not_called()
    chain._finish_scrape_batch_task(cover)
    chain.eventmanager.send_event.assert_called_once()
    event, payload = chain.eventmanager.send_event.call_args.args
    assert event == EventType.MetadataScrape
    assert payload["file_list"] == ["/library/Album/01.flac"]
    assert not chain._scrape_batches


@pytest.mark.parametrize("mode", ["copy", "link", "move"])
@pytest.mark.parametrize("rename", [False, True])
def test_music_cover_real_storage_transfer_preserves_filename_and_album_root(tmp_path, monkeypatch, mode, rename):
    """
    封面沿用真实复制/硬链接/移动执行及覆盖策略，重命名时不进入碟片子目录。
    """
    source = tmp_path / "source"
    source.mkdir()
    path = source / "COVER.JPG"
    Image.new("RGB", (2, 2), "red").save(path)
    content = path.read_bytes()
    storage = LocalStorage()
    item = storage.get_item(path)
    info = MusicInfo(title="Song", album="Album", artists=["Artist"], album_artist="Artist", disc_number=2, total_discs=2)
    meta = MetaMusic(title="Song", album="Album", artists=["Artist"], album_artist="Artist", disc_number=2, total_discs=2)
    planning = TransferPlanningInput(source_fileitem=item.model_dump(mode="json"), target_storage="local",
                                     target_path=str(tmp_path / "library"), requested_transfer_type=mode, need_rename=rename)
    monkeypatch.setattr("app.modules.filemanager.transhandler.eventmanager.send_event", Mock(return_value=None))
    handler = TransHandler()
    checkpoint = handler.plan_transfer(planning, meta=meta, mediainfo=info, source_oper=storage, target_storage="local",
                                       target_path=tmp_path / "library", transfer_type=mode, need_scrape=False,
                                       need_rename=rename, need_notify=False, overwrite_mode="never", episodes_info=None, preview=False)
    target = Path(checkpoint.final_target_path)
    assert target.name == path.name and "Disc 2" not in target.parts
    result = handler.execute_transfer_plan(checkpoint, meta=meta, mediainfo=info, source_oper=storage, target_oper=storage)
    assert result.success and target.read_bytes() == content
    assert path.exists() is (mode != "move")
    if mode == "link":
        assert os.path.samefile(path, target)
    refused = handler._TransHandler__resolve_overwrite(
        fileitem=item, meta=meta, mediainfo=info, target_oper=storage, target_storage="local", target_file=target,
        transfer_type=mode, overwrite_mode="never", need_notify=False,
    )
    assert refused[2].overwrite_skipped


@pytest.mark.parametrize("online", ["missing", "failed", "available"])
@pytest.mark.parametrize("policy", ["missing", "skip", "overwrite"])
@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
def test_music_scrape_uses_online_first_then_valid_local_cover_without_changing_seed(tmp_path, monkeypatch, online, policy, existing, enabled):
    """
    真实 FLAC 验证开关仅控制本地回退，不改变在线刮削、既有封面策略和种子。
    """
    monkeypatch.setattr(settings, "MUSIC_LOCAL_COVER_ENABLE", enabled)
    source = _tagged_audio(tmp_path / "source")
    existing_path = tmp_path / "existing.png"
    Image.new("RGB", (2, 2), "green").save(existing_path)
    existing_bytes = existing_path.read_bytes()
    if existing:
        audio = FLAC(source)
        picture = Picture()
        picture.type, picture.mime, picture.data = 3, "image/png", existing_bytes
        audio.add_picture(picture)
        audio.save()
    target_dir = tmp_path / "library"
    target_dir.mkdir()
    target = target_dir / "01.flac"
    os.link(source, target)
    before = source.read_bytes()
    (target_dir / "cover.jpg").write_bytes(b"invalid image")
    Image.new("RGB", (2, 2), "blue").save(target_dir / "front.png")
    local_bytes = (target_dir / "front.png").read_bytes()
    online_path = tmp_path / "online.png"
    Image.new("RGB", (2, 2), "red").save(online_path)
    online_bytes = online_path.read_bytes()
    info = MusicInfo(title="Song", artists=["Artist"], album="Album", cover_url=None if online == "missing" else "https://example.invalid/cover")
    storage = LocalStorage()
    chain = object.__new__(ScrapingChain)
    chain.storagechain = SimpleNamespace(
        get_parent_item=lambda item: storage.get_item(Path(item.path).parent),
        list_files=lambda item, **_kwargs: [storage.get_item(path) for path in Path(item.path).iterdir()],
        download_file=lambda item, **_kwargs: Path(item.path),
    )
    chain._download_music_cover = Mock(return_value=(online_bytes, "image/png") if online == "available" else (None, "image/jpeg"))
    result = chain._apply_music_file_scrape(
        fileitem=storage.get_item(target), local_path=target, mediainfo=info, write_tags=False, tag_overwrite=False,
        with_cover=policy != "skip", cover_overwrite=policy == "overwrite", cover=None, lyrics_option=None,
        lyrics_overwrite=False, lyrics_chain=None, album_info=None, media_source=None,
    )
    assert result.metadata_success
    assert source.read_bytes() == before
    pictures = FLAC(target).pictures
    if policy == "skip":
        assert [picture.data for picture in pictures] == ([existing_bytes] if existing else [])
        chain._download_music_cover.assert_not_called()
    elif not enabled and online != "available":
        assert [picture.data for picture in pictures] == ([existing_bytes] if existing else [])
    else:
        expected = existing_bytes if existing and policy == "missing" else online_bytes if online == "available" else local_bytes
        assert pictures[0].data == expected


def test_disabled_local_cover_does_not_read_storage(monkeypatch):
    """
    关闭本地封面时不列目录或下载图片，避免触发远端存储访问。
    """
    monkeypatch.setattr(settings, "MUSIC_LOCAL_COVER_ENABLE", False)
    chain = object.__new__(ScrapingChain)
    chain.storagechain = Mock()
    assert chain._load_local_music_cover(make_fileitem("/music/01.flac")) == (None, "image/jpeg")
    assert not chain.storagechain.mock_calls


def test_local_cover_setting_default_and_contract():
    """
    全局开关默认开启并通过设置目录公开可写的音乐布尔配置。
    """
    assert ConfigModel.model_fields["MUSIC_LOCAL_COVER_ENABLE"].default is True
    spec = ALL_SETTING_SPECS["MUSIC_LOCAL_COVER_ENABLE"]
    assert spec.group == "music"
    assert build_value_schema(spec)["type"] == "boolean"


@pytest.mark.parametrize("enabled", [False, True])
def test_local_cover_setting_persists_without_changing_patterns(tmp_path, monkeypatch, enabled):
    """
    开关可保存并重新加载，切换后仍保留用户的封面文件名匹配规则。
    """
    env_path = tmp_path / "app.env"
    env_path.touch()
    monkeypatch.delenv("MUSIC_LOCAL_COVER_ENABLE", raising=False)
    monkeypatch.delenv("MUSIC_COVER_FILE_PATTERNS", raising=False)
    monkeypatch.setattr("app.runtime.config.get_env_path", lambda: env_path)
    config = Settings(_env_file=env_path, MUSIC_LOCAL_COVER_ENABLE=not enabled)
    assert config.update_setting("MUSIC_COVER_FILE_PATTERNS", "Artwork.webp")[0]
    assert config.update_setting("MUSIC_LOCAL_COVER_ENABLE", enabled)[0]
    restored = Settings(_env_file=env_path)
    assert restored.MUSIC_LOCAL_COVER_ENABLE is enabled
    assert restored.MUSIC_COVER_FILE_PATTERNS == "Artwork.webp"


@pytest.mark.parametrize("remote", [False, True])
def test_local_cover_reader_supports_storage_paths_and_album_root(tmp_path, remote):
    """
    碟片音轨从专辑根读取归档封面，远端路径只通过存储下载读取。
    """
    picture_path = tmp_path / "cover.png"
    Image.new("RGB", (2, 2), "blue").save(picture_path)
    storage = "cloud" if remote else "local"
    audio = FileItem(storage=storage, path="/library/Album/Disc 2/01.flac", type="file")
    disc = FileItem(storage=storage, path="/library/Album/Disc 2", type="dir")
    album = FileItem(storage=storage, path="/library/Album", type="dir")
    cover = FileItem(storage=storage, path="/library/Album/cover.png", type="file")
    port = Mock()
    port.get_parent_item.side_effect = [disc, album]
    port.list_files.return_value = [cover]
    port.download_file.return_value = picture_path
    chain = object.__new__(ScrapingChain)
    chain.storagechain = port
    assert chain._load_local_music_cover(audio) == (picture_path.read_bytes(), "image/png")
    port.list_files.assert_called_once_with(album, recursion=False)
    assert port.download_file.call_args.args[0] == cover


def test_music_cover_patterns_are_case_insensitive_and_reject_non_images(monkeypatch):
    """
    通配配置可扩展文件名，但不能把文本或隐藏文件当作音乐封面。
    """
    monkeypatch.setattr(settings, "MUSIC_COVER_FILE_PATTERNS", "Artwork.*,*.PNG")
    chain = make_transfer_chain()
    assert chain._is_music_cover_file(make_fileitem("/music/ARTWORK.JPEG"))
    assert chain._is_music_cover_file(make_fileitem("/music/custom.png"))
    assert not chain._is_music_cover_file(make_fileitem("/music/Artwork.txt"))
    assert music_cover_priority("/music/.cover.png", "*.png") is None
    assert music_cover_priority("/music/cover.jpg", "") is None
