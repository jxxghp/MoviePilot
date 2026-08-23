"""
整理历史表的查询行为。

整理历史同时承担三个职责：查重（这个文件整理过没有）、溯源（这个媒体库文件是从哪
来的）、统计。查重误判会重复整理或永久漏件——挂载故障那一类问题最终就落在这张表上；
溯源查错会让「重新整理」把不相干的文件搬走。
"""
import asyncio
import time as _time

import pytest

from app.db.models import transferhistory as transferhistory_module
from app.db.models.transferhistory import TransferHistory
from app.schemas.types import MediaSource, MediaType

TMDB = str(MediaSource.TMDB)


@pytest.fixture(autouse=True)
def _track(db):
    """把整理历史表纳入用例级回收。"""
    db.watermark(TransferHistory)


def _hist(title: str = "片名", src: str = None, dest: str = None,
          src_storage: str = "local", dest_storage: str = "local",
          mode: str = "move", status: bool = True, mtype: str = None,
          year: str = "2026", media_id: str = "6001", seasons: str = None,
          episodes: str = None, date: str = "2026-08-13 10:00:00",
          download_hash: str = None) -> TransferHistory:
    """构造一条整理历史。"""
    return TransferHistory(src=src or f"/downloads/{title}.mkv", src_storage=src_storage,
                           dest=dest or f"/media/{title}.mkv", dest_storage=dest_storage,
                           mode=mode, type=mtype or MediaType.TV.value, title=title,
                           year=year, media_source=TMDB, media_id=media_id,
                           seasons=seasons, episodes=episodes, status=status,
                           date=date, download_hash=download_hash, files=[])


# --------------------------------------------------------------------------- #
# 按路径溯源
# --------------------------------------------------------------------------- #

def test_get_by_src_scopes_by_storage(db):
    """
    按源路径查询可限定存储，不限定时取主键最大的那条。

    同一路径在不同存储下是不同文件；不限定存储会把本地盘的记录当成网盘的。
    表上有 (src, src_storage) 唯一约束，所以「最新一条」只在跨存储时才有分歧。
    """
    db.add(_hist("本地", src="/data/same.mkv", src_storage="local"))
    latest = db.add(_hist("网盘", src="/data/same.mkv", src_storage="alist"))

    assert TransferHistory.get_by_src(db.session, "/data/same.mkv",
                                      storage="alist").title == "网盘"
    assert TransferHistory.get_by_src(db.session, "/data/same.mkv",
                                      storage="local").title == "本地"
    assert TransferHistory.get_by_src(db.session, "/data/same.mkv").id == latest.id
    assert TransferHistory.get_by_src(db.session, "/data/none.mkv") is None


def test_get_success_by_src_matches_path_verbatim(db):
    """
    成功记录按源路径原样精确匹配，不做归一化。

    蓝光原盘目录的记录带尾斜杠，归一化后反而匹配不到自己。
    """
    db.add(_hist("原盘", src="/data/BDMV/", status=True),
           _hist("失败的", src="/data/fail.mkv", status=False))

    assert TransferHistory.get_success_by_src(db.session, "/data/BDMV/").title == "原盘"
    assert TransferHistory.get_success_by_src(db.session, "/data/BDMV") is None
    assert TransferHistory.get_success_by_src(db.session, "/data/fail.mkv") is None


def test_get_success_by_src_can_scope_by_storage(db):
    """
    成功记录查询同样支持按源存储收窄。
    """
    db.add(_hist("本地", src="/data/x.mkv", src_storage="local"),
           _hist("网盘", src="/data/x.mkv", src_storage="alist"))

    assert TransferHistory.get_success_by_src(db.session, "/data/x.mkv",
                                              storage="alist").title == "网盘"


def test_get_by_dest_scopes_by_storage_and_takes_newest(db):
    """
    按目标路径查询用于从媒体库反查来源，可限定目标存储并取最新一条。
    """
    db.add(_hist("旧", src="/data/d1.mkv", dest="/media/same.mkv", dest_storage="local"))
    newest = db.add(_hist("新", src="/data/d2.mkv", dest="/media/same.mkv",
                          dest_storage="local"))
    db.add(_hist("别的存储", src="/data/d3.mkv", dest="/media/same.mkv",
                 dest_storage="alist"))

    assert TransferHistory.get_by_dest(db.session, "/media/same.mkv",
                                       storage="local").id == newest.id
    assert TransferHistory.get_by_dest(db.session, "/media/same.mkv",
                                       storage="alist").title == "别的存储"
    assert TransferHistory.get_by_dest(db.session, "/media/none.mkv") is None


def test_list_success_by_src_normalizes_the_path(db):
    """
    列举成功记录时对入参路径做归一化（反斜杠、尾斜杠）。

    Windows 侧传来的路径带反斜杠，不归一化会一条都匹配不到。
    """
    db.add(_hist("目录", src="/data/dir"))

    for probe in ("/data/dir", "/data/dir/", "\\data\\dir"):
        assert [h.title for h in TransferHistory.list_success_by_src(db.session, probe)] == ["目录"]


def test_list_success_by_src_recursive_matches_children_only(db):
    """
    递归模式匹配目录自身及其子项，但不能误伤同前缀的兄弟目录。

    直接用 `like("前缀%")` 会把 /data/dir2 也算成 /data/dir 的子项，
    重新整理时会把邻居目录一起搬走。
    """
    db.add(_hist("自身", src="/data/dir"),
           _hist("子项", src="/data/dir/a.mkv"),
           _hist("兄弟", src="/data/dir2/b.mkv"))

    titles = {h.title for h in TransferHistory.list_success_by_src(
        db.session, "/data/dir", recursive=True)}

    assert titles == {"自身", "子项"}


def test_list_success_by_src_escapes_like_wildcards(db):
    """
    路径中的 % 与 _ 必须被转义，否则会被当成通配符匹配到别的目录。
    """
    db.add(_hist("含下划线", src="/data/a_b/x.mkv"),
           _hist("被误伤", src="/data/axb/y.mkv"))

    titles = {h.title for h in TransferHistory.list_success_by_src(
        db.session, "/data/a_b", recursive=True)}

    assert titles == {"含下划线"}


def test_list_success_move_by_dest_only_returns_move_mode(db):
    """
    从媒体库现址发起重新整理时只认「移动」模式的记录。

    硬链接/复制的源文件仍在原处，把它们算作可回溯的移动记录会导致重复整理。
    """
    db.add(_hist("移动", src="/data/m.mkv", dest="/media/m.mkv", mode="move"),
           _hist("硬链", src="/data/l.mkv", dest="/media/l.mkv", mode="link"),
           _hist("移动失败", src="/data/f.mkv", dest="/media/f.mkv", mode="move",
                 status=False))

    titles = {h.title for h in TransferHistory.list_success_move_by_dest(db.session, "/media/m.mkv")}
    assert titles == {"移动"}
    assert TransferHistory.list_success_move_by_dest(db.session, "/media/l.mkv") == []
    assert TransferHistory.list_success_move_by_dest(db.session, "/media/f.mkv") == []


def test_list_success_move_by_dest_recursive_matches_children_only(db):
    """
    目标侧的递归匹配同样不能误伤同前缀的兄弟目录。
    """
    db.add(_hist("自身", src="/data/w1.mkv", dest="/media/show", mode="move"),
           _hist("子项", src="/data/w2.mkv", dest="/media/show/s01.mkv", mode="move"),
           _hist("兄弟", src="/data/w3.mkv", dest="/media/show2/s01.mkv", mode="move"))

    titles = {h.title for h in TransferHistory.list_success_move_by_dest(
        db.session, "/media/show", recursive=True)}

    assert titles == {"自身", "子项"}


def test_replace_by_src_keeps_one_record_per_source(db):
    """
    同一源路径在同一存储中只保留最新一条记录，且不波及其他存储。

    先删后插是在同一事务内完成的：旧的「查一条再删一条」在遗留重复数据下会留下
    脏记录，查重随后就会命中旧行。
    """
    db.add(_hist("旧一", src="/data/dup.mkv", src_storage="local"),
           _hist("别的存储", src="/data/dup.mkv", src_storage="alist"))

    TransferHistory.replace_by_src(db.session, src="/data/dup.mkv", src_storage="local",
                                   dest="/media/new.mkv", type=MediaType.TV.value,
                                   title="新记录", status=True, date="2026-08-13 12:00:00")

    local_rows = TransferHistory.list_success_by_src(db.session, "/data/dup.mkv",
                                                     storage="local")
    assert [h.title for h in local_rows] == ["新记录"]
    assert TransferHistory.get_by_src(db.session, "/data/dup.mkv",
                                      storage="alist").title == "别的存储"


def test_replace_by_src_defaults_storage_to_local(db):
    """
    未指定源存储时按 local 处理——历史数据没有这一列，缺省值必须与写入侧一致。
    """
    created = TransferHistory.replace_by_src(db.session, src="/data/nostorage.mkv",
                                             type=MediaType.TV.value, title="无存储",
                                             status=True, date="2026-08-13 12:00:00")

    assert created.src_storage == "local"


# --------------------------------------------------------------------------- #
# 按 hash / 身份查询
# --------------------------------------------------------------------------- #

def test_hash_lookups_return_single_and_all(db):
    """
    按下载 hash 既可取单条也可取全部，未命中时分别是 None 与空列表。
    """
    db.add(_hist("文件一", download_hash="th-1", src="/data/1.mkv"),
           _hist("文件二", download_hash="th-1", src="/data/2.mkv"))

    assert TransferHistory.get_by_hash(db.session, "th-1") is not None
    assert len(TransferHistory.list_by_hash(db.session, "th-1")) == 2
    assert TransferHistory.get_by_hash(db.session, "th-none") is None
    assert TransferHistory.list_by_hash(db.session, "th-none") == []


def test_get_by_media_identity_requires_matching_type(db):
    """
    按媒体身份查询时类型必须参与匹配，否则电影会命中同 ID 的剧集记录。
    """
    db.add(_hist("剧集", media_id="6100", mtype=MediaType.TV.value))

    assert TransferHistory.get_by_media_identity(
        db.session, MediaSource.TMDB, "6100", MediaType.TV.value) is not None
    assert TransferHistory.get_by_media_identity(
        db.session, MediaSource.TMDB, "6100", MediaType.MOVIE.value) is None


def test_update_download_hash_writes_only_the_target_row(db):
    """
    补写下载 hash 只影响指定的那一条记录。
    """
    target = db.add(_hist("目标", src="/data/t.mkv"))
    db.add(_hist("其他", src="/data/o.mkv"))

    TransferHistory.update_download_hash(db.session, historyid=target.id,
                                         download_hash="new-hash")

    assert TransferHistory.get_by_src(db.session, "/data/t.mkv").download_hash == "new-hash"
    assert TransferHistory.get_by_src(db.session, "/data/o.mkv").download_hash is None


@pytest.mark.parametrize("season,episode,dest,expected", [
    ("S01", "E01", "/media/s1e1.mkv", {"季集"}),
    ("S01", None, None, {"季集", "整季"}),
    (None, None, None, {"季集", "整季", "另一季"}),
])
def test_list_by_media_identity_narrows_by_season_episode_and_dest(
        db, season, episode, dest, expected):
    """
    按媒体身份查询时季、集、目标路径逐级收窄。

    收窄失效会让「这一集整理过没有」误判成整季都整理过，剩余剧集被永久跳过。
    """
    db.add(_hist("季集", src="/data/e1.mkv", seasons="S01", episodes="E01",
                 dest="/media/s1e1.mkv"),
           _hist("整季", src="/data/s1.mkv", seasons="S01", episodes=None,
                 dest="/media/s1.mkv"),
           _hist("另一季", src="/data/s2.mkv", seasons="S02", episodes=None,
                 dest="/media/s2.mkv"))

    got = TransferHistory.list_by(db.session, mtype=MediaType.TV.value,
                                  media_source=MediaSource.TMDB, media_id="6001",
                                  season=season, episode=episode, dest=dest)

    assert {h.title for h in got} == expected


def test_list_by_media_identity_uses_dest_for_movies(db):
    """
    电影没有季集，靠目标路径区分不同版本；给出目标路径即须生效。
    """
    db.add(_hist("4K 版", src="/data/m4k.mkv", mtype=MediaType.MOVIE.value,
                 media_id="6200", dest="/media/movie-4k.mkv"),
           _hist("1080 版", src="/data/m1080.mkv", mtype=MediaType.MOVIE.value,
                 media_id="6200", dest="/media/movie-1080.mkv"))

    got = TransferHistory.list_by(db.session, mtype=MediaType.MOVIE.value,
                                  media_source=MediaSource.TMDB, media_id="6200",
                                  dest="/media/movie-4k.mkv")

    assert [h.title for h in got] == ["4K 版"]


def test_list_by_falls_back_to_title_and_year(db):
    """
    没有媒体身份时退回「标题 + 年份」，服务于识别失败的历史数据。
    """
    db.add(_hist("回退标题", src="/data/fb.mkv", media_id="6300", year="2020",
                 seasons="S01"))

    assert [h.title for h in TransferHistory.list_by(
        db.session, title="回退标题", year="2020")] == ["回退标题"]
    assert [h.title for h in TransferHistory.list_by(
        db.session, title="回退标题", year="2020", season="S01")] == ["回退标题"]
    assert TransferHistory.list_by(db.session, title="回退标题", year="1999") == []


def test_list_by_supports_type_season_and_dest_prefix(db):
    """
    媒体服务器 webhook 缺少远端身份时，按「类型 + 季 + 目标路径前缀」查询。

    这是该场景下唯一能定位记录的路径，丢了会让 webhook 触发的刮削全部落空。
    """
    db.add(_hist("剧集一", src="/data/wh1.mkv", mtype=MediaType.TV.value, seasons="S01",
                 dest="/media/Show/Season 01/e01.mkv"),
           _hist("别的剧", src="/data/wh2.mkv", mtype=MediaType.TV.value, seasons="S01",
                 dest="/media/Other/Season 01/e01.mkv"))

    got = TransferHistory.list_by(db.session, mtype=MediaType.TV.value, season="S01",
                                  dest="/media/Show/")

    assert [h.title for h in got] == ["剧集一"]


def test_list_by_without_usable_criteria_returns_empty(db):
    """
    条件不足以定位时返回空列表，不能退化成返回全表。
    """
    db.add(_hist("任意"))

    assert TransferHistory.list_by(db.session) == []
    assert TransferHistory.list_by(db.session, title="只有标题") == []


# --------------------------------------------------------------------------- #
# 列表、计数与统计
# --------------------------------------------------------------------------- #

def test_list_by_page_filters_status_and_supports_unbounded_count(db):
    """
    分页可按成功状态收窄；count 为负表示不分页返回全部。

    负数这条约定是「导出全部历史」依赖的，当成普通 limit 处理会返回空。
    """
    db.add(_hist("成功一", src="/data/ok1.mkv", status=True, date="2026-08-13 10:00:01"),
           _hist("成功二", src="/data/ok2.mkv", status=True, date="2026-08-13 10:00:02"),
           _hist("失败", src="/data/ng.mkv", status=False, date="2026-08-13 10:00:03"))

    assert [h.title for h in TransferHistory.list_by_page(db.session, page=1, count=1,
                                                          status=True)] == ["成功二"]
    assert {h.title for h in TransferHistory.list_by_page(db.session, count=-1,
                                                          status=False)} == {"失败"}
    assert len(TransferHistory.list_by_page(db.session, count=-1)) >= 3


def test_list_by_title_searches_title_source_and_destination(db):
    """
    标题检索同时匹配标题、源路径与目标路径，且大小写不敏感。

    只匹配标题会让用户按文件名搜不到任何记录。
    """
    db.add(_hist("Alpha", src="/downloads/zzz.mkv", dest="/media/zzz.mkv"),
           _hist("Beta", src="/downloads/AlphaFile.mkv", dest="/media/beta.mkv"),
           _hist("Gamma", src="/downloads/g.mkv", dest="/media/alpha-dir/g.mkv"))

    titles = {h.title for h in TransferHistory.list_by_title(db.session, "alpha", count=-1)}

    assert titles == {"Alpha", "Beta", "Gamma"}


def test_list_by_title_wildcard_mode_takes_the_pattern_verbatim(db):
    """
    通配模式下调用方自带 % 通配符，不再额外包裹。
    """
    db.add(_hist("PrefixMatch", src="/downloads/p.mkv", dest="/media/p.mkv"),
           _hist("NoPrefix", src="/downloads/n.mkv", dest="/media/n.mkv"))

    titles = {h.title for h in TransferHistory.list_by_title(
        db.session, "Prefix%", count=-1, wildcard=True)}

    assert titles == {"PrefixMatch"}


def test_list_by_title_can_filter_status(db):
    """
    检索结果同样支持按成功状态收窄。
    """
    db.add(_hist("SearchOk", status=True, src="/downloads/ok.mkv"),
           _hist("SearchFail", status=False, src="/downloads/fail.mkv"))

    assert [h.title for h in TransferHistory.list_by_title(
        db.session, "SearchFail", count=-1, status=False)] == ["SearchFail"]


def test_list_by_title_matches_async_twin(db):
    """
    同步与异步的标题检索必须返回同一批记录。
    """
    db.add(_hist("ParallelSearch", src="/downloads/par.mkv"))

    sync_titles = [h.title for h in TransferHistory.list_by_title(
        db.session, "ParallelSearch", count=-1)]
    async_titles = [h.title for h in db.run_async_session(
        lambda session: TransferHistory.async_list_by_title(
            session, title="ParallelSearch", count=-1
        )
    )]

    assert sync_titles == async_titles


def test_count_and_count_by_title_match_async_twins(db):
    """
    计数与带条件计数的同步、异步结果必须一致——分页总数由它决定，
    对不上就会出现「翻到最后一页是空的」。
    """
    db.add(_hist("CountMe", status=True, src="/downloads/c1.mkv", dest="/media/c1.mkv"),
           _hist("CountMe", status=False, src="/downloads/c2.mkv", dest="/media/c2.mkv"))

    assert TransferHistory.count(db.session) == db.run_async_session(
        TransferHistory.async_count
    )
    assert TransferHistory.count(db.session, status=True) == \
        db.run_async_session(
            lambda session: TransferHistory.async_count(session, status=True)
        )
    assert TransferHistory.count_by_title(db.session, "CountMe") == 2
    assert TransferHistory.count_by_title(db.session, "CountMe", status=False) == 1
    assert TransferHistory.count_by_title(db.session, "CountMe") == \
        db.run_async_session(
            lambda session: TransferHistory.async_count_by_title(
                session, title="CountMe"
            )
        )


def test_statistic_groups_by_day_within_the_window(db):
    """
    统计按日期分组，且只统计窗口内的记录。
    """
    today = _time.strftime("%Y-%m-%d", _time.localtime())
    db.add(_hist("今天一", src="/data/t1.mkv", date=f"{today} 10:00:00"),
           _hist("今天二", src="/data/t2.mkv", date=f"{today} 11:00:00"),
           _hist("很久以前", src="/data/t3.mkv", date="2000-01-01 10:00:00"))

    rows = dict(TransferHistory.statistic(db.session, days=7))

    assert rows.get(today, 0) >= 2
    assert "2000-01-01" not in rows


def test_statistic_includes_the_window_start_boundary(db, frozen_now):
    """
    统计窗口是闭区间起点（``date >= 起点``），正好落在起点的记录必须计入，同步异步一致。

    起点由「调用时刻 - N 天」现算，不冻结时钟就摆不到边界上；上面那条用例用的是「今天」
    与 2000 年两个极端值，起点比较符改成 ``>`` 照样绿。
    """
    now = frozen_now(transferhistory_module)
    window_start = _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime(now - 86400 * 7))
    boundary_day = window_start[:10]
    db.add(_hist("窗口起点上", src="/data/bstat.mkv", date=window_start))

    rows = dict(TransferHistory.statistic(db.session, days=7))
    async_rows = dict(db.run_async_session(
        lambda session: TransferHistory.async_statistic(session, days=7)
    ))

    assert rows.get(boundary_day, 0) == 1
    assert async_rows.get(boundary_day, 0) == 1


def test_list_by_date_returns_newest_first(db):
    """
    按时间查询返回该时间之后的记录，按主键倒序。
    """
    db.add(_hist("旧", src="/data/od.mkv", date="2026-08-01 10:00:00"),
           _hist("新", src="/data/nd.mkv", date="2026-08-12 10:00:00"))

    titles = [h.title for h in TransferHistory.list_by_date(db.session, "2026-08-05")]

    assert titles == ["新"]


def test_list_by_date_excludes_the_row_exactly_at_the_boundary(db):
    """
    取的是「该时刻之后」的记录（``date > date``），正好等于该时刻的那条不算在内。

    上面那条用例两侧数据各离边界四天与七天，比较符放宽成 ``>=`` 一样绿；
    这里把行压在边界上，让开闭区间之差可观测。
    """
    boundary = "2026-08-05 00:00:00"
    db.add(_hist("边界上", src="/data/bd0.mkv", date=boundary),
           _hist("边界后一秒", src="/data/bd1.mkv", date="2026-08-05 00:00:01"))

    titles = [h.title for h in TransferHistory.list_by_date(db.session, boundary)]

    assert titles == ["边界后一秒"]


def test_delete_before_is_batched_and_keeps_recent(db):
    """
    历史清理分批执行且不碰保留期内的记录。
    """
    for index in range(4):
        db.add(_hist(f"old-{index}", src=f"/data/old{index}.mkv",
                     date=f"2026-01-01 10:00:0{index}"))
    db.add(_hist("recent", src="/data/recent.mkv", date="2026-08-13 10:00:00"))

    assert TransferHistory.delete_before(db.session, before_time="2026-08-01", limit=2) == 2
    assert TransferHistory.delete_before(db.session, before_time="2026-08-01", limit=100) == 2
    assert TransferHistory.delete_before(db.session, before_time="2026-08-01", limit=100) == 0

    assert TransferHistory.get_by_src(db.session, "/data/recent.mkv") is not None


def test_delete_before_keeps_the_row_exactly_at_the_boundary(db):
    """
    保留时间点上的整理历史属于「保留期内」，不能被清理（``date < before_time``）。

    整理历史被删掉就等于丢失溯源，同一文件会被重新整理一次；
    上面那条用例的数据离水位半年，``<`` 写成 ``<=`` 完全不可观测。
    """
    boundary = "2026-05-01 00:00:00"
    db.add(_hist("边界上", src="/data/bdel0.mkv", date=boundary),
           _hist("边界前一秒", src="/data/bdel1.mkv", date="2026-04-30 23:59:59"))

    assert TransferHistory.delete_before(db.session, before_time=boundary, limit=100) == 1

    assert TransferHistory.get_by_src(db.session, "/data/bdel0.mkv") is not None
    assert TransferHistory.get_by_src(db.session, "/data/bdel1.mkv") is None
