from unittest.mock import AsyncMock

from app.chain.media import MediaChain
from app.chain.music import MusicChain
from app.core.context import MusicAlbumInfo, MusicArtistInfo, MusicInfo
from app.core.meta import MetaMusic
from app.helper.audio import AudioMetadataHelper
from app.modules.musicbrainz import MusicBrainzModule


def test_parse_query_supports_artist_title_format():
    """艺术家与标题格式应拆分为结构化搜索条件。"""
    meta = MusicChain.parse_query("  周杰伦  -  晴天  ")

    assert meta.artists == ["周杰伦"]
    assert meta.title == "晴天"
    assert meta.org_string == "  周杰伦  -  晴天  "


def test_parse_query_keeps_plain_title():
    """普通文本应保留为歌曲或专辑标题。"""
    meta = MusicChain.parse_query("  Random   Access Memories ")

    assert meta.artists == []
    assert meta.title == "Random Access Memories"


def test_parse_query_strips_quality_tokens_before_artist_split():
    """音质规格不应参与艺术家/曲名拆分，年份括号与格式后缀需剔除。"""
    meta = MusicChain.parse_query("毛阿敏 - 永遠是朋友(2000) - ALAC [16B-44.1kHz]")

    assert meta.artists == ["毛阿敏"]
    assert meta.title == "永遠是朋友"
    assert meta.audio_format == "ALAC"


def test_parse_query_does_not_misplit_quality_only_tail():
    """无艺术家时格式规格段不能被误拆成曲名，曲名不能丢字。"""
    meta = MusicChain.parse_query("永遠是朋友(2000) - ALAC [16B-44.1kHz]")

    assert meta.artists == []
    assert meta.title == "永遠是朋友"


def test_parse_query_strips_trailing_artist_suffix():
    """曲名尾部重复的艺术家署名应被剥离，不作为曲名参与检索。"""
    meta = MusicChain.parse_query("毛阿敏 - 名人名曲-毛阿敏(2000)")

    assert meta.artists == ["毛阿敏"]
    assert meta.title == "名人名曲"
    assert meta.year == 2000

    meta = MusicChain.parse_query("许茹芸 - 争奇斗艳演唱会实况 2 - 许茹芸 (1996)")

    assert meta.artists == ["许茹芸"]
    assert meta.title == "争奇斗艳演唱会实况 2"


def test_parse_query_keeps_non_artist_suffix():
    """曲名尾段与艺术家不一致时不应被误剥离。"""
    meta = MusicChain.parse_query("毛阿敏 - 思念 - 现场版")

    assert meta.artists == ["毛阿敏"]
    assert meta.title == "思念 - 现场版"


def test_build_site_keywords_prefers_artist_album():
    """专辑订阅只按艺术家与专辑名搜索，不混入其中某首单曲。"""
    info = MusicInfo(
        music_type="album",
        title="Get Lucky",
        artists=["Daft Punk"],
        album="Random Access Memories",
    )

    assert MusicChain.build_site_keywords(info) == [
        "Daft Punk Random Access Memories",
        "Random Access Memories",
    ]


def test_build_site_keywords_keeps_recording_out_of_album_search():
    """单曲订阅只按艺术家与曲名搜索，不能优先命中所属整张专辑。"""
    info = MusicInfo(
        music_type="recording",
        title="Get Lucky",
        artists=["Daft Punk"],
        album="Random Access Memories",
    )

    assert MusicChain.build_site_keywords(info) == [
        "Daft Punk Get Lucky",
        "Get Lucky",
    ]


def test_album_resource_match_requires_selected_album_title():
    """专辑订阅只接受包含目标专辑名的站点资源，忽略大小写、空格和标点差异。"""
    album = MusicInfo(
        music_type="album",
        title="Random Access Memories",
        album="Random Access Memories",
        names=["Random-Access Memories"],
    )

    assert MusicChain.matches_site_resource(
        album,
        "Daft.Punk-Random.Access.Memories-2013-FLAC",
    ) is True
    assert MusicChain.matches_site_resource(album, "Daft Punk - Discovery - FLAC") is False


def test_recording_resource_match_does_not_treat_album_name_as_track_alias():
    """单曲候选的兼容 names 即使包含专辑名，也不能让整专标题冒充目标单曲。"""
    recording = MusicInfo(
        music_type="recording",
        title="Get Lucky",
        album="Random Access Memories",
        names=["Get Lucky", "Random Access Memories"],
    )

    assert MusicChain.matches_site_resource(recording, "Daft Punk - Get Lucky FLAC") is True
    assert MusicChain.matches_site_resource(
        recording,
        "Daft Punk - Random Access Memories FLAC",
    ) is False


def test_resource_match_requires_artist_when_target_artist_is_known():
    """同名作品很多，目标已知艺术家时资源标题也必须包含该艺术家。"""
    recording = MusicInfo(
        music_type="recording",
        title="晴天",
        artists=["周杰伦"],
    )

    assert MusicChain.matches_site_resource(recording, "周杰伦 - 晴天 FLAC") is True
    assert MusicChain.matches_site_resource(recording, "其他艺人 - 晴天 FLAC") is False
    assert MusicChain.matches_site_resource(recording, "晴天 FLAC") is False


def test_normalize_candidates_deduplicates_source_identity():
    """同一来源和媒体 ID 的音乐候选应只保留一次。"""
    results = MusicChain.normalize_candidates(
        [
            MusicInfo(source="musicbrainz", media_id="recording-1", title="A"),
            {
                "type": "音乐",
                "source": "musicbrainz",
                "media_id": "recording-1",
                "title": "A duplicate",
            },
        ]
    )

    assert len(results) == 1
    assert results[0].title == "A"


def test_normalize_candidates_keeps_different_entities_with_same_source_id():
    """同一来源 ID 在不同音乐实体命名空间下不能互相去重。"""
    results = MusicChain.normalize_candidates(
        [
            MusicInfo(source="musicbrainz", media_id="shared-id", music_type="recording", title="Song"),
            MusicInfo(source="musicbrainz", media_id="shared-id", music_type="album", title="Album"),
        ]
    )

    assert [item.music_type for item in results] == ["recording", "album"]


def test_normalize_candidates_deduplicates_metadata_without_id():
    """缺少来源 ID 时应按标题、艺术家和专辑去重。"""
    results = MusicChain.normalize_candidates(
        [
            MusicInfo(title="One More Time", artists=["Daft Punk"], album="Discovery"),
            MusicInfo(title=" one  more time ", artists=["daft punk"], album="DISCOVERY"),
        ]
    )

    assert len(results) == 1


def test_to_meta_preserves_selected_identity():
    """候选转换后应保留下载和整理所需的标准身份。"""
    info = MusicInfo(
        source="musicbrainz",
        media_id="recording-1",
        title="晴天",
        artists=["周杰伦"],
        album="叶惠美",
        year=2003,
        track_number=3,
    )

    meta = MusicChain.to_meta(info)

    assert meta.media_source == "musicbrainz"
    assert meta.media_id == "recording-1"
    assert meta.artists == ["周杰伦"]
    assert meta.album == "叶惠美"
    assert meta.track_number == 3


def test_chart_converts_page_to_listenbrainz_offset(monkeypatch):
    """音乐榜单处理链应将页码转换为模块需要的偏移量。"""
    chain = MusicChain()
    requested = {}

    def fake_run_module(method, **kwargs):
        """记录榜单模块调用并返回重复候选。"""
        requested.update(method=method, **kwargs)
        return [
            MusicInfo(source="musicbrainz", media_id="recording-1", title="晴天"),
            MusicInfo(source="musicbrainz", media_id="recording-1", title="晴天"),
        ]

    monkeypatch.setattr(chain, "run_module", fake_run_module)

    results = chain.chart(range_name="this_week", page=2, count=30)

    assert requested == {
        "method": "music_chart",
        "range_name": "this_week",
        "offset": 30,
        "count": 30,
    }
    assert len(results) == 1


def test_async_chart_applies_music_explore_filters(monkeypatch):
    """音乐探索应按收听次数、封面条件和升序设置筛选榜单。"""
    chain = MusicChain()

    async def fake_async_run_module(method, **kwargs):
        """返回包含不同热度和封面状态的榜单候选。"""
        assert method == "music_chart"
        return [
            MusicInfo(media_id="1", source="musicbrainz", title="A", listen_count=300),
            MusicInfo(
                media_id="2",
                source="musicbrainz",
                title="B",
                listen_count=120,
                cover_url="https://coverartarchive.org/release/2/front-500",
            ),
            MusicInfo(
                media_id="3",
                source="musicbrainz",
                title="C",
                listen_count=240,
                cover_url="https://coverartarchive.org/release/3/front-500",
            ),
        ]

    monkeypatch.setattr(chain, "async_run_module", fake_async_run_module)

    import asyncio

    results = asyncio.run(
        chain.async_chart(
            range_name="this_month",
            count=30,
            sort_by="listen_count.asc",
            min_listen_count=100,
            with_cover=True,
        )
    )

    assert [item.title for item in results] == ["B", "C"]


def test_musicbrainz_module_select_candidate_prefers_matching_audio_tags():
    """文件识别应优先选择标题、艺术家和专辑均匹配的 MusicBrainz 候选。"""
    meta = MetaMusic(title="晴天", artists=["周杰伦"], album="叶惠美")
    candidates = [
        MusicInfo(source="musicbrainz", media_id="1", title="晴天", artists=["其他歌手"]),
        MusicInfo(
            source="musicbrainz",
            media_id="2",
            title="晴天",
            artists=["周杰伦"],
            album="叶惠美",
        ),
    ]

    selected = MusicBrainzModule._select_candidate(meta, candidates, source="musicbrainz")

    assert selected is candidates[1]


def test_async_recognize_by_path_reads_local_audio_tags(tmp_path, monkeypatch):
    """本地音频识别应使用内嵌标签补全艺术家、专辑并提高封面候选命中率。"""
    audio_path = tmp_path / "02. 眼泪成诗.m4a"
    audio_path.write_bytes(b"audio")
    meta = MetaMusic(
        title="眼泪成诗",
        artists=["孙燕姿"],
        album="完美的一天",
        track_number=2,
        duration=221,
    )
    info = MusicInfo(
        source="musicbrainz",
        media_id="recording-1",
        title="眼泪成诗",
        artists=["孙燕姿"],
        album="完美的一天",
        cover_url="https://coverartarchive.org/release-group/album-1/front-500",
    )
    chain = MediaChain()
    recognize = AsyncMock(return_value=info)
    monkeypatch.setattr(AudioMetadataHelper, "read", lambda path: meta)
    monkeypatch.setattr(chain, "async_recognize_media", recognize)

    import asyncio

    recognized_meta, recognized_info = asyncio.run(
        chain.async_recognize_music_by_path(audio_path)
    )

    assert recognized_meta is meta
    assert recognized_info is info
    recognize.assert_awaited_once_with(meta=meta, source="musicbrainz")


def test_async_chart_forwards_album_entity(monkeypatch):
    """热门专辑探索应把实体类型透传给 ListenBrainz 榜单模块。"""
    chain = MusicChain()
    requested = {}

    async def fake_async_run_module(method, **kwargs):
        """记录榜单请求参数并返回一个专辑候选。"""
        requested.update(method=method, **kwargs)
        return [
            MusicInfo(
                media_id="release-group-1",
                source="musicbrainz",
                music_type="album",
                title="ARIRANG",
                listen_count=10,
            )
        ]

    monkeypatch.setattr(chain, "async_run_module", fake_async_run_module)

    import asyncio

    results = asyncio.run(chain.async_chart(range_name="week", page=3, count=20, entity="album"))

    assert requested["method"] == "music_chart"
    assert requested["entity"] == "album"
    assert requested["offset"] == 40
    assert results[0].music_type == "album"


def test_async_fresh_releases_keeps_official_order(monkeypatch):
    """新发行探索应保留官方排序，只按封面条件过滤。"""
    chain = MusicChain()
    requested = {}

    async def fake_async_run_module(method, **kwargs):
        """记录新发行请求参数并返回带封面与不带封面的候选。"""
        requested.update(method=method, **kwargs)
        return [
            MusicInfo(media_id="b", source="musicbrainz", music_type="album", title="B"),
            MusicInfo(
                media_id="a",
                source="musicbrainz",
                music_type="album",
                title="A",
                cover_url="https://coverartarchive.org/release/a/front-500",
            ),
        ]

    monkeypatch.setattr(chain, "async_run_module", fake_async_run_module)

    import asyncio

    results = asyncio.run(
        chain.async_fresh_releases(days=30, sort="release_name", page=2, count=10, with_cover=True)
    )

    assert requested["method"] == "music_fresh_releases"
    assert requested["offset"] == 10
    assert requested["sort"] == "release_name"
    assert [item.title for item in results] == ["A"]


def test_async_artist_related_deduplicates_artists(monkeypatch):
    """关联艺术家应按标准 ID 去重，避免同一成员重复出现。"""
    chain = MusicChain()

    async def fake_async_run_module(method, **kwargs):
        """返回重复的关联艺术家候选。"""
        assert method == "music_artist_related"
        return [
            MusicArtistInfo(source="musicbrainz", media_id="artist-1", name="Brian May"),
            MusicArtistInfo(source="musicbrainz", media_id="artist-1", name="Brian May"),
            MusicArtistInfo(source="musicbrainz", media_id="artist-2", name="John Deacon"),
        ]

    monkeypatch.setattr(chain, "async_run_module", fake_async_run_module)

    import asyncio

    results = asyncio.run(chain.async_artist_related(source="musicbrainz", media_id="artist-0"))

    assert [item.media_id for item in results] == ["artist-1", "artist-2"]


def test_async_album_restores_dataclass_from_plugin_dict(monkeypatch):
    """插件返回字典时专辑链应恢复为标准专辑对象。"""
    chain = MusicChain()

    async def fake_async_run_module(method, **kwargs):
        """模拟插件模块以字典形式返回专辑详情。"""
        assert method == "music_album"
        return MusicAlbumInfo(
            source="musicbrainz",
            media_id="release-group-1",
            title="A Night at the Opera",
            artists=["Queen"],
            release_date="1975-11-21",
        ).to_dict()

    monkeypatch.setattr(chain, "async_run_module", fake_async_run_module)

    import asyncio

    album = asyncio.run(chain.async_album(source="musicbrainz", media_id="release-group-1"))

    assert album is not None
    assert album.year == 1975
    assert album.artists == ["Queen"]
