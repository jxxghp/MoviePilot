"""
已有下载任务受理决策、MP 暂停标签与"确认选中过的目标"记录的纯规则回归。

下载器模块上的端到端行为见 ``tests/test_episode_file_selection.py``；这里覆盖规则本身的边界。
"""

import pytest

from app.domain import torrent as torrent_rules
from app.domain.torrent import (
    ConfirmedSelection,
    ExistingTaskFile,
    ExistingTaskSnapshot,
    ExistingTaskState,
    PauseMark,
)
from app.schemas.download import (
    DownloadPauseIntent,
    DownloadTaskAction,
    DownloadTaskActor,
    ExistingTaskRejection,
)

_MEDIA = [".mkv", ".mp4"]


def _snapshot(*files, state=ExistingTaskState.RUNNING, tags=()):
    """
    构造已有任务快照；files 为 (名称, 已选中, 已完成) 三元组，按顺序编号。
    """
    return ExistingTaskSnapshot(
        files=tuple(ExistingTaskFile(index, name, wanted, completed) for index, (name, wanted, completed) in enumerate(files)),
        state=state,
        tags=None if tags is None else tuple(tags),
    )


def _decide(snapshot, episodes=None, confirmed=None):
    """
    以统一的视频后缀调用决策。
    """
    return torrent_rules.decide_existing_task(snapshot, episodes=episodes, confirmed=confirmed, media_extensions=_MEDIA)


@pytest.mark.parametrize("mark,tag", [
    (PauseMark(DownloadTaskActor.SYSTEM, DownloadPauseIntent.RESUMABLE), "MP暂停:可恢复:系统"),
    (PauseMark(DownloadTaskActor.USER), "MP暂停:放弃:用户"),
    (PauseMark(DownloadTaskActor.USER, DownloadPauseIntent.DEFER), "MP暂停:暂缓:用户"),
    (PauseMark(DownloadTaskActor.PLUGIN, DownloadPauseIntent.ABANDON, "AutoClean"), "MP暂停:放弃:插件:AutoClean"),
    (PauseMark(DownloadTaskActor.PLUGIN, DownloadPauseIntent.DEFER, "NightOnly"), "MP暂停:暂缓:插件:NightOnly"),
    (PauseMark(DownloadTaskActor.PLUGIN, DownloadPauseIntent.RESUMABLE, "Saver"), "MP暂停:可恢复:插件:Saver"),
])
def test_pause_tag_round_trips(mark, tag):
    """
    暂停标签可以生成并原样解析回暂停意图与暂停方；任何暂停方都可以声明任何意图。
    """
    assert torrent_rules.pause_tag(mark) == tag
    assert torrent_rules.is_pause_tag(tag)
    assert torrent_rules.parse_pause_mark(["MOVIEPILOT", tag]) == mark


@pytest.mark.parametrize("mark", [
    PauseMark(DownloadTaskActor.EXTERNAL),
    PauseMark(DownloadTaskActor.PLUGIN, plugin_id=None),
    PauseMark(DownloadTaskActor.PLUGIN, plugin_id="a,b"),
    PauseMark(DownloadTaskActor.PLUGIN, plugin_id="a:b"),
])
def test_pause_tag_rejects_external_and_invalid_plugin_ids(mark):
    """
    外部暂停没有标签；插件 ID 缺失或含分隔符时拒绝生成，避免写出无法解析的标签。
    """
    with pytest.raises(ValueError):
        torrent_rules.pause_tag(mark)


def test_most_conservative_pause_mark_wins():
    """
    同时存在多个暂停标签时取最保守的意图：放弃 > 暂缓 > 可恢复，宁可不恢复也不推翻别人的决定。
    """
    assert torrent_rules.parse_pause_mark(["MP暂停:可恢复:系统", "MP暂停:暂缓:插件:A"]).intent is DownloadPauseIntent.DEFER
    assert torrent_rules.parse_pause_mark(["MP暂停:暂缓:插件:A", "MP暂停:放弃:用户"]).actor is DownloadTaskActor.USER
    assert torrent_rules.parse_pause_mark(
        ["MP暂停", "MP暂停:未知:插件:A", "MP暂停:放弃:插件", "MP暂停:放弃:用户:多余", "其他"],
    ) is None


@pytest.mark.parametrize("mark,expected", [
    (PauseMark(DownloadTaskActor.USER, DownloadPauseIntent.ABANDON), ExistingTaskRejection.PAUSED_ABANDONED),
    (PauseMark(DownloadTaskActor.PLUGIN, DownloadPauseIntent.ABANDON, "AutoClean"), ExistingTaskRejection.PAUSED_ABANDONED),
    (PauseMark(DownloadTaskActor.USER, DownloadPauseIntent.DEFER), DownloadTaskAction.DEFERRED),
    (PauseMark(DownloadTaskActor.PLUGIN, DownloadPauseIntent.DEFER, "NightOnly"), DownloadTaskAction.DEFERRED),
    (PauseMark(DownloadTaskActor.SYSTEM, DownloadPauseIntent.RESUMABLE), DownloadTaskAction.APPENDED),
    (PauseMark(DownloadTaskActor.PLUGIN, DownloadPauseIntent.RESUMABLE, "Saver"), DownloadTaskAction.APPENDED),
])
def test_paused_task_handling_depends_only_on_intent(mark, expected):
    """
    已暂停的任务怎么处理只看意图、不看是谁暂停的：插件声明放弃与用户暂停一样换种子；
    任何暂停方声明可恢复，MP 都会在需要时恢复。
    """
    snapshot = _snapshot(
        ("S01E01.mkv", True, True), ("S01E02.mkv", False, False),
        state=ExistingTaskState.PAUSED, tags=[torrent_rules.pause_tag(mark)],
    )

    decision = _decide(snapshot, episodes={2})

    if isinstance(expected, ExistingTaskRejection):
        assert decision.rejection is expected
        assert decision.enable == () and decision.resume is False
    else:
        assert decision.action is expected
        assert decision.enable == (1,)
        assert decision.resume is (mark.intent is DownloadPauseIntent.RESUMABLE)


def test_confirmed_selection_note_round_trip_and_legacy_notes():
    """
    确认记录写入下载历史 note 后可读回；升级前的 note 或格式不符时视为空记录。
    """
    merged = ConfirmedSelection.from_request({5}).merge(ConfirmedSelection.from_request({6}))
    note = {"source": "Subscribe|{}", torrent_rules.CONFIRMED_SELECTION_NOTE_KEY: merged.to_note()}

    assert ConfirmedSelection.from_note(note) == ConfirmedSelection(episodes=frozenset({5, 6}))
    assert ConfirmedSelection.from_request(None) == ConfirmedSelection(whole=True)
    assert ConfirmedSelection.from_note({"source": "旧版本"}) == ConfirmedSelection()
    assert ConfirmedSelection.from_note(None) == ConfirmedSelection()
    assert ConfirmedSelection.from_note({torrent_rules.CONFIRMED_SELECTION_NOTE_KEY: {"episodes": ["5", True]}}) == ConfirmedSelection()


def test_external_pause_after_seeding_is_not_resumed():
    """
    做种完成后被下载器分享率规则停掉的任务也没有 MP 标签：追加新集时不恢复，按外部暂停拒绝。
    """
    snapshot = _snapshot(("S01E01.mkv", True, True), ("S01E02.mkv", False, False), state=ExistingTaskState.PAUSED)

    decision = _decide(snapshot, episodes={2})

    assert decision.rejection is ExistingTaskRejection.PAUSED_EXTERNALLY
    assert not decision.rejection.is_resource_level
    assert decision.enable == () and decision.resume is False


def test_cancelled_attachment_is_skipped_silently_but_never_reselected():
    """
    曾被确认选中、后被取消的非视频附属文件不重新选中，也不因此拒绝；视频照常追加。
    """
    snapshot = _snapshot(("S01E01.mkv", True, True), ("S01E01.ass", False, True), ("S01E02.mkv", False, False))

    decision = _decide(snapshot, episodes={1, 2}, confirmed=ConfirmedSelection(episodes=frozenset({1})))

    assert decision.action is DownloadTaskAction.APPENDED
    assert decision.enable == (2,)


@pytest.mark.parametrize("cancelled", [{0, 1}, {1}])
def test_cancelled_tracks_of_audio_only_torrent_are_rejected(cancelled):
    """
    没有视频的种子（例如音乐专辑）以全部文件为主文件：曾确认选中的音轨被取消后按取消拒绝，
    不能因为"没有被取消的视频"而把缺轨判成复用成功。
    """
    files = [(f"{index:02d}.flac", index not in cancelled, index not in cancelled) for index in range(2)]

    decision = _decide(_snapshot(*files), confirmed=ConfirmedSelection(whole=True))

    assert decision.rejection is ExistingTaskRejection.FILES_CANCELLED
    assert decision.enable == ()


def test_cancelled_non_video_files_beside_videos_stay_attachments():
    """
    有视频的种子里，被取消的音频、字幕等非视频文件仍按附属文件静默跳过，不影响整包复用。
    """
    snapshot = _snapshot(("Movie.mkv", True, True), ("Movie.Commentary.flac", False, False))

    decision = _decide(snapshot, confirmed=ConfirmedSelection(whole=True))

    assert decision.action is DownloadTaskAction.REUSED
    assert decision.enable == ()


def test_unknown_tags_and_states_are_never_guessed():
    """
    标签读不到时一律拒绝；状态读不到时只有在仍需下载时才拒绝（目标已完成不需要知道状态）。
    """
    files = (("S01E01.mkv", True, False),)

    assert _decide(_snapshot(*files, tags=None), episodes={1}).rejection is ExistingTaskRejection.STATE_UNAVAILABLE
    assert _decide(_snapshot(*files, state=ExistingTaskState.UNKNOWN), episodes={1}).rejection is (
        ExistingTaskRejection.STATE_UNAVAILABLE
    )
    done = _snapshot(("S01E01.mkv", True, True), state=ExistingTaskState.UNKNOWN)
    assert _decide(done, episodes={1}).action is DownloadTaskAction.REUSED


def test_error_state_is_rejected_even_when_target_is_complete():
    """
    下载器报告出错（例如文件缺失）时进度不可信，即使显示已完成也拒绝。
    """
    snapshot = _snapshot(("S01E01.mkv", True, True), state=ExistingTaskState.ERROR)

    decision = _decide(snapshot, episodes={1})

    assert decision.rejection is ExistingTaskRejection.TASK_ERROR
    assert decision.rejection.is_resource_level


def test_rejection_messages_are_recognized_by_prefix():
    """
    下载器返回的失败说明以原因开头，下载链据此识别，不依赖细节文本。
    """
    message = ExistingTaskRejection.FILES_CANCELLED.describe("abc", "目标视频已被删除")

    assert ExistingTaskRejection.from_message(message) is ExistingTaskRejection.FILES_CANCELLED
    assert ExistingTaskRejection.from_message("添加种子任务失败：已有任务的文件") is None
    assert ExistingTaskRejection.from_message(None) is None
