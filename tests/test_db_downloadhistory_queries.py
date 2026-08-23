"""
下载历史与下载文件记录的查询行为。

下载历史是「这个种子是为哪部片下的」的唯一来源，整理链靠它把文件落到正确的媒体库
目录；查错一条就是整理到错误的剧里。下载文件记录则决定「删种时该删哪些文件」，
条件写宽会误删别的任务的文件。
"""
import asyncio
import time as _time

import pytest

from app.db.models import downloadhistory as downloadhistory_module
from app.db.models.downloadhistory import DownloadFiles, DownloadHistory
from app.schemas.types import MediaSource, MediaType

TMDB = str(MediaSource.TMDB)


@pytest.fixture(autouse=True)
def _track(db):
    """把下载历史与下载文件表纳入用例级回收。"""
    db.watermark(DownloadHistory, DownloadFiles)


def _history(title: str, download_hash: str = None, media_id: str = "7001",
             mtype: str = None, year: str = "2026", seasons: str = None,
             episodes: str = None, date: str = "2026-08-13 10:00:00",
             username: str = "alice", music_type: str = None,
             path: str = None) -> DownloadHistory:
    """构造一条下载历史。"""
    return DownloadHistory(path=path or f"/downloads/{title}", type=mtype or MediaType.TV.value,
                           title=title, year=year, media_source=TMDB, media_id=media_id,
                           music_type=music_type, seasons=seasons, episodes=episodes,
                           download_hash=download_hash, date=date, username=username)


def _file(download_hash: str, fullpath: str, savepath: str = "/downloads",
          state: int = 1) -> DownloadFiles:
    """构造一条下载文件记录。"""
    return DownloadFiles(downloader="qbittorrent", download_hash=download_hash,
                         fullpath=fullpath, savepath=savepath,
                         filepath=fullpath.rsplit("/", 1)[-1],
                         torrentname="种子", state=state)


# --------------------------------------------------------------------------- #
# DownloadHistory：按 hash 查询
# --------------------------------------------------------------------------- #

def test_get_by_hash_returns_the_latest_record(db):
    """
    同一 hash 存在多条时取最新的一条。

    重复下载会留下多条历史，取到旧的那条会让整理用上过期的识别结果。
    """
    db.add(_history("旧记录", download_hash="h-1", date="2026-08-01 10:00:00"),
           _history("新记录", download_hash="h-1", date="2026-08-12 10:00:00"))

    assert DownloadHistory.get_by_hash(db.session, "h-1").title == "新记录"
    assert DownloadHistory.get_by_hash(db.session, "h-missing") is None


def test_get_by_hashes_keeps_request_order_and_dedupes(db):
    """
    批量查询按请求顺序返回、去重、跳过查不到的 hash，每个 hash 只给最新一条。

    这个方法存在的理由就是消除 N+1；返回顺序与入参不一致会让上层错位地把
    A 任务的历史贴到 B 任务上。
    """
    db.add(_history("A 旧", download_hash="h-a", date="2026-08-01 10:00:00"),
           _history("A 新", download_hash="h-a", date="2026-08-12 10:00:00"),
           _history("B", download_hash="h-b", date="2026-08-05 10:00:00"))

    got = DownloadHistory.get_by_hashes(db.session, ["h-b", "h-a", "h-a", "", "h-missing"])

    assert [h.title for h in got] == ["B", "A 新"]
    assert DownloadHistory.get_by_hashes(db.session, []) == []
    assert DownloadHistory.get_by_hashes(db.session, None) == []


# --------------------------------------------------------------------------- #
# DownloadHistory：身份与列表查询
# --------------------------------------------------------------------------- #

def test_get_by_media_identity_optionally_narrows_by_music_type(db):
    """
    按媒体身份查询时音乐实体类型可选，给出即须生效；空身份短路成空列表。
    """
    db.add(_history("单曲", media_id="mb-1", music_type="recording"),
           _history("专辑", media_id="mb-1", music_type="album"))

    assert len(DownloadHistory.get_by_media_identity(db.session, MediaSource.TMDB, "mb-1")) == 2
    assert [h.title for h in DownloadHistory.get_by_media_identity(
        db.session, MediaSource.TMDB, "mb-1", music_type="album")] == ["专辑"]
    assert DownloadHistory.get_by_media_identity(db.session, MediaSource.TMDB, "  ") == []
    assert DownloadHistory.get_by_media_identity(db.session, None, "mb-1") == []


def test_list_by_page_is_newest_first_and_paged(db):
    """
    历史列表按时间倒序、同时间按主键倒序分页，并与异步孪生方法一致。
    """
    for index in range(4):
        db.add(_history(f"p-{index}", date=f"2026-08-13 10:00:0{index}"))

    page1 = DownloadHistory.list_by_page(db.session, page=1, count=2)
    assert [h.title for h in page1] == ["p-3", "p-2"]
    assert [h.title for h in DownloadHistory.list_by_page(db.session, page=2, count=2)] == \
        ["p-1", "p-0"]
    assert [h.title for h in db.run_async_session(
        lambda session: DownloadHistory.async_list_by_page(
            session, page=1, count=2
        )
    )] == ["p-3", "p-2"]


def test_get_by_path_finds_the_download_directory(db):
    """
    按保存路径查询用于把落地文件反查回下载任务，查不到时返回 None。
    """
    db.add(_history("有路径", path="/downloads/unique-path"))

    assert DownloadHistory.get_by_path(db.session, "/downloads/unique-path").title == "有路径"
    assert DownloadHistory.get_by_path(db.session, "/downloads/nope") is None


@pytest.mark.parametrize("season,episode,expected", [
    ("S01", "E01", ["季集精确"]),
    ("S01", None, ["季集精确", "整季"]),
    (None, None, ["季集精确", "整季", "另一季"]),
])
def test_get_last_by_media_identity_narrows_by_season_and_episode(db, season, episode, expected):
    """
    按媒体身份查询时，季与集逐级收窄。

    收窄失效会让「这一集下过没有」误判成整季都下过，订阅直接跳过后续剧集。
    """
    db.add(_history("季集精确", seasons="S01", episodes="E01"),
           _history("整季", seasons="S01", episodes=None),
           _history("另一季", seasons="S02", episodes=None))

    got = DownloadHistory.get_last_by(db.session, mtype=MediaType.TV.value,
                                      media_source=MediaSource.TMDB, media_id="7001",
                                      season=season, episode=episode)

    assert sorted(h.title for h in got) == sorted(expected)


@pytest.mark.parametrize("season,episode,expected", [
    ("S01", "E01", ["标题季集"]),
    ("S01", None, ["标题季集", "标题整季"]),
    (None, None, ["标题季集", "标题整季"]),
])
def test_get_last_by_falls_back_to_title_and_year(db, season, episode, expected):
    """
    没有媒体身份时退回「标题 + 年份」查询，同样支持季集收窄。

    这条回退路径服务于识别失败的历史数据，丢了会让这些记录彻底查不到。
    """
    db.add(_history("标题季集", media_id="7900", seasons="S01", episodes="E01", year="2020"),
           _history("标题整季", media_id="7901", seasons="S01", episodes=None, year="2020"))

    got = DownloadHistory.get_last_by(db.session, title="标题季集", year="2020",
                                      season=season, episode=episode)
    got += DownloadHistory.get_last_by(db.session, title="标题整季", year="2020",
                                       season=season, episode=episode)

    assert sorted(h.title for h in got) == sorted(expected)


def test_get_last_by_without_any_identity_returns_empty(db):
    """
    既无媒体身份也无标题年份时返回空列表，不能退化成返回全表。
    """
    db.add(_history("任意"))

    assert DownloadHistory.get_last_by(db.session) == []
    assert DownloadHistory.get_last_by(db.session, title="只有标题") == []


def test_list_by_user_date_scopes_to_owner(db):
    """
    按用户与时间查询必须限定用户名；不给用户名则跨用户返回。
    """
    db.add(_history("alice 的", username="alice", date="2026-08-01 10:00:00"),
           _history("bob 的", username="bob", date="2026-08-01 10:00:00"),
           _history("太新", username="alice", date="2026-08-20 10:00:00"))

    mine = DownloadHistory.list_by_user_date(db.session, "2026-08-10", username="alice")
    assert [h.title for h in mine] == ["alice 的"]

    everyone = DownloadHistory.list_by_user_date(db.session, "2026-08-10")
    assert {h.title for h in everyone} >= {"alice 的", "bob 的"}


def test_list_by_user_date_excludes_the_row_exactly_at_the_boundary(db):
    """
    取的是「该时刻之前」的历史（``date < date``），正好等于该时刻的那条不算在内。

    上面的用例数据离查询时刻有十天之遥，比较符放宽成 ``<=`` 也照样绿；
    这里把行摆在边界上，让开闭区间之差可观测。
    """
    boundary = "2026-08-10 00:00:00"
    db.add(_history("边界上", username="carol", date=boundary),
           _history("边界前一秒", username="carol", date="2026-08-09 23:59:59"))

    rows = DownloadHistory.list_by_user_date(db.session, boundary, username="carol")

    assert [h.title for h in rows] == ["边界前一秒"]


def test_list_by_date_optionally_narrows_by_season(db):
    """
    按时间与媒体身份查询时季号可选，给出即须生效。
    """
    db.add(_history("第一季", seasons="S01", date="2026-08-12 10:00:00"),
           _history("第二季", seasons="S02", date="2026-08-12 10:00:00"),
           _history("太旧", seasons="S01", date="2026-01-01 10:00:00"))

    scoped = DownloadHistory.list_by_date(db.session, "2026-08-01", MediaType.TV.value,
                                          MediaSource.TMDB, "7001", seasons="S01")
    assert [h.title for h in scoped] == ["第一季"]

    both = DownloadHistory.list_by_date(db.session, "2026-08-01", MediaType.TV.value,
                                        MediaSource.TMDB, "7001")
    assert {h.title for h in both} == {"第一季", "第二季"}


def test_list_by_date_excludes_the_row_exactly_at_the_boundary(db):
    """
    取的是「该时刻之后」的历史（``date > date``），正好等于该时刻的那条不算在内。

    这个查询用于判断某媒体近期是否已下载过，边界放宽成 ``>=`` 会把上一轮刚好压线的
    记录算成「已下过」，从而误跳过一次下载；两侧数据都离边界很远时看不出来。
    """
    boundary = "2026-08-01 00:00:00"
    db.add(_history("边界上", media_id="7011", date=boundary),
           _history("边界后一秒", media_id="7011", date="2026-08-01 00:00:01"))

    rows = DownloadHistory.list_by_date(db.session, boundary, MediaType.TV.value,
                                        MediaSource.TMDB, "7011")

    assert [h.title for h in rows] == ["边界后一秒"]


def test_list_by_type_only_returns_recent_days(db):
    """
    按类型取最近 N 天，超出窗口的不返回——否则首页统计会把全量历史拉出来。
    """
    db.add(_history("最近", date="2099-01-01 00:00:00"),
           _history("很久以前", date="2000-01-01 00:00:00"))

    names = {h.title for h in DownloadHistory.list_by_type(db.session, MediaType.TV.value, days=7)}

    assert "最近" in names and "很久以前" not in names


def test_list_by_type_includes_the_window_start_boundary(db, frozen_now):
    """
    时间窗是闭区间起点（``date >= 起点``），正好落在起点的那条必须在结果里。

    窗口起点由「调用时刻 - N 天」现算，不冻结时钟就摆不到边界上；上面那条用例用的是
    2099/2000 两个极端值，比较符改成 ``>`` 也照样绿。
    """
    now = frozen_now(downloadhistory_module)
    window_start = _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime(now - 86400 * 7))
    one_second_earlier = _time.strftime("%Y-%m-%d %H:%M:%S",
                                        _time.localtime(now - 86400 * 7 - 1))
    db.add(_history("窗口起点上", media_id="7012", date=window_start),
           _history("窗口起点前一秒", media_id="7012", date=one_second_earlier))

    names = {h.title for h in DownloadHistory.list_by_type(db.session, MediaType.TV.value, days=7)}

    assert "窗口起点上" in names
    assert "窗口起点前一秒" not in names


def test_delete_before_is_batched_and_keeps_recent(db):
    """
    历史清理分批执行且不碰保留期内的记录。
    """
    for index in range(4):
        db.add(_history(f"old-{index}", date=f"2026-01-01 10:00:0{index}"))
    db.add(_history("recent", date="2026-08-13 10:00:00"))

    assert DownloadHistory.delete_before(db.session, before_time="2026-08-01", limit=2) == 2
    assert DownloadHistory.delete_before(db.session, before_time="2026-08-01", limit=100) == 2
    assert DownloadHistory.delete_before(db.session, before_time="2026-08-01", limit=100) == 0

    assert DownloadHistory.list_by_page(db.session, page=1, count=1)[0].title == "recent"


def test_delete_before_keeps_the_row_exactly_at_the_boundary(db):
    """
    保留时间点上的历史属于「保留期内」，不能被清理（``date < before_time``）。

    上面那条用例的数据离水位有半年，``<`` 写成 ``<=`` 也不可观测；
    这里把行压在水位上，让开闭区间之差暴露出来。
    """
    boundary = "2026-05-01 00:00:00"
    at_boundary = db.add(_history("边界上", media_id="7013", date=boundary))
    db.add(_history("边界前一秒", media_id="7013", date="2026-04-30 23:59:59"))

    assert DownloadHistory.delete_before(db.session, before_time=boundary, limit=100) == 1

    assert db.session.get(DownloadHistory, at_boundary.id) is not None


def test_count_and_title_search_match_async_twins(db):
    """
    异步的总数与标题检索必须与已落库的数据一致。

    标题检索走大小写不敏感匹配，退化成精确匹配会让搜索框形同虚设。
    """
    db.add(_history("Unique Title Here", date="2026-08-13 10:00:00"))

    assert db.run_async_session(DownloadHistory.async_count) >= 1
    assert db.run_async_session(
        lambda session: DownloadHistory.async_count_by_title(
            session, title="unique title"
        )
    ) == 1
    assert [h.title for h in db.run_async_session(
        lambda session: DownloadHistory.async_list_by_title(
            session, title="UNIQUE TITLE"
        )
    )] == ["Unique Title Here"]


# --------------------------------------------------------------------------- #
# DownloadFiles
# --------------------------------------------------------------------------- #

def test_files_get_by_hash_optionally_filters_state(db):
    """
    按 hash 取文件时状态可选：不给返回全部，给出则只返回该状态。

    删种时取的是「状态正常」这一批，条件失效会把已删除的文件重复删一遍。
    """
    db.add(_file("fh-1", "/downloads/a.mkv", state=1),
           _file("fh-1", "/downloads/b.mkv", state=0),
           _file("fh-2", "/downloads/c.mkv", state=1))

    assert len(DownloadFiles.get_by_hash(db.session, "fh-1")) == 2
    assert [f.fullpath for f in DownloadFiles.get_by_hash(db.session, "fh-1", state=1)] == \
        ["/downloads/a.mkv"]


def test_files_get_by_fullpath_returns_newest_or_all(db):
    """
    按完整路径查询默认取最新一条，要求全部时按主键倒序返回。

    同一路径可能被多次下载覆盖，取到旧记录会关联到错误的下载任务。
    """
    first = db.add(_file("fh-3", "/downloads/same.mkv"))
    second = db.add(_file("fh-4", "/downloads/same.mkv"))

    assert DownloadFiles.get_by_fullpath(db.session, "/downloads/same.mkv").id == second.id
    assert [f.id for f in DownloadFiles.get_by_fullpath(
        db.session, "/downloads/same.mkv", all_files=True)] == [second.id, first.id]
    assert DownloadFiles.get_by_fullpath(db.session, "/downloads/none.mkv") is None


def test_files_get_by_savepath_returns_every_file_of_that_directory(db):
    """
    按保存目录查询返回该目录下的全部文件记录。
    """
    db.add(_file("fh-5", "/downloads/dir/a.mkv", savepath="/downloads/dir"),
           _file("fh-5", "/downloads/dir/b.mkv", savepath="/downloads/dir"),
           _file("fh-6", "/downloads/other/c.mkv", savepath="/downloads/other"))

    assert len(DownloadFiles.get_by_savepath(db.session, "/downloads/dir")) == 2


def test_files_delete_by_fullpath_marks_state_instead_of_removing(db):
    """
    「删除」是把状态置 0 而不是删行，并且只影响状态正常的那条。

    保留行是为了让后续整理仍能追溯文件来源；直接删行会让历史断链。
    """
    db.add(_file("fh-7", "/downloads/del.mkv", state=1),
           _file("fh-8", "/downloads/keep.mkv", state=1))

    DownloadFiles.delete_by_fullpath(db.session, "/downloads/del.mkv")

    assert DownloadFiles.get_by_fullpath(db.session, "/downloads/del.mkv").state == 0
    assert DownloadFiles.get_by_fullpath(db.session, "/downloads/keep.mkv").state == 1


def test_files_delete_orphans_only_removes_records_without_parent(db):
    """
    孤儿清理只删掉找不到父下载历史的文件记录，且分批执行。

    条件写反会把仍有父记录的文件删光，删种时便再也找不到要删哪些文件。
    """
    db.add(_history("有父记录", download_hash="fh-parent"))
    db.add(_file("fh-parent", "/downloads/child.mkv"),
           _file("fh-orphan-1", "/downloads/o1.mkv"),
           _file("fh-orphan-2", "/downloads/o2.mkv"))

    assert DownloadFiles.delete_orphans(db.session, limit=1) == 1
    assert DownloadFiles.delete_orphans(db.session, limit=100) == 1
    assert DownloadFiles.delete_orphans(db.session, limit=100) == 0

    assert DownloadFiles.get_by_fullpath(db.session, "/downloads/child.mkv") is not None
