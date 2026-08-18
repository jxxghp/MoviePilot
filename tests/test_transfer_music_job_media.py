from app import schemas
from app.application.transfer import TransferTask
from app.application.orchestration.transfer import JobManager
from app.domain.meta.metamusic import MetaMusic
from app.schemas import FileItem


def _music_task() -> TransferTask:
    """构造未识别出媒体信息的音乐整理任务，复现 #6325 场景。"""
    meta = MetaMusic(
        org_string="歌手 - 歌曲名 (1998) [FLAC]",
        title="歌曲名",
        artists=["歌手"],
        album="专辑名",
        year=1998,
        track_number=3,
    )
    fileitem = FileItem(
        storage="local",
        path="/music/歌手/歌曲名.flac",
        type="file",
        name="歌曲名.flac",
        basename="歌曲名",
        extension="flac",
    )
    return TransferTask(fileitem=fileitem, meta=meta)


def test_add_music_task_without_mediainfo_builds_music_info():
    """音乐任务识别失败登记作业时应按 MusicInfo 兜底，而不是把 int 年份塞进 MediaInfo。"""
    jobview = JobManager()
    task = _music_task()

    # 修复前这里会抛 pydantic 校验异常：MediaInfo.year 期望 str 而音乐年份是 int
    assert jobview.add_task(task)

    jobs = list(jobview._job_view.values())
    assert len(jobs) == 1
    media = jobs[0].media
    assert isinstance(media, schemas.MusicInfo)
    # MetaMusic.name 优先返回专辑名，作为作业展示标题
    assert media.title == "专辑名"
    assert media.year == 1998
    assert media.artists == ["歌手"]
    assert media.album == "专辑名"
    assert media.title_year == "专辑名 (1998)"


def test_add_music_task_without_year_keeps_title():
    """无年份的音乐兜底展示不应拼接空年份。"""
    task = _music_task()
    task.meta = MetaMusic(org_string="歌曲名", title="歌曲名")

    jobview = JobManager()
    assert jobview.add_task(task)

    media = next(iter(jobview._job_view.values())).media
    assert isinstance(media, schemas.MusicInfo)
    assert media.year is None
    assert media.title_year == "歌曲名"
