"""
订阅的写入路径：app/application/subscribe.py 的 add_subscribe / async_add_subscribe。

这两个函数是订阅表的唯一写入口，把 MediaInfo / MusicInfo 翻译成一行订阅：
标题、年份、类型、海报背景、评分简介、剧集组、音乐实体与曲目数，再叠上
持久化类型强转（布尔开关转整型、年份转字符串）。字段映射错了不会报错，
只会让订阅静静地记错——而搜索、洗版、完成判定、去重全都读这张表。

因此这里断言的是「落库后每个字段的实际值」，不是「调用了什么」：
同目录的 test_subscribe_oper.py 用替身钉的是查重语义（谁被查、查几次），
证明不了写进去的到底是什么。两者互补，缺一不可。

唯一的例外是**持久化类型强转**（年份转字符串、布尔开关转整型）。这两步是
为 PostgreSQL 的严格类型检查而存在的，而测试库是 SQLite——SQLite 的类型
亲和会在写入时把 ``2003`` 悄悄转成 ``'2003'``、把 ``True`` 转成 ``1``，
落库后的值对「写入路径自己有没有转」完全无感（已用变异验证确认：删掉
``_normalize_year`` 后按落库值断言的用例全部照过）。所以这两类契约必须在
建模那一刻、即 ``Subscribe(**kwargs)`` 的入参上断言，见 ``payloads`` 夹具。
强转跟着订阅表的列走，因此仍留在 ``app/db/oper/subscribe.py``，夹具也就
仍然钉在那个模块的 ``Subscribe`` 上。

同步与异步两条链路是两份逐字复制的实现，任何一条改了另一条没跟上都属于
真实缺陷，故每个字段契约都在两条链路上各断言一次。
"""
import asyncio

import pytest

from app.application.subscribe import add_subscribe, async_add_subscribe
from app.db.models.subscribe import Subscribe
from app.db.oper.subscribe import SubscribeOper
from app.domain.context import MediaInfo, MusicInfo
from app.schemas.types import MediaSource, MediaType


@pytest.fixture(autouse=True)
def _track(db):
    """把订阅表纳入用例级回收。"""
    db.watermark(Subscribe)


def _media_id(tag: str) -> str:
    """给每个用例分配独立媒体 ID，避免共用测试库时互相去重。"""
    return f"wp-{tag}"


def _mediainfo(media_id: str, title: str = "识别标题",
               mtype: MediaType = MediaType.TV, year: str = "2026",
               episode_group: str = None) -> MediaInfo:
    """构造带完整展示字段的识别结果。"""
    media = MediaInfo()
    media.type = mtype
    media.title = title
    media.year = year
    media.media_source = MediaSource.TMDB
    media.media_id = media_id
    media.episode_group = episode_group
    media.vote_average = 8.5
    media.overview = "测试简介"
    media.poster_path = "https://image.tmdb.org/t/p/original/poster.jpg"
    media.backdrop_path = "https://image.tmdb.org/t/p/original/backdrop.jpg"
    return media


def _musicinfo(media_id: str, music_type: str, **kwargs) -> MusicInfo:
    """构造音乐订阅所需的标准音乐信息。"""
    return MusicInfo(media_source=MediaSource.MusicBrainz, media_id=media_id,
                     music_type=music_type, **kwargs)


def _add(oper: SubscribeOper, is_async: bool, **kwargs):
    """按链路分派到同步或异步新增，让同一份字段契约跑两遍。"""
    if is_async:
        return asyncio.run(async_add_subscribe(subscribe_oper=oper, **kwargs))
    return add_subscribe(subscribe_oper=oper, **kwargs)


def _row(db, subscribe_id: int) -> Subscribe:
    """按主键读回落库的订阅行。"""
    db.session.expire_all()
    return Subscribe.get(db.session, subscribe_id)


class _SubscribeSpy:
    """记录建模入参并转交真实模型，保持写入路径仍然真的落库。"""

    def __init__(self, recorded: list):
        self._recorded = recorded

    def __call__(self, **kwargs):
        """截获 ``Subscribe(**kwargs)`` 的入参后构造真实模型实例。"""
        self._recorded.append(dict(kwargs))
        return Subscribe(**kwargs)

    def __getattr__(self, name):
        """查重用的类方法（exists / async_exists 等）原样透传给真实模型。"""
        return getattr(Subscribe, name)


@pytest.fixture
def payloads(monkeypatch):
    """
    捕获写入路径建模时的原始 kwargs，即落库**前**的值与类型。

    只用于持久化类型强转这一类契约：SQLite 的类型亲和会在写入时替写入路径
    把类型「修好」，落库后的值证明不了转换真的发生过；而这两步转换恰恰是为
    PostgreSQL 而写的，漏了只会在生产库上炸。
    """
    recorded: list = []
    monkeypatch.setattr("app.db.oper.subscribe.Subscribe", _SubscribeSpy(recorded))
    return recorded


# 每个字段契约都在同步与异步两条链路上跑一遍：两份实现是逐字复制的，
# 只测一条等于放任另一条漂移
_BOTH_PATHS = pytest.mark.parametrize(
    "is_async", [pytest.param(False, id="sync"), pytest.param(True, id="async")]
)


# --------------------------------------------------------------------------- #
# 展示字段的翻译
# --------------------------------------------------------------------------- #

@_BOTH_PATHS
def test_add_maps_every_display_field_onto_the_row(db, is_async):
    """
    识别结果的展示字段必须完整落库。

    标题、年份、类型、评分、简介都是订阅列表和通知的唯一数据来源，
    错一项用户就看到一条张冠李戴的订阅。
    """
    oper = SubscribeOper()
    media_id = _media_id(f"display-{is_async}")

    sid, message = _add(oper, is_async, mediainfo=_mediainfo(media_id), season=1)

    assert message == "新增订阅成功"
    row = _row(db, sid)
    assert row.name == "识别标题"
    assert row.year == "2026"
    assert row.type == MediaType.TV.value
    assert row.media_source == MediaSource.TMDB.value
    assert row.media_id == media_id
    assert row.season == 1
    assert row.vote == 8.5
    assert row.description == "测试简介"


@_BOTH_PATHS
def test_add_persists_poster_and_backdrop_from_media(db, is_async):
    """
    海报与背景取自识别结果的图片接口，而不是原始路径字段。

    接口会把 original 尺寸换成 w500，直接存 poster_path 会让列表页
    每张卡片都去拉原图。
    """
    oper = SubscribeOper()
    media_id = _media_id(f"image-{is_async}")

    sid, _ = _add(oper, is_async, mediainfo=_mediainfo(media_id), season=1)

    row = _row(db, sid)
    assert row.poster == "https://image.tmdb.org/t/p/w500/poster.jpg"
    assert row.backdrop == "https://image.tmdb.org/t/p/w500/backdrop.jpg"


@_BOTH_PATHS
def test_add_persists_episode_group(db, is_async):
    """
    剧集组必须来自识别结果并落库。

    订阅按剧集组去重、搜索也按剧集组匹配集数，丢了它主季与自定义组会互相顶替。
    """
    oper = SubscribeOper()
    media_id = _media_id(f"eg-{is_async}")

    sid, _ = _add(oper, is_async,
                  mediainfo=_mediainfo(media_id, episode_group="eg-1"), season=1)

    assert _row(db, sid).episode_group == "eg-1"


@_BOTH_PATHS
def test_add_stamps_creation_date(db, is_async):
    """
    新增时间由写入路径盖戳，调用方传入的值不作数。

    订阅列表默认按 date 排序、过期清理也读它，留空会让这条订阅永远排在最后。
    """
    oper = SubscribeOper()
    media_id = _media_id(f"date-{is_async}")

    sid, _ = _add(oper, is_async, mediainfo=_mediainfo(media_id), season=1,
                  date="1970-01-01 00:00:00")

    date = _row(db, sid).date
    assert date is not None
    assert date != "1970-01-01 00:00:00"
    # 形如 2026-08-14 12:34:56
    assert len(date) == 19 and date[4] == "-" and date[13] == ":"


# --------------------------------------------------------------------------- #
# 持久化类型强转
# --------------------------------------------------------------------------- #

@_BOTH_PATHS
def test_add_converts_boolean_flags_to_integers(db, payloads, is_async):
    """
    历史兼容的布尔开关建模前必须转成整型。

    PostgreSQL 的整型列拒收布尔值，不转会让新增订阅在 PG 上直接抛类型错误。
    断言落在建模入参上而非落库值：SQLite 会替我们把 True 存成 1，
    按落库值断言的话删掉转换也照样通过。
    """
    oper = SubscribeOper()
    media_id = _media_id(f"flags-{is_async}")

    _add(oper, is_async, mediainfo=_mediainfo(media_id), season=1,
         best_version=True, best_version_full=False, manual_total_episode=True)

    payload = payloads[-1]
    for field, expected in (("best_version", 1), ("best_version_full", 0),
                            ("manual_total_episode", 1)):
        assert payload[field] == expected
        assert type(payload[field]) is int, f"{field} 仍是 {type(payload[field])}"


@_BOTH_PATHS
@pytest.mark.parametrize(
    "supplied, expected",
    [
        pytest.param(True, 1, id="真"),
        pytest.param(False, 0, id="假"),
        pytest.param(None, 0, id="缺省"),
    ],
)
def test_add_normalizes_search_imdbid_to_zero_or_one(db, payloads, is_async,
                                                     supplied, expected):
    """
    search_imdbid 无论传什么都归一到整型 0/1。

    这一列参与搜索分支判定，存进 None 或 True 会让「是否用 imdbid 搜」
    在不同订阅上表现不一致，在 PG 上还会直接拒写。
    """
    oper = SubscribeOper()
    media_id = _media_id(f"imdb-{is_async}-{supplied}")

    _add(oper, is_async, mediainfo=_mediainfo(media_id), season=1,
         search_imdbid=supplied)

    payload = payloads[-1]
    assert payload["search_imdbid"] == expected
    assert type(payload["search_imdbid"]) is int


@_BOTH_PATHS
def test_add_converts_numeric_year_to_string(db, payloads, is_async):
    """
    音乐链路的年份是数字，而 year 列是字符串，建模前必须转换。

    不转在 PostgreSQL 上直接抛类型错误。同样只能在建模入参上验证：
    SQLite 的 TEXT 亲和会把整数 2003 自动存成 '2003'，读回来看不出差别。
    """
    oper = SubscribeOper()
    media_id = _media_id(f"year-{is_async}")

    sid, _ = _add(oper, is_async,
                  mediainfo=_musicinfo(media_id, "album", title="叶惠美", year=2003))

    assert payloads[-1]["year"] == "2003"
    assert type(payloads[-1]["year"]) is str
    # 落库值也要对得上，转换不能只发生在建模而在写入时被改回去
    assert _row(db, sid).year == "2003"


@_BOTH_PATHS
def test_add_keeps_missing_year_as_null(db, payloads, is_async):
    """
    年份缺失时留空，不能变成字符串 "None"。

    转字符串离无脑 str() 只有一步之遥，写成 "None" 后年份筛选会命中一个
    不存在的年份，而这条订阅从此在按年份筛选的界面里凭空消失。
    """
    oper = SubscribeOper()
    media_id = _media_id(f"noyear-{is_async}")

    sid, _ = _add(oper, is_async,
                  mediainfo=_musicinfo(media_id, "album", title="无年份专辑"))

    assert payloads[-1]["year"] is None
    assert _row(db, sid).year is None


# --------------------------------------------------------------------------- #
# 音乐字段
# --------------------------------------------------------------------------- #

@_BOTH_PATHS
def test_add_persists_album_entity_and_track_count(db, is_async):
    """
    专辑订阅要落实体类型和总曲目数。

    整专完成判定拿 total_tracks 当分母，缺了这条订阅永远判不到完成。
    """
    oper = SubscribeOper()
    media_id = _media_id(f"album-{is_async}")

    sid, _ = _add(oper, is_async,
                  mediainfo=_musicinfo(media_id, "album", title="叶惠美", total_tracks=11))

    row = _row(db, sid)
    assert row.type == MediaType.MUSIC.value
    assert row.music_type == "album"
    assert row.total_tracks == 11


@_BOTH_PATHS
def test_add_drops_track_count_for_single_recording(db, is_async):
    """
    单曲订阅只留实体类型，专辑曲目数必须丢弃。

    单曲带着专辑的 total_tracks 会让完成判定把一首歌当整专等，永远不完成。
    """
    oper = SubscribeOper()
    media_id = _media_id(f"recording-{is_async}")

    sid, _ = _add(oper, is_async,
                  mediainfo=_musicinfo(media_id, "recording", title="晴天", total_tracks=11))

    row = _row(db, sid)
    assert row.music_type == "recording"
    assert row.total_tracks is None


@_BOTH_PATHS
def test_add_clears_music_fields_for_non_music_media(db, is_async):
    """
    非音乐媒体的音乐字段一律置空，调用方传进来的也要被覆盖。

    影视订阅带上 music_type 会被音乐去重逻辑当成音乐实体，造成串号。
    """
    oper = SubscribeOper()
    media_id = _media_id(f"nonmusic-{is_async}")

    sid, _ = _add(oper, is_async, mediainfo=_mediainfo(media_id), season=1,
                  music_type="album", total_tracks=99)

    row = _row(db, sid)
    assert row.music_type is None
    assert row.total_tracks is None


@_BOTH_PATHS
def test_add_persists_music_cover_as_poster_and_backdrop(db, is_async):
    """
    音乐订阅的海报与背景都用发行封面。

    音乐没有独立背景图，留空会让订阅卡片在列表里显示成一块空白。
    """
    oper = SubscribeOper()
    media_id = _media_id(f"cover-{is_async}")
    cover = "https://coverartarchive.org/release-group/example/front-500"

    sid, _ = _add(oper, is_async,
                  mediainfo=_musicinfo(media_id, "album", title="封面专辑", cover_url=cover))

    row = _row(db, sid)
    assert row.poster == cover
    assert row.backdrop == cover


# --------------------------------------------------------------------------- #
# 调用方字段的保留
# --------------------------------------------------------------------------- #

@_BOTH_PATHS
def test_add_keeps_caller_supplied_subscription_settings(db, is_async):
    """
    调用方传入的订阅设置要原样保留，不被媒体翻译覆盖。

    写入路径只负责翻译媒体身份与展示字段；把用户填的保存路径、过滤词、
    总集数一并覆盖掉，等于用户每次新增订阅的设置都白填。
    """
    oper = SubscribeOper()
    media_id = _media_id(f"settings-{is_async}")

    sid, _ = _add(oper, is_async, mediainfo=_mediainfo(media_id), season=1,
                  username="alice", save_path="/media/tv", keyword="关键字",
                  include="内嵌", exclude="预告", total_episode=12,
                  start_episode=3, downloader="qbittorrent", state="R")

    row = _row(db, sid)
    assert row.username == "alice"
    assert row.save_path == "/media/tv"
    assert row.keyword == "关键字"
    assert (row.include, row.exclude) == ("内嵌", "预告")
    assert (row.total_episode, row.start_episode) == (12, 3)
    assert row.downloader == "qbittorrent"
    assert row.state == "R"


@_BOTH_PATHS
def test_add_overrides_caller_supplied_media_fields(db, is_async):
    """
    媒体身份与展示字段以识别结果为准，调用方传的同名值要被覆盖。

    否则上游一个陈旧的 name/year 就能让订阅记成另一部剧，而去重按身份走、
    发现不了这种错位。
    """
    oper = SubscribeOper()
    media_id = _media_id(f"override-{is_async}")

    sid, _ = _add(oper, is_async,
                  mediainfo=_mediainfo(media_id, title="正确标题", year="2026"),
                  season=1, name="错误标题", year="1999", type="电影")

    row = _row(db, sid)
    assert row.name == "正确标题"
    assert row.year == "2026"
    assert row.type == MediaType.TV.value
