"""
音乐订阅最小有效记录；不注册测试用例。
"""

from types import SimpleNamespace
from app.schemas.types import MediaType


def _subscribe(**overrides) -> SimpleNamespace:
    """
    构造不依赖数据库的音乐订阅对象。
    """
    values = dict(
        id=7,
        name="晴天",
        year="2003",
        type=MediaType.MUSIC.value,
        keyword=None,
        search_imdbid=False,
        media_source="musicbrainz",
        media_id="recording-1",
        mediaid=None,
        music_type="recording",
        total_tracks=None,
        downloaded_tracks=None,
        season=None,
        episode_group=None,
        tmdbid=None,
        imdbid=None,
        tvdbid=None,
        doubanid=None,
        bangumiid=None,
        anilistid=None,
        sites=[],
        filter_groups=[],
        quality=None,
        resolution=None,
        effect=None,
        audio_quality=None,
        audio_format=None,
        min_bitrate=None,
        min_bit_depth=None,
        min_sample_rate=None,
        include=None,
        exclude=None,
        username="admin",
        save_path=None,
        downloader=None,
        custom_words=None,
        media_category=None,
        best_version=0,
        best_version_full=0,
        current_priority=None,
        current_audio_format=None,
        current_bitrate=None,
        current_bit_depth=None,
        current_sample_rate=None,
        state="R",
        note=None,
        description=None,
        poster=None,
        backdrop=None,
    )
    values.update(overrides)
    subscribe = SimpleNamespace(**values)
    subscribe.to_dict = lambda: dict(values)
    return subscribe
