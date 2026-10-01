"""按真实发行单元整理多碟、群星和全集，保持标签类型与文件查询范围。"""

import shutil
from pathlib import Path
from unittest.mock import Mock

import pytest
from mutagen.flac import FLAC
from mutagen.id3 import TPOS, TXXX

from app.application.audio import AudioMetadataHelper
from app.chain.media import MediaChain
from app.chain.media.cache import AlbumDirectoryCache
from app.domain.meta.metamusic import MetaMusic
from app.schemas.types import MediaType
from tests.test_music_identity_evidence import _tag_audio
from tests.test_transfer_sync_extra_files import make_fileitem, make_transfer_chain

RELEASE_A = "11111111-1111-4111-8111-111111111111"
RELEASE_B = "22222222-2222-4222-8222-222222222222"


def _audio(path: Path, *, artist="Artist", album="Record", release=None, release_type=None, disc=None, compilation=False):
    """写入真实 FLAC 标签，省略总碟数以验证目录补缺。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(__file__).parent / "fixtures/audio/silence.flac", path)
    audio = FLAC(path)
    audio["title"] = [path.stem]
    audio["tracknumber"] = ["1"]
    audio["date"] = ["2003"]
    for key, value in (("artist", artist), ("album", album), ("discnumber", disc),
                       ("musicbrainz_albumid", release), ("releasetype", release_type)):
        if value is not None:
            audio[key] = [str(value)]
    if compilation:
        audio["compilation"] = ["1"]
    audio.save()
    return make_fileitem(str(path))


def _prepare(items, mtype=MediaType.MUSIC):
    """构建隔离整理批次，保留生产读取及分组函数。"""
    owner = make_transfer_chain()
    context = owner._prepare_music_batch_context([(item, False) for item in items], mtype)
    return owner, context


def _resolve(owner, context, item):
    """从批次读取真实文件的最终局部音乐上下文，不执行文件副作用。"""
    return owner._resolve_music_batch_file_context(
        batch_context=context, file_item=item, file_path=Path(item.path),
        file_meta=AudioMetadataHelper.read(Path(item.path)), selected_tracks={}, fallback=None,
        discard_shared_identity=True, multi_track_batch=True, release_regions=None, release_scripts=None,
    )


@pytest.mark.parametrize("mtype", [None, MediaType.MUSIC])
def test_disc_directories_share_release_and_keep_disc_paths(tmp_path, mtype):
    """自动与显式音乐目录的 CD1/CD2 属于同一发行，总碟数缺失也保留分碟。"""
    items = [_audio(tmp_path / "Record" / f"CD{disc}" / "Song.flac", release=RELEASE_A)
             for disc in (1, 2)]
    owner, context = _prepare(items, mtype)
    groups = [context.release_by_main_key[owner._get_file_key(item)] for item in items]

    assert groups[0] is groups[1]
    assert groups[0].directory == tmp_path / "Record"
    assert context.single_main_keys == set()
    for disc, item in enumerate(items, 1):
        meta, info = _resolve(owner, context, item)
        assert meta.disc_number == info.disc_number == disc
        assert meta.total_discs == info.total_discs == 2
        assert meta.field_sources["total_discs"] == "directory"


def test_compilation_groups_different_performers_without_replacing_them(tmp_path):
    """明确合辑标签可补专辑艺人为群星，各曲真实艺人不得被多数票覆盖。"""
    items = [_audio(tmp_path / "Compilation" / f"Song {index}.flac", artist=artist, compilation=True)
             for index, artist in enumerate(("Artist A", "Artist B"), 1)]
    owner, context = _prepare(items)

    for item, artist in zip(items, ("Artist A", "Artist B")):
        meta, info = _resolve(owner, context, item)
        assert meta.artists == info.artists == [artist]
        assert meta.album_artist == info.album_artist == "Various Artists"
        assert "Compilation" in info.secondary_types


@pytest.mark.parametrize("release_type,expected", [("EP", "EP"), ("single", "Single"), ("album / soundtrack", "Album")])
def test_explicit_release_type_survives_local_batch(tmp_path, release_type, expected):
    """多音轨目录不能把标签已明确的 EP 或 Single 强制改成 Album。"""
    items = [_audio(tmp_path / "Record" / f"Song {index}.flac", release_type=release_type) for index in (1, 2)]
    owner, context = _prepare(items)

    for item in items:
        meta, info = _resolve(owner, context, item)
        assert meta.album_type == info.album_type == expected
        if "soundtrack" in release_type:
            assert info.secondary_types == ["Soundtrack"]


def test_collection_child_albums_and_same_name_editions_stay_separate(tmp_path):
    """全集子专辑和同目录不同 Release ID 都不能被合成一个发行单元。"""
    items = [
        _audio(tmp_path / "Collection" / "2003 - First" / "Song.flac", album="First"),
        _audio(tmp_path / "Collection" / "2004 - Second" / "Song.flac", album="Second"),
        _audio(tmp_path / "Editions" / "A.flac", release=RELEASE_A),
        _audio(tmp_path / "Editions" / "B.flac", release=RELEASE_B),
    ]
    owner, context = _prepare(items)
    groups = [context.release_by_main_key[owner._get_file_key(item)] for item in items]

    assert len({tuple(item.path for item in group.files) for group in groups}) == 4


def test_partial_tags_use_consistent_album_peers_without_online_lookup(tmp_path, monkeypatch):
    """同发行有足够一致标签时，只有曲名的文件可用邻居标签补专辑而不重复联网。"""
    items = [_audio(tmp_path / "Record" / f"Song {index}.flac") for index in (1, 2)]
    items.append(_audio(tmp_path / "Record" / "Third Song.flac", artist=None, album=None))
    owner, context = _prepare(items)
    online = Mock(side_effect=AssertionError("已确认的标签组不应重新联网"))
    monkeypatch.setattr(owner, "_match_music_album_context", online)

    meta, info = _resolve(owner, context, items[-1])

    assert meta.title == info.title == "Third Song"
    assert meta.album == info.album == "Record"
    assert meta.artists == ["Artist"]
    assert meta.field_sources["album"] == "album_tags"
    online.assert_not_called()


def test_tag_scan_is_shared_by_grouping_and_local_resolution(tmp_path, monkeypatch):
    """同一批次的分组与快速整理复用纯标签快照，避免各分组重复读取。"""
    items = [_audio(tmp_path / "Record" / f"Song {index}.flac") for index in (1, 2)]
    reader = Mock(wraps=AudioMetadataHelper.read_tags)
    monkeypatch.setattr(AudioMetadataHelper, "read_tags", reader)
    owner, context = _prepare(items)
    for item in items:
        owner._resolve_music_batch_file_context(
            batch_context=context, file_item=item, file_path=Path(item.path),
            file_meta=MetaMusic(title=Path(item.path).stem), selected_tracks={}, fallback=None,
            discard_shared_identity=True, multi_track_batch=True, release_regions=None, release_scripts=None,
        )

    assert reader.call_count == 2


def test_album_cache_separates_file_scopes_in_one_directory(tmp_path, monkeypatch):
    """同名同年的两组文件各自缓存结果，不能交替覆盖成重复查询或串用映射。"""
    items = [_audio(tmp_path / f"Song {index}.flac") for index in range(4)]
    chain = MediaChain()
    monkeypatch.setattr(chain, "_album_dir_cache", AlbumDirectoryCache(16))
    source = Mock(match_music_album=Mock(return_value=None))
    monkeypatch.setattr("app.chain.media.album.MusicBrainzChain", Mock(return_value=source))
    first = [Path(item.path) for item in items[:2]]
    second = [Path(item.path) for item in items[2:]]
    for selected in (first, second, first):
        chain.recognize_music_album_directory(tmp_path, file_paths=selected)

    assert source.match_music_album.call_count == 2
    assert [len(call.args[1]) for call in source.match_music_album.call_args_list] == [2, 2]


def test_container_header_overrides_misleading_extension(tmp_path):
    """MP3 即使命名为 FLAC 也不能被标作无损音频。"""
    path = tmp_path / "misnamed.flac"
    shutil.copyfile(Path(__file__).parent / "fixtures/audio/silence.mp3", path)

    meta = AudioMetadataHelper.read(path)

    assert meta.audio_format == "MP3"
    assert meta.audio_lossless is False
    assert meta.field_sources["audio_format"] == "stream"


def test_tag_cache_never_crosses_storage_namespaces(tmp_path):
    """云盘同名逻辑路径不能借用本地音频的标签或发行分组。"""
    local = _audio(tmp_path / "Song.flac")
    remote = local.model_copy(update={"storage": "alist"})
    owner, context = _prepare([local, remote])

    assert len(context.tags_by_file) == 1
    assert context.release_by_main_key[owner._get_file_key(remote)].evidence is None


@pytest.mark.parametrize("different_disc,expected_groups", [(False, 2), (True, 1)])
def test_duplicate_positions_split_encodings_but_mixed_discs_do_not(tmp_path, different_disc, expected_groups):
    """重复曲序的两种编码独立匹配，混合编码但不同碟号的同一发行保留一组。"""
    paths = [tmp_path / "Song.flac", tmp_path / "Song.mp3"]
    for path in paths:
        shutil.copyfile(Path(__file__).parent / f"fixtures/audio/silence{path.suffix}", path)
        _tag_audio(path)
    if different_disc:
        from mutagen.mp3 import MP3

        audio = MP3(paths[1])
        audio.tags.add(TPOS(encoding=3, text=["1/2"]))
        audio.tags.add(TXXX(encoding=3, desc="MusicBrainz Release Track Id", text=["55555555-5555-4555-8555-555555555555"]))
        audio.save()
    items = [make_fileitem(str(path)) for path in paths]
    owner, context = _prepare(items)

    groups = {tuple(file.path for file in context.release_by_main_key[owner._get_file_key(item)].files) for item in items}
    assert len(groups) == expected_groups


def test_missing_album_with_conflicting_artist_does_not_gain_peer_tags(tmp_path):
    """已知艺人冲突时，不能给未声明专辑的文件套上邻居的专辑标签。"""
    items = [_audio(tmp_path / f"Song {index}.flac") for index in (1, 2)]
    items.append(_audio(tmp_path / "Unrelated.flac", artist="Different Artist", album=None))
    owner, context = _prepare(items)

    assert owner._get_file_key(items[-1]) not in context.album_main_keys
    assert context.release_by_main_key[owner._get_file_key(items[-1])].files == (items[-1],)


def test_sparse_tags_do_not_label_an_entire_mixed_inbox(tmp_path):
    """普通收件目录的少数标签不能把大量未知音轨都绑定到同一专辑。"""
    items = [_audio(tmp_path / "inbox" / f"Tagged {index}.flac") for index in (1, 2)]
    items += [_audio(tmp_path / "inbox" / f"Unknown {index}.flac", artist=None, album=None) for index in range(6)]
    owner, context = _prepare(items)

    assert all(owner._get_file_key(item) not in context.album_main_keys for item in items[2:])
