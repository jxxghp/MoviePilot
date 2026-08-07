from app.chain.music import MusicChain
from app.core.music import MusicInfo, MusicMeta


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


def test_build_site_keywords_prefers_artist_album():
    """站点关键词应优先使用艺术家和专辑组合。"""
    info = MusicInfo(
        title="Get Lucky",
        artists=["Daft Punk"],
        album="Random Access Memories",
    )

    assert MusicChain.build_site_keywords(info) == [
        "Daft Punk Random Access Memories",
        "Daft Punk Get Lucky",
        "Random Access Memories",
        "Get Lucky",
    ]


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


def test_select_path_candidate_prefers_matching_audio_tags():
    """文件识别应优先选择标题、艺术家和专辑均匹配的 MusicBrainz 候选。"""
    meta = MusicMeta(title="晴天", artists=["周杰伦"], album="叶惠美")
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

    selected = MusicChain._select_path_candidate(meta, candidates, source="musicbrainz")

    assert selected is candidates[1]
