"""音乐搜索的名称边界、别名和人工候选回归测试。"""

import copy
import pickle

import pytest

from app.chain.download import DownloadChain
from app.chain.media import MediaChain
from app.chain.search import SearchChain
from app.domain.context import Context, MusicAlbumInfo, MusicInfo
from app.domain.meta.metamusic import MetaMusic
from app.domain.meta.runtime import get_metainfo_accelerator
from app.domain.music import match_music_resource


@pytest.mark.parametrize("title", ["U2 - One Tree Hill FLAC", "U2 - Someone FLAC", "U2 - One - Tree Hill FLAC"])
def test_music_match_rejects_other_titles(title):
    """短曲名不能将较长的另一首作品判为同一单曲。"""
    assert match_music_resource(MusicInfo(title="One", artists=["U2"]), title).status == "rejected"


@pytest.mark.parametrize("artist", ["Jay Chou", "周杰倫"])
def test_music_match_accepts_source_artist_aliases(artist):
    """同一艺术家来源别名和繁简署名均可命中。"""
    music = MusicInfo(title="晴天", artists=["周杰伦"], artist_aliases=["Jay Chou"])
    assert match_music_resource(music, f"{artist} - 晴天 FLAC").status == "exact"


@pytest.mark.parametrize("artist", ["VA", "V.A.", "群星"])
def test_music_match_accepts_compilation_credit(artist):
    """合辑通用艺术家署名不应造成漏搜。"""
    music = MusicInfo(music_type="album", title="Test Compilation", artists=["Various Artists"])
    assert match_music_resource(music, f"{artist} - Test Compilation FLAC").status == "exact"


@pytest.mark.parametrize("title,reason", [
    ("Jay Chou - 晴天 FLAC", "artist_unverified"),
    ("周杰伦 - 晴天 (Live) FLAC", "version_mismatch"),
])
def test_music_match_uncertain_identity_requires_confirmation(title, reason):
    """未核验艺名和不同录音版本只能交给用户确认。"""
    result = match_music_resource(MusicInfo(title="晴天", artists=["周杰伦"]), title)
    assert (result.status, result.reason) == ("candidate", reason)


def test_music_match_distinguishes_missing_evidence_and_related_album():
    """缺艺术家、缺分类和所属专辑均不得直接绑定成目标单曲。"""
    music = MusicInfo(title="Get Lucky", artists=["Daft Punk"], album="Random Access Memories")
    assert match_music_resource(music, "Daft Punk - Random Access Memories FLAC").status == "album"
    assert match_music_resource(music, "Daft Punk - Get Lucky FLAC", category=None).reason == "category_unknown"
    assert match_music_resource(MusicInfo(title="晴天"), "周杰伦 - 晴天 FLAC").reason == "target_artist_missing"


def test_music_match_handles_accents_and_edition_suffix():
    """变音符不改变身份，缺少明确的目标发行版本则必须人工确认。"""
    assert match_music_resource(MusicInfo(title="Halo", artists=["Beyoncé"]), "Beyonce - Halo FLAC").status == "exact"
    album = MusicInfo(music_type="album", title="Test Album (Deluxe Edition)", artists=["Artist"])
    assert match_music_resource(album, "Artist - Test Album FLAC").reason == "edition_unverified"


def test_resource_parser_merges_subtitle_without_target_information():
    """副标题补全艺术家、专辑和音质，资源元数据不携带目标 ID。"""
    meta = MetaMusic.parse_resource("晴天 FLAC", "演唱：周杰伦；专辑：叶惠美；24bit 96kHz")
    assert meta.title == "晴天"
    assert meta.artists == ["周杰伦"]
    assert meta.album == "叶惠美"
    assert meta.bit_depth == 24
    assert meta.media_id is None


def test_resource_parser_preserves_title_evidence_and_parses_track_segments():
    """冲突副标题不能覆盖标题艺术家，明确的曲序段则可用于区分专辑和单曲。"""
    meta = MetaMusic.parse_resource("Artist - Album - 01 - Song [FLAC]", "演唱：Other Artist")
    assert (meta.artists, meta.album, meta.track_number, meta.title) == (["Artist"], "Album", 1, "Song")
    live = MetaMusic.parse_resource("U2 - One (Live) FLAC")
    assert live.version == "Live"


def test_resource_parser_keeps_bracketed_title():
    """纯展示括号内的作品名不能与规格标签一同丢弃。"""
    meta = MetaMusic.parse_resource("【永遠・是朋友】24bit／96kHz", "專輯藝人：周華健；無損音樂")
    assert meta.title and "永遠" in meta.title
    assert meta.artists == ["周華健"]


def test_music_album_field_cannot_match_target_recording():
    """结构化资源中的所属专辑不能冒充同名目标单曲。"""
    music = MusicInfo(title="Album", artists=["Artist"])
    assert match_music_resource(music, "Artist - Album - 01 - Other Song FLAC").status == "rejected"
    wanted = MusicInfo(title="Wanted Song", album="Album", artists=["Artist"])
    assert match_music_resource(wanted, "Artist - Album - 01 - Other Song FLAC").status == "rejected"


def test_music_artist_in_subtitle_cannot_override_conflicting_title_credit():
    """另一位艺人的同名歌曲不能借副标题出现目标艺人而成为精确命中。"""
    music = MusicInfo(title="One", artists=["U2"])
    assert match_music_resource(music, "Metallica - One FLAC", "Related artist: U2").reason == "artist_unverified"


def test_artist_name_is_not_a_recording_version():
    """艺人名称含 Live 时，不能把署名误当作现场录音标记。"""
    music = MusicInfo(title="Song", artists=["Live"])
    assert match_music_resource(music, "Live - Song FLAC").status == "exact"


def test_compilation_album_credit_does_not_prove_track_artist():
    """单曲已经有明确艺人时，整专的合辑署名不构成同一录音的身份依据。"""
    music = MusicInfo(title="Song", artists=["Artist"], album_artist="Various Artists")
    assert match_music_resource(music, "VA - Song FLAC").status == "candidate"


def test_distinct_album_artist_is_not_a_recording_artist_alias():
    """演唱者与专辑艺术家分属不同实体时，不把专辑署名视为演唱者别名。"""
    music = MusicInfo(title="Song", artists=["Performer"], album_artist="Album Artist")
    assert match_music_resource(music, "Album Artist - Song FLAC").status == "candidate"


def test_simplification_does_not_merge_recording_and_album_artist_aliases():
    """文本展示转换不能把另一位专辑艺术家混入录音艺术家别名。"""
    original = MusicInfo(title="晴天", artists=["周杰倫"], album_artist="周華健")
    simplified = MediaChain._simplify_recognized_music_info(original)
    assert simplified.artist_aliases == ["周杰倫"]


@pytest.mark.parametrize("artist", ["AC/DC", "Earth, Wind & Fire"])
def test_compound_artist_credit_preserves_name_punctuation(artist):
    """已知完整艺名含分隔符时，不能因资源解析拆段而误判为另一位艺人。"""
    music = MusicInfo(title="Song", artists=[artist])
    assert match_music_resource(music, f"{artist} - Song FLAC").status == "exact"


@pytest.mark.parametrize("python_only", [False, True])
def test_resource_evidence_matches_python_and_native_parser_paths(monkeypatch, python_only):
    """资源级补充规则必须同时适用于 Python 回退和真实 Rust 标题解析。"""
    if python_only:
        monkeypatch.setattr("app.domain.meta.metamusic.get_metainfo_accelerator", lambda: None)
    else:
        accelerator = get_metainfo_accelerator()
        if not accelerator or not accelerator.parse_metamusic("Artist - Song FLAC"):
            pytest.skip("当前环境没有可用的 Rust 音乐解析器")
    meta = MetaMusic.parse_resource("Artist - Album - 01 - Song FLAC", "24bit 96kHz")
    assert (meta.title, meta.album, meta.track_number) == ("Song", "Album", 1)
    assert meta.artists == ["Artist"]
    assert (meta.bit_depth, meta.sample_rate) == (24, 96000)
    bracketed = MetaMusic.parse_resource("【永遠・是朋友】24bit／96kHz", "專輯藝人：周華健")
    assert bracketed.title and "永遠" in bracketed.title
    assert bracketed.artists == ["周華健"]


@pytest.mark.parametrize("factory", [MusicInfo, MusicAlbumInfo])
def test_old_music_cache_restores_alias_defaults(factory):
    """旧 pickle 没有新增字段时应恢复空列表，继续支持标准序列化和专辑投影。"""
    info = factory(title="Album", artists=["Artist"])
    for field in ("title_aliases", "artist_aliases", "album_aliases"):
        info.__dict__.pop(field, None)
    restored = pickle.loads(pickle.dumps(info))
    assert restored.to_dict()["title_aliases"] == []
    if isinstance(restored, MusicAlbumInfo):
        assert restored.to_music_info().artist_aliases == []


def test_clearing_music_context_does_not_mutate_source_snapshot():
    """共用结果裁剪只清理副本，不能清空模块缓存或调用方仍持有的原始响应。"""
    original = MusicInfo(title="Album", raw_data={"id": "source"})
    duplicate = copy.copy(original)
    duplicate.clear()
    assert duplicate.raw_data == {}
    assert original.raw_data == {"id": "source"}


def test_music_simplification_preserves_original_search_names():
    """展示繁转简后必须仍能按原始标题与艺术家检索。"""
    original = MusicInfo(music_type="album", title="永遠是朋友", album="永遠是朋友", artists=["周華健"])
    simplified = MediaChain._simplify_recognized_music_info(original)
    keywords = SearchChain.music_site_keywords(simplified)
    assert keywords[:2] == ["永远是朋友", "永遠是朋友"]
    assert "周華健" in simplified.artist_aliases
    assert MusicInfo.from_dict(simplified.to_dict()).title_aliases == ["永遠是朋友"]


def test_music_album_alias_is_used_for_search():
    """匹配接受的作品别名也必须进入实际站点查询阶梯。"""
    music = MusicInfo(music_type="album", title="Ye Hui Mei", title_aliases=["叶惠美"], artists=["Jay Chou"])
    assert "叶惠美" in SearchChain.music_site_keywords(music)


def test_automatic_batch_download_excludes_manual_music_candidates():
    """人工候选即使被传入批量入口，也不得自动交给下载器。"""
    chain = object.__new__(DownloadChain)
    assert chain._execute_batch_download([Context(match_status="candidate")]) == ([], None)
    assert chain._execute_batch_download([Context(match_status="candidate", match_reason="related_album")]) == ([], None)
