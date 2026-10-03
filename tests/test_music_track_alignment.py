"""发行曲目对位不依赖排序，并在名称、身份、曲序和时长之间保留证据边界。"""

from itertools import permutations
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.chain.media import MediaChain
from app.chain.transfer.facade import TransferChain
from app.domain.context import MusicInfo
from app.domain.meta.metamusic import MetaMusic
from app.domain.music import align_music_tracks
from tests.test_transfer_sync_extra_files import make_fileitem


def _track(title, number, duration=None, **kwargs):
    """构造带独立录音身份和实际发行位置的来源曲目。"""
    return MusicInfo(title=title, media_source="musicbrainz", media_id=f"recording-{number}",
                     track_number=number, duration=duration, **kwargs)


def test_unmatched_remainder_is_never_zipped_to_remaining_song():
    """前曲匹配不代表剩余文件自动属于最后一曲。"""
    files = [Path("01.flac"), Path("bonus.flac")]
    metas = [MetaMusic(title="First", track_number=1), MetaMusic(title="Unrelated Bonus")]
    tracks = [_track("First", 1), _track("Second", 2)]

    assert MediaChain._align_music_album_tracks(files, metas, tracks) == {files[0]: tracks[0]}


@pytest.mark.parametrize("title", ["01", "Track 01", "audio_01", "音轨 01", ""])
def test_placeholder_name_can_use_unique_position(title):
    """不完整的抓轨命名可用发行位置补全，不要求占位曲名命中。"""
    assert align_music_tracks([MetaMusic(title=title, track_number=1)], [_track("Actual Song", 1)]) == {0: 0}


def test_numeric_title_from_real_tags_is_not_a_placeholder():
    """真实数字歌曲名会与候选比较，不能被错误曲序吞掉。"""
    local = MetaMusic(title="22", track_number=1, field_sources={"title": "tag"})
    tracks = [_track("Different Song", 1), _track("22", 2)]

    assert align_music_tracks([local], tracks) == {0: 1}


def test_placeholder_stored_in_tags_is_still_weak():
    """抓轨器把 Track 01 写进标签也不会使它成为真实歌曲名。"""
    local = MetaMusic(title="Track 01", track_number=1, field_sources={"title": "tag"})

    assert align_music_tracks([local], [_track("Actual Song", 1)]) == {0: 0}


def test_same_title_and_missing_disc_remains_ambiguous():
    """多碟重复曲名及曲序需要碟号或时长区分，不能默认分配到第一碟。"""
    tracks = [_track("Song", 1, disc_number=disc) for disc in (1, 2)]

    assert align_music_tracks([MetaMusic(title="Song", track_number=1)], tracks) == {}
    assert align_music_tracks([MetaMusic(title="Song", track_number=1, disc_number=2)], tracks) == {0: 1}


def test_duplicate_local_editions_cannot_race_for_one_track():
    """相同证据的两份音源不能由遍历顺序决定谁冒充对应发行曲目。"""
    metas = [MetaMusic(title="First", track_number=1) for _ in range(2)]

    assert align_music_tracks(metas, [_track("First", 1)]) == {}


def test_unlabeled_tracks_can_use_unique_durations_and_ignore_filename_sort():
    """缺少曲序时，彼此明显不同的时长仍可恢复顺序；两个方向均需唯一。"""
    local = [MetaMusic(title="Track", duration=seconds) for seconds in (240, 180, 300)]
    remote = [_track("First", 1, 180), _track("Second", 2, 240), _track("Third", 3, 300)]
    for metas in permutations(local):
        for tracks in permutations(remote):
            matched = align_music_tracks(list(metas), list(tracks))
            assert {metas[index].duration: tracks[other].duration for index, other in matched.items()} == {
                180: 180, 240: 240, 300: 300,
            }


def test_near_equal_durations_do_not_break_ambiguity():
    """测量取整的一秒差异不能在两个同样合理的候选之间制造唯一性。"""
    local = MetaMusic(title="01", duration=180)

    assert align_music_tracks([local], [_track("First", 1, 179), _track("Second", 2, 181)]) == {}


@pytest.mark.parametrize("manual", [False, True])
def test_duration_conflict_blocks_even_exact_title_and_position(manual):
    """曲名和位置都相同也不能覆盖实际时长指示的不同内容。"""
    local = MetaMusic(title="First", track_number=1, duration=300)

    assert align_music_tracks([local], [_track("First", 1, 180)], allow_title_override=manual) == {}


def test_conflicting_position_does_not_beat_exact_title_or_duration():
    """错标曲序应由明确的曲名或唯一时长纠正，不能先按序号绑定。"""
    tracks = [_track("First", 1, 180), _track("Second", 2, 195)]
    assert align_music_tracks([MetaMusic(title="Second", track_number=1, duration=195)], tracks) == {0: 1}
    assert align_music_tracks([MetaMusic(title="01", track_number=1, duration=195)], tracks) == {0: 1}


def test_manual_release_can_correct_titles_without_enabling_automatic_guessing():
    """明确手选发行可用唯一曲序纠名；自动匹配对同一冲突仍然拒绝。"""
    local = MetaMusic(title="Wrong Name", track_number=1)
    tracks = [_track("Correct Name", 1)]

    assert align_music_tracks([local], tracks) == {}
    assert align_music_tracks([local], tracks, allow_title_override=True) == {0: 0}


def test_explicit_release_identity_rejects_different_release():
    """具体发行 ID 不能被相同曲名及曲序抵消，手选新发行可纠正旧标签身份。"""
    local = MetaMusic(title="First", track_number=1, musicbrainz_release_id="release-a")
    track = _track("First", 1, musicbrainz_release_id="release-b")

    assert align_music_tracks([local], [track]) == {}
    assert align_music_tracks([local], [track], allow_title_override=True) == {0: 0}


def test_release_track_id_resolves_repeated_recording():
    """同一录音多次出现在发行时，Release Track ID 可确定实际槽位。"""
    local = MetaMusic(title="Song", musicbrainz_release_track_id="slot-b")
    tracks = [_track("Song", number, musicbrainz_release_track_id=slot) for number, slot in ((1, "slot-a"), (2, "slot-b"))]

    assert align_music_tracks([local], tracks) == {0: 1}


def test_recording_id_conflict_is_not_ignored():
    """相同标题不能覆盖已知的另一个录音身份。"""
    local = MetaMusic(title="First", media_source="musicbrainz", media_id="other-recording")

    assert align_music_tracks([local], [_track("First", 1)]) == {}


def test_symbolic_names_and_recording_versions_keep_distinct_identities():
    """纯符号歌曲名不能归一为空，同名现场/混音也不能当作普通录音。"""
    assert align_music_tracks([MetaMusic(title="!!!")], [_track("???", 1), _track("!!!", 2)]) == {0: 1}
    assert align_music_tracks([MetaMusic(title="Song (Live)", track_number=1)], [_track("Song", 1)]) == {}


def test_mismatched_file_metadata_lengths_are_rejected():
    """元数据读取丢项时不能用截断 zip 悄悄改变文件对应关系。"""
    assert MediaChain._align_music_album_tracks([Path("first.flac"), Path("second.flac")],
                                               [MetaMusic(title="First")], [_track("First", 1)]) == {}


def test_numbered_filename_survives_transfer_album_evidence_check(monkeypatch):
    """专辑已经按位置对位后，整理层不能再次用占位曲名把正确曲目拒绝。"""
    path = Path("/music/Artist - Album/01.flac")
    track = _track("Actual Song (Live)", 1, 180, album="Album", artists=["Artist"])
    monkeypatch.setattr(MediaChain, "recognize_music_album_directory", Mock(return_value={str(path.resolve()): track}))
    local = MetaMusic(title="01", track_number=1, album="Album", artists=["Artist"], duration=180,
                      field_sources={"title": "filename"})

    meta, info = TransferChain._match_music_album_context(make_fileitem(str(path)), path, local)

    assert info is not None
    assert meta.title == info.title == "Actual Song (Live)"
    assert info.media_id == track.media_id
