"""
验证专辑刮削对曲目标签的覆盖策略、对位保护和结果提示。
"""

import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mutagen.flac import FLAC

from app.api.dependencies.auth import get_current_active_user
from app.api.endpoints import media as media_endpoint
from app.application.audio import AudioMetadataHelper, use_music_cue
from app.chain.scraping import ScrapingChain, ScrapingConfig
from app.domain.context import MusicAlbumInfo, MusicInfo
from app.schemas.file import FileItem
from app.schemas.types import MediaSource

_GROUP = "25aa1b18-5e85-43c2-94c4-4b07ad96268a"
_RECORDINGS = (
    "38035858-f990-4fbb-b3b2-f2f8b958eeba",
    "f4f1b7b2-22b0-4ce0-9ac6-31c336c52812",
)
_OTHER_RECORDING = "70b71113-b078-4f07-9d24-374482016a41"


def _album() -> MusicAlbumInfo:
    """
    构造包含两首不同曲目和独立录音身份的专辑。
    """
    return MusicAlbumInfo(
        media_source=MediaSource.MusicBrainz,
        media_id=_GROUP,
        musicbrainz_release_group_id=_GROUP,
        title="正确专辑",
        artists=["专辑歌手"],
        total_discs=1,
        tracks=[
            MusicInfo(
                media_source=MediaSource.MusicBrainz,
                media_id=recording_id,
                album_id=_GROUP,
                musicbrainz_release_group_id=_GROUP,
                title=title,
                artists=[artist],
                disc_number=1,
                track_number=number,
                total_tracks=2,
            )
            for number, (recording_id, title, artist) in enumerate(
                zip(_RECORDINGS, ("正确一", "正确二"), ("线上歌手一", "线上歌手二")),
                start=1,
            )
        ],
    )


def _audio(path: Path, title: str, artist: str, track: int, recording_id: str = "") -> Path:
    """
    创建真实 FLAC 文件并写入待修正的本地标签。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(__file__).parent / "fixtures/audio/silence.flac", path)
    tags = FLAC(path)
    tags["title"] = [title]
    tags["artist"] = [artist]
    tags["album"] = ["旧专辑"]
    tags["tracknumber"] = [str(track)]
    tags["discnumber"] = ["1"]
    if recording_id:
        tags["musicbrainz_trackid"] = [recording_id]
    tags.save()
    return path


def _item(path: Path, storage: str = "local") -> FileItem:
    """
    将测试音频路径转换为刮削链使用的文件项。
    """
    return FileItem(storage=storage, path=path.as_posix(), type="file", name=path.name)


def _chain(monkeypatch: pytest.MonkeyPatch, album: MusicAlbumInfo, policy: str) -> ScrapingChain:
    """
    构造隔离的刮削链，专辑详情使用离线替身，封面和歌词关闭。
    """
    chain = object.__new__(ScrapingChain)
    chain.storagechain = Mock()
    chain.storagechain.download_file.side_effect = lambda item, **_kwargs: Path(item.path)
    chain.scraping_policies = ScrapingConfig({
        "music_nfo": policy,
        "music_poster": "skip",
        "music_lyrics": "skip",
    })
    media_chain = Mock()
    media_chain.get_music_album.return_value = album
    monkeypatch.setattr("app.chain.scraping.MediaChain", lambda: media_chain)
    return chain


def _scrape(chain: ScrapingChain, album: MusicAlbumInfo, files: list[FileItem], *, overwrite: bool = True):
    """
    按指定专辑刮削测试文件，保持文件顺序由调用方决定。
    """
    return chain.scrape_music_metadata(
        fileitem=FileItem(storage=files[0].storage, path="/music/album", type="dir"),
        mediainfo=album.to_music_info(),
        audio_files=files,
        overwrite=overwrite,
    )


def _remote_storage(chain: ScrapingChain, audio: Path, cue: Optional[Path] = None):
    """
    用临时文件模拟远端目录、逐次下载和上传，不访问真实存储服务。
    """
    audio_item = _item(Path("/music/album") / audio.name, "u115")
    cue_item = _item(Path("/music/album") / cue.name, "u115") if cue else None
    contents = {audio_item.path: audio}
    if cue_item:
        contents[cue_item.path] = cue
    storage = Mock()
    storage.get_parent_item.return_value = FileItem(storage="u115", path="/music/album", type="dir")
    storage.list_files.return_value = [audio_item, *([cue_item] if cue_item else [])]

    def download(item, path=None):
        """
        把远端测试文件复制到每次调用自己的临时目录。
        """
        target = path / item.name
        shutil.copyfile(contents[item.path], target)
        return target

    def upload(_parent, path, new_name=None):
        """
        模拟上传更新后的音频，不修改同目录 CUE。
        """
        assert new_name == audio.name
        shutil.copyfile(path, audio)
        return audio_item

    storage.download_file.side_effect = download
    storage.upload_file.side_effect = upload
    chain.storagechain = storage
    return audio_item, cue_item, storage


def test_missing_only_preserves_track_tags_and_explains_how_to_overwrite(tmp_path, monkeypatch):
    """
    手动刮削也保留仅缺失策略下的本地曲目字段，并告知可切换音乐标签策略。
    """
    album = _album()
    path = _audio(tmp_path / "01.flac", "旧拼接标题", "本地歌手", 1)
    chain = _chain(monkeypatch, album, "missingOnly")

    success, message = _scrape(chain, album, [_item(path)])

    result = AudioMetadataHelper.read_tags(path)
    assert success
    assert (result.title, result.artists, result.track_number) == ("旧拼接标题", ["本地歌手"], 1)
    assert result.album == "正确专辑"
    assert "保留" in message and "曲名" in message and "艺人" in message and "曲序" in message
    assert "音乐标签" in message and "覆盖" in message


def test_overwrite_aligns_each_track_and_preserves_linked_source(tmp_path, monkeypatch):
    """
    覆盖策略按整批唯一证据纠正曲名、艺人和错序，同时不修改硬链接源文件。
    """
    album = _album()
    seeds = [
        _audio(tmp_path / "seed" / "first.flac", "旧拼接标题", "本地一", 1),
        _audio(tmp_path / "seed" / "second.flac", "正确二", "本地二", 1),
    ]
    library = tmp_path / "library"
    library.mkdir()
    files = [library / f"{number}.flac" for number in (1, 2)]
    for seed, target in zip(seeds, files):
        os.link(seed, target)
    before = [seed.read_bytes() for seed in seeds]
    chain = _chain(monkeypatch, album, "overwrite")

    success, message = _scrape(chain, album, [_item(path) for path in files])

    assert success
    assert "匹配 2" in message
    for index, (seed, target) in enumerate(zip(seeds, files)):
        result = AudioMetadataHelper.read_tags(target)
        track = album.tracks[index]
        assert (result.title, result.artists, result.track_number) == (track.title, track.artists, track.track_number)
        assert result.album == "正确专辑"
        assert result.media_id is None
        assert seed.read_bytes() == before[index]
        assert not os.path.samefile(seed, target)


def test_overwrite_preserves_duplicate_and_ambiguous_local_tracks(tmp_path, monkeypatch):
    """
    两个文件争用相同曲目时不得按遍历顺序决定谁被覆盖。
    """
    album = _album()
    paths = [_audio(tmp_path / f"{number}.flac", "未知旧标题", "本地歌手", 1) for number in (1, 2)]
    chain = _chain(monkeypatch, album, "overwrite")

    success, message = _scrape(chain, album, [_item(path) for path in paths])

    assert success
    assert "未可靠匹配" in message and "保留" in message
    assert all(AudioMetadataHelper.read_tags(path).title == "未知旧标题" for path in paths)
    assert all(AudioMetadataHelper.read_tags(path).track_number == 1 for path in paths)


def test_overwrite_preserves_conflicting_recording_identity(tmp_path, monkeypatch):
    """
    不同录音 ID 不能仅凭相同曲序被改成另一首歌，也不能污染原身份。
    """
    album = _album()
    path = _audio(tmp_path / "01.flac", "旧标题", "本地歌手", 1, _OTHER_RECORDING)
    chain = _chain(monkeypatch, album, "overwrite")

    success, message = _scrape(chain, album, [_item(path)])

    result = AudioMetadataHelper.read_tags(path)
    assert success
    assert "未可靠匹配" in message and "保留" in message
    assert (result.title, result.artists, result.media_id) == ("旧标题", ["本地歌手"], _OTHER_RECORDING)


def test_overwrite_preserves_track_on_duration_conflict(tmp_path, monkeypatch):
    """
    本地时长与线上曲目显著冲突时，即使曲序相同也不覆盖标题。
    """
    album = _album()
    album.tracks[0].duration = 300
    path = _audio(tmp_path / "01.flac", "旧标题", "本地歌手", 1)
    chain = _chain(monkeypatch, album, "overwrite")

    success, message = _scrape(chain, album, [_item(path)])

    assert success
    assert "未可靠匹配" in message
    assert AudioMetadataHelper.read_tags(path).title == "旧标题"


def test_automatic_overwrite_does_not_guess_from_position_alone(tmp_path, monkeypatch):
    """
    自动刮削即使启用覆盖，也不能把手动选择专辑时的曲名纠正放宽到普通自动识别。
    """
    album = _album()
    path = _audio(tmp_path / "01.flac", "本地不同曲名", "本地歌手", 1)
    chain = _chain(monkeypatch, album, "overwrite")

    success, message = _scrape(chain, album, [_item(path)], overwrite=False)

    assert success
    assert "未可靠匹配" in message
    assert AudioMetadataHelper.read_tags(path).title == "本地不同曲名"


def test_automatic_overwrite_corrects_position_with_strict_title_evidence(tmp_path, monkeypatch):
    """
    自动刮削在曲名和艺人有严格证据时，覆盖策略仍可修正错标曲序。
    """
    album = _album()
    path = _audio(tmp_path / "01.flac", "正确一", "线上歌手一", 2)
    chain = _chain(monkeypatch, album, "overwrite")

    success, message = _scrape(chain, album, [_item(path)], overwrite=False)

    assert success and "匹配 1" in message
    assert AudioMetadataHelper.read_tags(path).track_number == 1


def test_partial_track_matches_report_both_updated_and_preserved_files(tmp_path, monkeypatch):
    """
    一个曲目成功对位、另一个录音身份冲突时，应分别报告匹配与保留数量。
    """
    album = _album()
    first = _audio(tmp_path / "01.flac", "旧一", "本地一", 1)
    second = _audio(tmp_path / "02.flac", "旧二", "本地二", 2, _OTHER_RECORDING)
    chain = _chain(monkeypatch, album, "overwrite")

    success, message = _scrape(chain, album, [_item(first), _item(second)])

    assert success and "曲目匹配 1 个" in message and "1 个文件未可靠匹配" in message
    assert AudioMetadataHelper.read_tags(first).title == "正确一"
    assert AudioMetadataHelper.read_tags(second).title == "旧二"


def test_matched_track_with_missing_fields_keeps_local_values(tmp_path, monkeypatch):
    """
    线上曲目缺少标题或艺人时不能用空值清除本地标签。
    """
    album = _album()
    album.tracks[0].title = None
    album.tracks[0].artists = []
    path = _audio(tmp_path / "01.flac", "本地标题", "本地歌手", 1)
    chain = _chain(monkeypatch, album, "overwrite")

    success, message = _scrape(chain, album, [_item(path)])

    result = AudioMetadataHelper.read_tags(path)
    assert success and "匹配 1" in message
    assert (result.title, result.artists) == ("本地标题", ["本地歌手"])


def test_overwrite_rechecks_metadata_before_writing(tmp_path, monkeypatch):
    """
    对位后用户修改了音轨证据时，不能按过期快照覆盖新标签。
    """
    album = _album()
    path = _audio(tmp_path / "01.flac", "旧标题", "本地歌手", 1)
    chain = _chain(monkeypatch, album, "overwrite")
    downloads = []

    def download(item, **_kwargs):
        """
        在计划读取后模拟用户修改标签，再交还同一个本地文件。
        """
        downloads.append(item.path)
        if len(downloads) == 2:
            tags = FLAC(path)
            tags["title"] = ["用户新标题"]
            tags.save()
        return path

    chain.storagechain.download_file.side_effect = download
    success, message = _scrape(chain, album, [_item(path)])

    assert success
    assert "未可靠匹配" in message
    assert AudioMetadataHelper.read_tags(path).title == "用户新标题"


def test_incomplete_batch_evidence_cannot_authorize_other_track_overwrites(tmp_path, monkeypatch):
    """
    一个文件无法读取时，剩余文件不能声称已在整个批次中唯一对位。
    """
    album = _album()
    paths = [_audio(tmp_path / f"{number}.flac", "旧标题", "本地歌手", 1) for number in (1, 2)]
    chain = _chain(monkeypatch, album, "overwrite")
    calls = []

    def download(item, **_kwargs):
        """
        仅让计划阶段的第二个文件读取失败，后续写入阶段仍可访问。
        """
        calls.append(item.path)
        return None if len(calls) == 2 else Path(item.path)

    chain.storagechain.download_file.side_effect = download
    success, message = _scrape(chain, album, [_item(path) for path in paths])

    assert success and "未可靠匹配" in message
    assert all(AudioMetadataHelper.read_tags(path).title == "旧标题" for path in paths)


def test_overwrite_applies_simplified_track_text_supplied_by_source(tmp_path, monkeypatch):
    """
    专辑曲目已按系统设置转为简体时，覆盖应实际写入曲名和艺人。
    """
    album = _album()
    album.tracks[0].title = "后来"
    album.tracks[0].artists = ["优里"]
    path = _audio(tmp_path / "01.flac", "後來", "優里", 1)
    chain = _chain(monkeypatch, album, "overwrite")

    success, message = _scrape(chain, album, [_item(path)])

    result = AudioMetadataHelper.read_tags(path)
    assert success and "匹配 1" in message
    assert (result.title, result.artists) == ("后来", ["优里"])


def test_overwrite_preserves_track_if_album_details_are_unavailable(tmp_path, monkeypatch):
    """
    专辑详情无曲目时仍可刮削专辑字段，但必须报告保留本地曲目。
    """
    album = _album()
    path = _audio(tmp_path / "01.flac", "本地标题", "本地歌手", 1)
    chain = _chain(monkeypatch, album, "overwrite")
    provider = Mock()
    provider.get_music_album.return_value = None
    monkeypatch.setattr("app.chain.scraping.MediaChain", lambda: provider)

    success, message = _scrape(chain, album, [_item(path)])

    result = AudioMetadataHelper.read_tags(path)
    assert success and "未可靠匹配" in message and "保留" in message
    assert (result.title, result.artists, result.album) == ("本地标题", ["本地歌手"], "正确专辑")


def test_overwrite_rejects_conflicting_isrc(tmp_path, monkeypatch):
    """
    两个有效 ISRC 明确不同时，不能用匹配曲序覆盖另一录音的标签。
    """
    album = _album()
    album.tracks[0].isrc = "USQX91300105"
    path = _audio(tmp_path / "01.flac", "本地标题", "本地歌手", 1)
    tags = FLAC(path)
    tags["isrc"] = ["USRC17607839"]
    tags.save()
    chain = _chain(monkeypatch, album, "overwrite")

    success, message = _scrape(chain, album, [_item(path)])

    result = AudioMetadataHelper.read_tags(path)
    assert success and "未可靠匹配" in message
    assert (result.title, result.isrc) == ("本地标题", "USRC17607839")


def test_overwrite_preserves_conflicting_release_identity(tmp_path, monkeypatch):
    """
    已知具体发行版本冲突时不覆写曲目，以免标签与保留的发行 ID 自相矛盾。
    """
    album = _album()
    album.musicbrainz_release_id = _RECORDINGS[1]
    for track in album.tracks:
        track.musicbrainz_release_id = album.musicbrainz_release_id
    path = _audio(tmp_path / "01.flac", "本地标题", "本地歌手", 1)
    tags = FLAC(path)
    tags["musicbrainz_albumid"] = [_OTHER_RECORDING]
    tags.save()
    chain = _chain(monkeypatch, album, "overwrite")

    success, message = _scrape(chain, album, [_item(path)])

    result = AudioMetadataHelper.read_tags(path)
    assert success and "未可靠匹配" in message
    assert (result.title, result.musicbrainz_release_id) == ("本地标题", _OTHER_RECORDING)


def test_overwrite_requests_selected_release_and_rejects_other_release(tmp_path, monkeypatch):
    """
    已选择具体发行时必须传递该 ID，来源返回另一版本时不能套用其曲目。
    """
    album = _album()
    requested = album.to_music_info()
    requested.musicbrainz_release_id = _OTHER_RECORDING.upper()
    album.musicbrainz_release_id = _RECORDINGS[0]
    path = _audio(tmp_path / "01.flac", "本地标题", "本地歌手", 1)
    chain = _chain(monkeypatch, album, "overwrite")
    provider = Mock()
    provider.get_music_album.return_value = album
    monkeypatch.setattr("app.chain.scraping.MediaChain", lambda: provider)

    success, message = chain.scrape_music_metadata(
        fileitem=_item(path), mediainfo=requested, overwrite=True,
    )

    assert success and "未可靠匹配" in message
    assert AudioMetadataHelper.read_tags(path).title == "本地标题"
    provider.get_music_album.assert_called_once_with(
        media_source=MediaSource.MusicBrainz, media_id=_GROUP, musicbrainz_release_id=_OTHER_RECORDING,
    )


def test_overwrite_uses_confirmed_selected_release(tmp_path, monkeypatch):
    """
    已确认的具体发行版本按规范化 ID 获取一次并用于曲目覆盖。
    """
    album = _album()
    album.musicbrainz_release_id = _OTHER_RECORDING
    for track in album.tracks:
        track.musicbrainz_release_id = _OTHER_RECORDING
    requested = album.to_music_info()
    requested.musicbrainz_release_id = _OTHER_RECORDING.upper()
    path = _audio(tmp_path / "01.flac", "本地标题", "本地歌手", 1)
    chain = _chain(monkeypatch, album, "overwrite")
    provider = Mock()
    provider.get_music_album.return_value = album
    monkeypatch.setattr("app.chain.scraping.MediaChain", lambda: provider)

    success, message = chain.scrape_music_metadata(fileitem=_item(path), mediainfo=requested, overwrite=True)

    assert success and "匹配 1" in message
    assert AudioMetadataHelper.read_tags(path).title == "正确一"
    provider.get_music_album.assert_called_once_with(
        media_source=MediaSource.MusicBrainz, media_id=_GROUP, musicbrainz_release_id=_OTHER_RECORDING,
    )


def test_overwrite_does_not_guess_between_discs_without_disc_number(tmp_path, monkeypatch):
    """
    多碟发行中同一曲序重复且本地缺少碟号时不能选第一碟。
    """
    album = _album()
    album.total_discs = 2
    album.tracks[1].disc_number = 2
    album.tracks[1].track_number = 1
    path = _audio(tmp_path / "01.flac", "本地标题", "本地歌手", 1)
    tags = FLAC(path)
    del tags["discnumber"]
    tags.save()
    chain = _chain(monkeypatch, album, "overwrite")

    success, message = _scrape(chain, album, [_item(path)])

    assert success and "未可靠匹配" in message
    assert AudioMetadataHelper.read_tags(path).title == "本地标题"


def test_overwrite_keeps_image_cue_as_album_instead_of_one_track(tmp_path, monkeypatch):
    """
    整轨 CUE 是包含多首歌的物理文件，不能将任意单曲标题写入该文件。
    """
    album = _album()
    path = _audio(tmp_path / "image.flac", "本地整轨", "本地歌手", 1)
    cue = tmp_path / "album.cue"
    cue.write_text(
        'PERFORMER "本地歌手"\nTITLE "整轨专辑"\nFILE "image.flac" WAVE\n'
        '  TRACK 01 AUDIO\n    TITLE "第一首"\n    INDEX 01 00:00:00\n'
        '  TRACK 02 AUDIO\n    TITLE "第二首"\n    INDEX 01 00:01:00\n',
        encoding="utf-8",
    )
    before = cue.read_bytes()
    chain = _chain(monkeypatch, album, "overwrite")

    with use_music_cue(True):
        assert AudioMetadataHelper.read(path).music_layout == "image_cue"
        success, message = _scrape(chain, album, [_item(path)])

    result = AudioMetadataHelper.read_tags(path)
    assert success and "未可靠匹配" in message
    assert (result.title, result.artists, result.track_number) == ("本地整轨", ["本地歌手"], 1)
    assert result.media_id is None and cue.read_bytes() == before


def test_remote_overwrite_rechecks_and_uploads_real_audio(tmp_path, monkeypatch):
    """
    远端音轨使用隔离副本两次确认后上传，匹配结果和实际标签一致。
    """
    album = _album()
    path = _audio(tmp_path / "remote" / "01.flac", "旧标题", "本地歌手", 1)
    chain = _chain(monkeypatch, album, "overwrite")
    audio_item, _, storage = _remote_storage(chain, path)

    with use_music_cue(True):
        success, message = _scrape(chain, album, [audio_item])

    result = AudioMetadataHelper.read_tags(path)
    assert success and "匹配 1" in message
    assert (result.title, result.artists) == ("正确一", ["线上歌手一"])
    assert storage.download_file.call_count == 2
    assert storage.list_files.call_count == 2
    storage.upload_file.assert_called_once()


def test_remote_image_cue_does_not_receive_a_single_track_title(tmp_path, monkeypatch):
    """
    远端整轨的 CUE 必须参与对位，不能因临时目录只有音频而误认为普通单曲。
    """
    album = _album()
    path = _audio(tmp_path / "remote" / "image.flac", "本地整轨", "本地歌手", 1)
    cue = path.parent / "album.cue"
    cue.write_text(
        'PERFORMER "本地歌手"\nTITLE "整轨专辑"\nFILE "image.flac" WAVE\n'
        '  TRACK 01 AUDIO\n    TITLE "第一首"\n    INDEX 01 00:00:00\n'
        '  TRACK 02 AUDIO\n    TITLE "第二首"\n    INDEX 01 00:01:00\n',
        encoding="utf-8",
    )
    before = cue.read_bytes()
    chain = _chain(monkeypatch, album, "overwrite")
    audio_item, _, storage = _remote_storage(chain, path, cue)

    with use_music_cue(True):
        success, message = _scrape(chain, album, [audio_item])

    assert success and "未可靠匹配" in message
    assert AudioMetadataHelper.read_tags(path).title == "本地整轨"
    assert cue.read_bytes() == before
    storage.upload_file.assert_called_once()


def test_remote_cue_added_after_planning_invalidates_track_match(tmp_path, monkeypatch):
    """
    对位后出现整轨 CUE 时，写入阶段必须重新读取并取消过期的单曲匹配。
    """
    album = _album()
    path = _audio(tmp_path / "remote" / "image.flac", "本地整轨", "本地歌手", 1)
    cue = path.parent / "album.cue"
    cue.write_text(
        'PERFORMER "本地歌手"\nTITLE "整轨专辑"\nFILE "image.flac" WAVE\n'
        '  TRACK 01 AUDIO\n    TITLE "第一首"\n    INDEX 01 00:00:00\n'
        '  TRACK 02 AUDIO\n    TITLE "第二首"\n    INDEX 01 00:01:00\n',
        encoding="utf-8",
    )
    chain = _chain(monkeypatch, album, "overwrite")
    audio_item, cue_item, storage = _remote_storage(chain, path, cue)
    storage.list_files.side_effect = [[audio_item], [audio_item, cue_item]]

    with use_music_cue(True):
        success, message = _scrape(chain, album, [audio_item])

    assert success and "未可靠匹配" in message
    assert AudioMetadataHelper.read_tags(path).title == "本地整轨"
    assert storage.list_files.call_count == 2


def test_remote_upload_failure_is_not_reported_as_matched(tmp_path, monkeypatch):
    """
    临时音轨完成对位但上传失败时，响应必须报告失败而非声称远端已匹配。
    """
    album = _album()
    path = _audio(tmp_path / "remote" / "01.flac", "本地标题", "本地歌手", 1)
    before = path.read_bytes()
    chain = _chain(monkeypatch, album, "overwrite")
    audio_item, _, storage = _remote_storage(chain, path)
    storage.upload_file.side_effect = None
    storage.upload_file.return_value = None

    with use_music_cue(True):
        success, message = _scrape(chain, album, [audio_item])

    assert not success and "写入失败" in message
    assert "曲目匹配" not in message
    assert path.read_bytes() == before


def test_remote_overwrite_honors_disabled_cue_policy(tmp_path, monkeypatch):
    """
    用户关闭 CUE 读取时，曲目对位不能额外扫描远端目录或下载索引。
    """
    album = _album()
    path = _audio(tmp_path / "remote" / "01.flac", "旧标题", "本地歌手", 1)
    chain = _chain(monkeypatch, album, "overwrite")
    audio_item, _, storage = _remote_storage(chain, path)
    storage.list_files.side_effect = AssertionError("CUE 关闭时不得扫描目录")

    with use_music_cue(False):
        success, message = _scrape(chain, album, [audio_item])

    assert success and "匹配 1" in message
    assert AudioMetadataHelper.read_tags(path).title == "正确一"
    storage.list_files.assert_not_called()


def test_remote_directory_with_too_many_cues_is_not_guessed(tmp_path, monkeypatch):
    """
    远端 CUE 数量超过读取器上限时应保留标签，不能下载部分索引后猜测。
    """
    album = _album()
    path = _audio(tmp_path / "remote" / "01.flac", "本地标题", "本地歌手", 1)
    chain = _chain(monkeypatch, album, "overwrite")
    audio_item, _, storage = _remote_storage(chain, path)
    storage.list_files.return_value = [
        audio_item,
        *[_item(Path(f"/music/album/{number}.cue"), "u115") for number in range(33)],
    ]

    with use_music_cue(True):
        success, message = _scrape(chain, album, [audio_item])

    assert success and "未可靠匹配" in message
    assert AudioMetadataHelper.read_tags(path).title == "本地标题"
    assert all(call.args[0] == audio_item for call in storage.download_file.call_args_list)


@pytest.mark.parametrize("failure", ["list", "empty", "missing_audio", "cue"])
def test_remote_unknown_cue_state_preserves_track_tags(tmp_path, monkeypatch, failure):
    """
    远端目录或 CUE 读取失败时不能假设音轨没有整轨索引。
    """
    album = _album()
    path = _audio(tmp_path / "remote" / "01.flac", "本地标题", "本地歌手", 1)
    cue = path.parent / "album.cue"
    cue.write_text("FILE \"01.flac\" WAVE\n", encoding="utf-8")
    chain = _chain(monkeypatch, album, "overwrite")
    audio_item, cue_item, storage = _remote_storage(chain, path, cue)
    if failure == "list":
        storage.list_files.return_value = None
    elif failure == "empty":
        storage.list_files.return_value = []
    elif failure == "missing_audio":
        storage.list_files.return_value = [cue_item]
    else:
        original_download = storage.download_file.side_effect
        storage.download_file.side_effect = lambda item, path=None: (
            None if item == cue_item else original_download(item, path=path)
        )

    with use_music_cue(True):
        success, message = _scrape(chain, album, [audio_item])

    assert success and "未可靠匹配" in message
    assert AudioMetadataHelper.read_tags(path).title == "本地标题"


def test_remote_renamed_download_does_not_authorize_track_overwrite(tmp_path, monkeypatch):
    """
    音频下载名不等于远端原名时，CUE 无法可靠引用，必须保留已有曲目。
    """
    album = _album()
    path = _audio(tmp_path / "remote" / "01.flac", "本地标题", "本地歌手", 1)
    chain = _chain(monkeypatch, album, "overwrite")
    audio_item, _, storage = _remote_storage(chain, path)

    def download(_item, path=None):
        """
        模拟存储提供方意外更改本地副本文件名。
        """
        target = path / "renamed.flac"
        shutil.copyfile(audio_item_path, target)
        return target

    audio_item_path = path
    storage.download_file.side_effect = download
    with use_music_cue(True):
        success, message = _scrape(chain, album, [audio_item])

    assert success and "未可靠匹配" in message
    assert AudioMetadataHelper.read_tags(path).title == "本地标题"


def test_album_preservation_summary_is_logged(tmp_path, monkeypatch):
    """
    手动调用不仅返回保留提示，也应在刮削日志中留下同样的信息。
    """
    album = _album()
    path = _audio(tmp_path / "01.flac", "本地标题", "本地歌手", 1)
    chain = _chain(monkeypatch, album, "missingOnly")
    logger = Mock()
    monkeypatch.setattr("app.chain.scraping.logger", logger)

    success, message = _scrape(chain, album, [_item(path)])

    assert success and "保留" in message
    assert any("保留" in call.args[0] and "音乐标签" in call.args[0] for call in logger.info.call_args_list)


def test_album_lyrics_counts_and_track_preservation_are_both_reported(tmp_path, monkeypatch):
    """
    歌词成功数量和专辑曲目保留提示必须同时返回，不能互相覆盖。
    """
    album = _album()
    path = _audio(tmp_path / "01.flac", "本地标题", "本地歌手", 1)
    chain = _chain(monkeypatch, album, "missingOnly")
    chain.scraping_policies = ScrapingConfig({
        "music_nfo": "missingOnly", "music_poster": "skip", "music_lyrics": "upgrade",
    })
    chain._scrape_music_lyrics = Mock(return_value="saved")
    monkeypatch.setattr("app.chain.scraping.LyricsChain", Mock(return_value=Mock()))

    success, message = _scrape(chain, album, [_item(path)])

    assert success and "保留已有曲名、艺人和曲序" in message
    assert "歌词新增 1 首" in message


@pytest.mark.parametrize("policy,expected", [("missingOnly", "保留"), ("overwrite", "匹配 1")])
def test_music_album_http_response_reports_track_outcome(tmp_path, monkeypatch, policy, expected):
    """
    真实管理权限路由必须把专辑曲目处理说明返回给 REST 调用方。
    """
    album = _album()
    path = _audio(tmp_path / "01.flac", "本地标题", "本地歌手", 1)
    chain = _chain(monkeypatch, album, policy)
    recognizer = Mock()
    recognizer.return_value.recognize_media.return_value = album.to_music_info()
    monkeypatch.setattr(media_endpoint, "MediaChain", recognizer)
    monkeypatch.setattr(media_endpoint, "ScrapingChain", lambda: chain)
    app = FastAPI()
    app.include_router(media_endpoint.router, prefix="/api/v1/media")
    app.dependency_overrides[get_current_active_user] = lambda: SimpleNamespace(
        is_active=True, is_superuser=True, permissions={},
    )

    response = TestClient(app).post(
        "/api/v1/media/scrape/local",
        params={"media_source": "musicbrainz", "media_id": _GROUP, "type_name": "音乐", "music_type": "album"},
        json=_item(path).model_dump(mode="json"),
    )

    assert response.status_code == 200
    assert response.json()["success"] is True
    assert expected in response.json()["message"]
