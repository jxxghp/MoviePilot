import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from app.chain.acoustid import AcoustIdChain
from app.chain.listenbrainz import ListenBrainzChain
from app.chain.media import MediaChain
from app.chain.musicbrainz import MusicBrainzChain
from app.chain.recommend import RecommendChain
from app.chain.search import SearchChain
from app.domain.context import MusicAlbumInfo, MusicArtistInfo, MusicInfo
from app.domain.meta.metamusic import MetaMusic
from app.modules.musicbrainz import MusicBrainzModule
from app.schemas.types import MediaSource, MediaType


def test_parse_query_supports_artist_title_format():
    """艺术家与标题格式应拆分为结构化搜索条件。"""
    meta = MetaMusic.parse_query("  周杰伦  -  晴天  ")

    assert meta.artists == ["周杰伦"]
    assert meta.title == "晴天"
    assert meta.org_string == "  周杰伦  -  晴天  "


def test_parse_query_keeps_plain_title():
    """普通文本应保留为歌曲或专辑标题。"""
    meta = MetaMusic.parse_query("  Random   Access Memories ")

    assert meta.artists == []
    assert meta.title == "Random Access Memories"


def test_parse_query_strips_quality_tokens_before_artist_split():
    """音质规格不应参与艺术家/曲名拆分，年份括号与格式后缀需剔除。"""
    meta = MetaMusic.parse_query("毛阿敏 - 永遠是朋友(2000) - ALAC [16B-44.1kHz]")

    assert meta.artists == ["毛阿敏"]
    assert meta.title == "永遠是朋友"
    assert meta.audio_format == "ALAC"


def test_parse_query_does_not_misplit_quality_only_tail():
    """无艺术家时格式规格段不能被误拆成曲名，曲名不能丢字。"""
    meta = MetaMusic.parse_query("永遠是朋友(2000) - ALAC [16B-44.1kHz]")

    assert meta.artists == []
    assert meta.title == "永遠是朋友"


def test_parse_query_strips_trailing_artist_suffix():
    """曲名尾部重复的艺术家署名应被剥离，不作为曲名参与检索。"""
    meta = MetaMusic.parse_query("毛阿敏 - 名人名曲-毛阿敏(2000)")

    assert meta.artists == ["毛阿敏"]
    assert meta.title == "名人名曲"
    assert meta.year == 2000

    meta = MetaMusic.parse_query("许茹芸 - 争奇斗艳演唱会实况 2 - 许茹芸 (1996)")

    assert meta.artists == ["许茹芸"]
    assert meta.title == "争奇斗艳演唱会实况 2"


def test_parse_query_keeps_non_artist_suffix():
    """曲名尾段与艺术家不一致时不应被误剥离。"""
    meta = MetaMusic.parse_query("毛阿敏 - 思念 - 现场版")

    assert meta.artists == ["毛阿敏"]
    assert meta.title == "思念 - 现场版"


def test_build_site_keywords_searches_album_before_combined_artist():
    """专辑先按专辑名扩大召回，再用艺术家与专辑名组合词回退。"""
    info = MusicInfo(
        music_type="album",
        title="Get Lucky",
        artists=["Daft Punk"],
        album="Random Access Memories",
    )

    assert SearchChain.music_site_keywords(info) == [
        "Random Access Memories",
        "Daft Punk Random Access Memories",
    ]


def test_build_site_keywords_searches_track_before_combined_artist():
    """单曲先按曲名扩大召回，且关键词不能混入所属专辑名。"""
    info = MusicInfo(
        music_type="recording",
        title="Get Lucky",
        artists=["Daft Punk"],
        album="Random Access Memories",
    )

    assert SearchChain.music_site_keywords(info) == [
        "Get Lucky",
        "Daft Punk Get Lucky",
    ]


def test_build_site_keywords_places_simplified_album_terms_before_original_terms():
    """繁体专辑信息应先搜索简体名称及组合词，再回退原文且不单搜艺术家。"""
    info = MusicInfo(
        music_type="album",
        title="永遠是朋友",
        artists=["周華健"],
        album="永遠是朋友",
    )

    assert SearchChain.music_site_keywords(info) == [
        "永远是朋友",
        "永遠是朋友",
        "周华健 永远是朋友",
        "周華健 永遠是朋友",
    ]


def test_build_site_keywords_places_simplified_recording_terms_before_original_terms():
    """繁体单曲信息应先搜索简体曲名及组合词，不生成仅艺术家的关键词。"""
    info = MusicInfo(
        music_type="recording",
        title="後來",
        artists=["劉若英"],
        album="我等你",
    )

    assert SearchChain.music_site_keywords(info) == [
        "后来",
        "後來",
        "刘若英 后来",
        "劉若英 後來",
    ]


def test_album_resource_match_requires_selected_album_title():
    """专辑订阅只接受同时包含目标专辑名和艺术家的站点资源。"""
    album = MusicInfo(
        music_type="album",
        title="Random Access Memories",
        album="Random Access Memories",
        artists=["Daft Punk"],
        names=["Random-Access Memories"],
    )

    assert SearchChain.matches_music_resource(
        album,
        "Daft.Punk-Random.Access.Memories-2013-FLAC",
    ) is True
    assert SearchChain.matches_music_resource(album, "Daft Punk - Discovery - FLAC") is False


def test_recording_resource_match_does_not_treat_album_name_as_track_alias():
    """单曲候选的兼容 names 即使包含专辑名，也不能让整专标题冒充目标单曲。"""
    recording = MusicInfo(
        music_type="recording",
        title="Get Lucky",
        album="Random Access Memories",
        artists=["Daft Punk"],
        names=["Get Lucky", "Random Access Memories"],
    )

    assert SearchChain.matches_music_resource(recording, "Daft Punk - Get Lucky FLAC") is True
    assert SearchChain.matches_music_resource(
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

    assert SearchChain.matches_music_resource(recording, "周杰伦 - 晴天 FLAC") is True
    assert SearchChain.matches_music_resource(recording, "其他艺人 - 晴天 FLAC") is False
    assert SearchChain.matches_music_resource(recording, "晴天 FLAC") is False


def test_album_resource_match_combines_title_description_and_converts_traditional_chinese():
    """专辑名与艺术家可分处标题和副标题，繁简及干扰符号不影响匹配。"""
    album = MusicInfo(
        music_type="album",
        title="永远是朋友",
        album="永远是朋友",
        artists=["周华健"],
    )

    assert SearchChain.matches_music_resource(
        album,
        "【永遠・是朋友】24bit／96kHz",
        "專輯藝人：周華健；無損音樂",
    ) is True


def test_recording_resource_match_combines_title_description_and_converts_traditional_chinese():
    """单曲按曲名与艺术家匹配，并统一繁简及全角符号。"""
    recording = MusicInfo(
        music_type="recording",
        title="晴天",
        artists=["周杰伦"],
    )

    assert SearchChain.matches_music_resource(
        recording,
        "０１．晴 天［FLAC］",
        "演唱：周杰倫",
    ) is True


def test_music_resource_match_rejects_target_without_artist():
    """目标缺少艺术家时不能仅凭同名专辑或单曲放行。"""
    recording = MusicInfo(music_type="recording", title="晴天")

    assert SearchChain.matches_music_resource(recording, "周杰伦 - 晴天 FLAC") is False


def test_normalize_candidates_deduplicates_source_identity():
    """同一来源和媒体 ID 的音乐候选应只保留一次。"""
    results = MediaChain.normalize_music_candidates(
        [
            MusicInfo(media_source="musicbrainz", media_id="recording-1", title="A"),
            {
                "type": "音乐",
                "media_source": "musicbrainz",
                "media_id": "recording-1",
                "title": "A duplicate",
            },
        ]
    )

    assert len(results) == 1
    assert results[0].title == "A"


def test_normalize_candidates_keeps_different_entities_with_same_source_id():
    """同一来源 ID 在不同音乐实体命名空间下不能互相去重。"""
    results = MediaChain.normalize_music_candidates(
        [
            MusicInfo(media_source="musicbrainz", media_id="shared-id", music_type="recording", title="Song"),
            MusicInfo(media_source="musicbrainz", media_id="shared-id", music_type="album", title="Album"),
        ]
    )

    assert [item.music_type for item in results] == ["recording", "album"]


def test_normalize_candidates_deduplicates_metadata_without_id():
    """缺少来源 ID 时应按标题、艺术家和专辑去重。"""
    results = MediaChain.normalize_music_candidates(
        [
            MusicInfo(title="One More Time", artists=["Daft Punk"], album="Discovery"),
            MusicInfo(title=" one  more time ", artists=["daft punk"], album="DISCOVERY"),
        ]
    )

    assert len(results) == 1


def test_to_meta_preserves_selected_identity():
    """候选转换后应保留下载和整理所需的标准身份。"""
    info = MusicInfo(
        media_source="musicbrainz",
        media_id="recording-1",
        title="晴天",
        artists=["周杰伦"],
        album="叶惠美",
        year=2003,
        track_number=3,
    )

    meta = MetaMusic.from_music_info(info)

    assert meta.media_source == "musicbrainz"
    assert meta.media_id == "recording-1"
    assert meta.artists == ["周杰伦"]
    assert meta.album == "叶惠美"
    assert meta.track_number == 3


def test_chart_converts_page_to_listenbrainz_offset(monkeypatch):
    """音乐榜单处理链应将页码转换为模块需要的偏移量。"""
    chain = ListenBrainzChain()
    requested = {}

    def fake_unicast(method, **kwargs):
        """记录榜单模块调用并返回重复候选。"""
        requested.update(method=method, **kwargs)
        return [
            MusicInfo(media_source="musicbrainz", media_id="recording-1", title="晴天"),
            MusicInfo(media_source="musicbrainz", media_id="recording-1", title="晴天"),
        ]

    monkeypatch.setattr(chain, "unicast", fake_unicast)

    results = chain.music_chart(range_name="this_week", page=2, count=30)

    assert requested == {
        "method": "music_chart",
        "range_name": "this_week",
        "offset": 30,
        "count": 30,
        "entity": "recording",
    }
    assert len(results) == 2


def test_async_chart_applies_music_explore_filters(monkeypatch):
    """音乐探索应按收听次数、封面条件和升序设置筛选榜单。"""
    chain = RecommendChain()

    async def fake_music_chart(**kwargs):
        """返回包含不同热度和封面状态的榜单候选。"""
        return [
            MusicInfo(media_id="1", media_source="musicbrainz", title="A", listen_count=300),
            MusicInfo(
                media_id="2",
                media_source="musicbrainz",
                title="B",
                listen_count=120,
                cover_url="https://coverartarchive.org/release/2/front-500",
            ),
            MusicInfo(
                media_id="3",
                media_source="musicbrainz",
                title="C",
                listen_count=240,
                cover_url="https://coverartarchive.org/release/3/front-500",
            ),
        ]

    source_chain = Mock()
    source_chain.async_music_chart = AsyncMock(side_effect=fake_music_chart)
    monkeypatch.setattr("app.chain.recommend.ListenBrainzChain", Mock(return_value=source_chain))

    results = asyncio.run(
        chain.async_music_chart(
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
        MusicInfo(media_source="musicbrainz", media_id="1", title="晴天", artists=["其他歌手"]),
        MusicInfo(
            media_source="musicbrainz",
            media_id="2",
            title="晴天",
            artists=["周杰伦"],
            album="叶惠美",
        ),
    ]

    selected = MusicBrainzModule._select_candidate(meta, candidates, media_source="musicbrainz")

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
        media_source="musicbrainz",
        media_id="recording-1",
        title="眼泪成诗",
        artists=["孙燕姿"],
        album="完美的一天",
        cover_url="https://coverartarchive.org/release-group/album-1/front-500",
    )
    chain = MediaChain()
    recognize = AsyncMock(return_value=info)
    filename_meta = MetaMusic(title="02. 眼泪成诗")
    monkeypatch.setattr(
        "app.chain.media.AudioMetadataHelper.read_evidence",
        Mock(return_value=(meta, meta, filename_meta)),
    )
    monkeypatch.setattr(
        AcoustIdChain,
        "async_identify_music_by_fingerprint",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(chain, "async_recognize_media", recognize)

    recognized_meta, recognized_info = asyncio.run(
        chain.async_recognize_music_by_path(audio_path)
    )

    assert recognized_meta is meta
    assert recognized_info is info
    recognize.assert_awaited_once_with(
        meta=meta,
        media_source=None,
        music_type="recording",
    )


def test_media_chain_default_recognition_only_queries_musicbrainz(monkeypatch):
    """自动音乐识别只应调用 MusicBrainz 主数据源。"""
    chain = MediaChain()
    meta = MetaMusic(
        title="晴天",
        artists=["周杰伦"],
        album="叶惠美",
        year=2003,
    )
    expected = MusicInfo(
        media_source="musicbrainz",
        media_id="mb-1",
        title="晴天",
        artists=["周杰伦"],
        album="叶惠美",
        year=2003,
    )
    recognize_source = Mock(return_value=expected)
    monkeypatch.setattr(chain, "recognize_music_from_source", recognize_source)

    with patch("app.chain._recognition.MoviePilotServerHelper.report_recognize_share"):
        result = chain.recognize_media(meta=meta)

    assert result is expected
    recognize_source.assert_called_once_with(
        media_source=MediaSource.MusicBrainz,
        meta=meta,
        cache=True,
        music_type="recording",
    )


def test_recognize_from_source_selects_only_declared_music_source_chain(monkeypatch):
    """单源识别只允许调用声明该音乐来源的模块，忽略同接口影视模块。"""
    chain = MediaChain()
    meta = MetaMusic(title="晴天", artists=["周杰伦"])
    expected = MusicInfo(
        media_source="musicbrainz",
        media_id="recording-1",
        title="晴天",
        artists=["周杰伦"],
    )
    source_chain = Mock()
    source_chain.recognize_music.return_value = expected
    monkeypatch.setattr(chain, "_music_source_chain", Mock(return_value=source_chain))

    result = chain.recognize_music_from_source(
        media_source="musicbrainz",
        meta=meta,
        cache=True,
    )

    assert result is expected
    source_chain.recognize_music.assert_called_once_with(
        meta=meta,
        media_id=None,
        cache=True,
        music_type=None,
    )


def test_default_recognition_does_not_fallback_after_musicbrainz_miss(monkeypatch):
    """MusicBrainz 未命中时自动识别不得继续请求其它音乐来源。"""
    chain = MediaChain()
    meta = MetaMusic(title="晴天", artists=["周杰伦"])
    recognize_source = Mock(return_value=None)
    monkeypatch.setattr(chain, "recognize_music_from_source", recognize_source)

    with patch(
        "app.chain._recognition.MoviePilotServerHelper.query_recognize_share",
        return_value=None,
    ):
        assert chain.recognize_media(meta=meta) is None
    recognize_source.assert_called_once()
    assert recognize_source.call_args.kwargs["media_source"] == MediaSource.MusicBrainz


def test_async_default_recognition_only_queries_musicbrainz(monkeypatch):
    """异步自动音乐识别也只应调用 MusicBrainz 主数据源。"""
    chain = MediaChain()
    meta = MetaMusic(title="晴天", artists=["周杰伦"])
    expected = MusicInfo(
        media_source="musicbrainz",
        media_id="recording-1",
        title="晴天",
        artists=["周杰伦"],
    )
    recognize_source = AsyncMock(return_value=expected)
    monkeypatch.setattr(chain, "async_recognize_music_from_source", recognize_source)

    with patch(
        "app.chain._recognition.MoviePilotServerHelper.async_report_recognize_share",
        new=AsyncMock(),
    ):
        result = asyncio.run(chain.async_recognize_media(meta=meta))

    assert result is expected
    recognize_source.assert_awaited_once_with(
        media_source=MediaSource.MusicBrainz,
        meta=meta,
        cache=True,
        music_type="recording",
    )


def test_musicbrainz_source_chain_calls_module_async_method(monkeypatch):
    """单源异步识别必须直接等待模块异步入口。"""
    chain = MusicBrainzChain()
    expected = MusicInfo(
        media_source="musicbrainz",
        media_id="recording-1",
        title="晴天",
    )
    async_unicast = AsyncMock(return_value=expected)
    unicast = Mock(side_effect=AssertionError("异步识别不应调用同步模块方法"))
    monkeypatch.setattr(chain, "async_unicast", async_unicast)
    monkeypatch.setattr(chain, "unicast", unicast)

    result = asyncio.run(chain.async_recognize_music(
        meta=MetaMusic(title="晴天"), cache=True
    ))

    assert result is expected
    async_unicast.assert_awaited_once()
    unicast.assert_not_called()


def test_async_identify_by_fingerprint_uses_async_module_contract(monkeypatch):
    """指纹异步链路应请求模块的异步方法名。"""
    chain = AcoustIdChain()
    async_unicast = AsyncMock(return_value="recording-1")
    monkeypatch.setattr(chain, "async_unicast", async_unicast)

    result = asyncio.run(chain.async_identify_music_by_fingerprint("/music/track.flac"))

    assert result == "recording-1"
    async_unicast.assert_awaited_once_with(
        "async_identify_music_by_fingerprint",
        path=Path("/music/track.flac"),
    )


def test_async_chart_forwards_album_entity(monkeypatch):
    """热门专辑探索应把实体类型透传给 ListenBrainz 榜单模块。"""
    chain = ListenBrainzChain()
    requested = {}

    async def fake_async_unicast(method, **kwargs):
        """记录榜单请求参数并返回一个专辑候选。"""
        requested.update(method=method, **kwargs)
        return [
            MusicInfo(
                media_id="release-group-1",
                media_source="musicbrainz",
                music_type="album",
                title="ARIRANG",
                listen_count=10,
            )
        ]

    monkeypatch.setattr(chain, "async_unicast", fake_async_unicast)

    results = asyncio.run(chain.async_music_chart(range_name="week", page=3, count=20, entity="album"))

    assert requested["method"] == "music_chart"
    assert requested["entity"] == "album"
    assert requested["offset"] == 40
    assert results[0].music_type == "album"


def test_async_fresh_releases_keeps_official_order(monkeypatch):
    """新发行探索应保留官方排序，只按封面条件过滤。"""
    chain = RecommendChain()
    requested = {}

    async def fake_fresh_releases(**kwargs):
        """记录新发行请求参数并返回带封面与不带封面的候选。"""
        requested.update(**kwargs)
        return [
            MusicInfo(media_id="b", media_source="musicbrainz", music_type="album", title="B"),
            MusicInfo(
                media_id="a",
                media_source="musicbrainz",
                music_type="album",
                title="A",
                cover_url="https://coverartarchive.org/release/a/front-500",
            ),
        ]

    source_chain = Mock()
    source_chain.async_music_fresh_releases = AsyncMock(side_effect=fake_fresh_releases)
    monkeypatch.setattr("app.chain.recommend.ListenBrainzChain", Mock(return_value=source_chain))

    results = asyncio.run(
        chain.async_music_fresh_releases(
            days=30, sort="release_name", page=2, count=10, with_cover=True
        )
    )

    assert requested["page"] == 2
    assert requested["sort"] == "release_name"
    assert [item.title for item in results] == ["A"]


def test_async_artist_related_preserves_source_results(monkeypatch):
    """关联艺术家来源链应保留来源返回顺序和实体。"""
    chain = MusicBrainzChain()

    async def fake_async_unicast(method, **kwargs):
        """返回重复的关联艺术家候选。"""
        assert method == "music_artist_related"
        return [
            MusicArtistInfo(media_source="musicbrainz", media_id="artist-1", name="Brian May"),
            MusicArtistInfo(media_source="musicbrainz", media_id="artist-1", name="Brian May"),
            MusicArtistInfo(media_source="musicbrainz", media_id="artist-2", name="John Deacon"),
        ]

    monkeypatch.setattr(chain, "async_unicast", fake_async_unicast)

    results = asyncio.run(chain.async_get_music_artist_related(media_id="artist-0"))

    assert [item.media_id for item in results] == ["artist-1", "artist-1", "artist-2"]


def test_async_album_returns_source_chain_result(monkeypatch):
    """媒体链应按来源和 ID 返回来源链提供的标准专辑对象。"""
    chain = MediaChain()

    async def fake_get_album(media_id):
        """模拟插件模块以字典形式返回专辑详情。"""
        assert media_id == "release-group-1"
        return MusicAlbumInfo(
            media_source="musicbrainz",
            media_id="release-group-1",
            title="A Night at the Opera",
            artists=["Queen"],
            release_date="1975-11-21",
        )

    source_chain = Mock()
    source_chain.async_get_music_album = AsyncMock(side_effect=fake_get_album)
    monkeypatch.setattr(chain, "_music_source_chain", Mock(return_value=source_chain))

    album = asyncio.run(chain.async_get_music_album(
        media_source="musicbrainz", media_id="release-group-1"
    ))

    assert album is not None
    assert album.year == 1975
    assert album.artists == ["Queen"]
