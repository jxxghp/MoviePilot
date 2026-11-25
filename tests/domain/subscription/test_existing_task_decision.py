"""
已有下载任务受理决策与"MP 确认选中过的文件"记录的纯规则回归。

下载器模块上的端到端行为见 ``tests/integration/download/test_episode_file_selection.py``；这里覆盖规则本身的边界。
"""

import pytest

from app.domain import torrent as torrent_rules
from app.domain.torrent import (
    ConfirmedSelection,
    EpisodeSelection,
    ExistingTaskFile,
    ExistingTaskSnapshot,
    ExistingTaskState,
)
from app.schemas.download import DownloadTaskAction, ExistingTaskRejection

_MEDIA = [".mkv", ".mp4"]


def _snapshot(*files, state=ExistingTaskState.RUNNING):
    """
    构造已有任务快照；files 为 (名称, 已选中, 已完成) 三元组，按顺序编号。
    """
    return ExistingTaskSnapshot(
        files=tuple(
            ExistingTaskFile(index, name, wanted, completed)
            for index, (name, wanted, completed) in enumerate(files)
        ),
        state=state,
    )


def _decide(snapshot, episodes=None, confirmed=None, **kwargs):
    """
    以统一的视频后缀调用决策。
    """
    return torrent_rules.decide_existing_task(
        snapshot, episodes=episodes, confirmed=confirmed, media_extensions=_MEDIA, **kwargs,
    )


def _confirmed(*indexes):
    """
    构造确认记录里某台下载器的文件索引集合。
    """
    return frozenset(indexes)


def test_paused_task_is_rejected_whoever_paused_it():
    """
    自动流程遇到任务已暂停、目标还没下完时一律拒绝且不改动：不论是谁暂停的，MP 都不恢复，也不往里追加。
    """
    snapshot = _snapshot(("S01E01.mkv", True, True), ("S01E02.mkv", False, False), state=ExistingTaskState.PAUSED)

    decision = _decide(snapshot, episodes={2})

    assert decision.rejection is ExistingTaskRejection.PAUSED
    assert decision.enable == () and decision.required == ()
    # 暂停方恢复后就能接着用，只短冷却，且冷却作用于整个任务。
    assert not decision.rejection.is_resource_level
    assert not decision.rejection.is_target_specific


@pytest.mark.parametrize("target_selected,action,enable", [
    (False, DownloadTaskAction.APPENDED, (1,)),
    (True, DownloadTaskAction.RESUMED, ()),
])
def test_user_specified_torrent_resumes_paused_task(target_selected, action, enable):
    """
    用户手动指定了这个种子：这次的明确要求比之前的暂停更新，追加目标（如需）后恢复任务；
    目标已选中只是没下完时只恢复，按"恢复"结算。
    """
    snapshot = _snapshot(
        ("S01E01.mkv", True, True), ("S01E02.mkv", target_selected, False), state=ExistingTaskState.PAUSED,
    )

    decision = _decide(snapshot, episodes={2}, user_specified=True)

    assert decision.rejection is None
    assert decision.action is action
    assert decision.enable == enable and decision.required == (1,)
    assert decision.resume is True and decision.pending is True


@pytest.mark.parametrize("user_specified", [False, True])
def test_paused_task_with_complete_target_is_reused_without_resuming(user_specified):
    """
    目标已经下完时，暂停的任务照样按复用受理，不改启停状态；手动指定种子也不为已下完的目标恢复任务。
    """
    snapshot = _snapshot(("S01E01.mkv", True, True), ("S01E02.mkv", False, False), state=ExistingTaskState.PAUSED)

    decision = _decide(snapshot, episodes={1}, user_specified=user_specified)

    assert decision.action is DownloadTaskAction.REUSED
    assert decision.required == (0,) and decision.pending is False and decision.resume is False


def test_running_task_is_never_resumed():
    """
    运行中的任务不需要恢复，手动指定种子时同样只追加。
    """
    snapshot = _snapshot(("S01E01.mkv", True, False), ("S01E02.mkv", False, False))

    decision = _decide(snapshot, episodes={2}, user_specified=True)

    assert decision.action is DownloadTaskAction.APPENDED
    assert decision.resume is False


def test_running_task_appends_and_requires_all_targets():
    """
    运行中的任务只追加尚未选中的目标；回读要核对的是全部目标，而不只是新追加的。
    """
    snapshot = _snapshot(("S01E01.mkv", True, False), ("S01E02.mkv", False, False), ("S01E03.mkv", False, False))

    decision = _decide(snapshot, episodes={1, 2})

    assert decision.action is DownloadTaskAction.APPENDED
    assert decision.enable == (1,)
    assert decision.required == (0, 1)
    assert decision.pending is True


def test_confirmed_selection_note_round_trip_and_legacy_notes():
    """
    确认记录按下载器分开写入下载历史 note 后可读回；没有记录、版本或格式不符时视为空记录。
    """
    selection = ConfirmedSelection().record("qb", _confirmed(0), replace=True)
    selection = selection.record("qb", _confirmed(1), replace=False)
    selection = selection.record("tr", _confirmed(3), replace=True)
    note = {"source": "Subscribe|{}", torrent_rules.CONFIRMED_SELECTION_NOTE_KEY: selection.to_note()}

    restored = ConfirmedSelection.from_note(note)

    assert restored == selection
    assert restored.files_for("qb") == _confirmed(0, 1)
    assert restored.files_for("tr") == _confirmed(3)
    assert restored.files_for("rt") == frozenset()
    assert ConfirmedSelection.from_note({"source": "旧版本"}) == ConfirmedSelection()
    assert ConfirmedSelection.from_note(None) == ConfirmedSelection()
    # 开发期的旧格式（逐文件带路径）版本不符，按没有记录处理。
    assert ConfirmedSelection.from_note(
        {torrent_rules.CONFIRMED_SELECTION_NOTE_KEY: {
            "version": 2, "downloaders": {"qb": [{"index": 0, "path": ["a"]}]},
        }},
    ) == ConfirmedSelection()


def test_confirmed_selection_note_stores_compact_index_ranges():
    """
    每条下载历史都带一份累积记录，且 note 会随下载历史接口返回：记录只存索引，并压缩成连续区间，
    整包记录不随文件数增长。
    """
    whole = ConfirmedSelection().record("qb", frozenset(range(500)), replace=True)
    scattered = ConfirmedSelection().record("qb", _confirmed(0, 1, 2, 5, 7, 8), replace=True)

    assert whole.to_note()["downloaders"] == {"qb": [[0, 499]]}
    assert scattered.to_note()["downloaders"] == {"qb": [[0, 2], [5, 5], [7, 8]]}
    assert ConfirmedSelection.from_note({torrent_rules.CONFIRMED_SELECTION_NOTE_KEY: whole.to_note()}) == whole


@pytest.mark.parametrize("ranges", [
    [[3, 1]], [[0]], [[0, True]], [[-1, 2]], "0-3", [[0, 1 << 21]], [{"index": 0}],
])
def test_malformed_ranges_drop_only_that_downloader(ranges):
    """
    某台下载器的区间格式不符（含异常大的区间）时，该下载器按没有记录处理，其他下载器的记录照常读取。
    """
    note = {torrent_rules.CONFIRMED_SELECTION_NOTE_KEY: {
        "version": 3, "downloaders": {"qb": ranges, "tr": [[2, 3]], 7: [[0, 0]]},
    }}

    restored = ConfirmedSelection.from_note(note)

    assert restored.files_for("qb") == frozenset()
    assert restored.files_for("tr") == _confirmed(2, 3)


def test_new_task_replaces_only_its_own_downloader_record():
    """
    新建任务替换这台下载器的旧记录（同 hash 的旧任务可能已被删除重建），其他下载器的记录保留。
    """
    selection = ConfirmedSelection().record("qb", _confirmed(0), replace=True)
    selection = selection.record("tr", _confirmed(1), replace=True)

    rebuilt = selection.record("qb", _confirmed(2), replace=True)

    assert rebuilt.files_for("qb") == _confirmed(2)
    assert rebuilt.files_for("tr") == _confirmed(1)


def test_renamed_file_cancelled_in_downloader_is_not_reselected():
    """
    用户在下载器里给 E06 改了名又取消选择：索引没变，仍按被取消处理，不会当成"从未选中"重新勾选。
    """
    snapshot = _snapshot(("E05.mkv", True, True), ("E06 我的改名.mkv", False, False))

    decision = _decide(snapshot, confirmed=_confirmed(0, 1))

    assert decision.rejection is ExistingTaskRejection.FILES_CANCELLED


def test_confirmed_target_files_follow_downloader_selection_rules():
    """
    受理后记录的是按与下载器相同的规则算出的文件：按集请求是全部目标文件，包括对应集的字幕与无集号附件；
    已有任务上的整包请求只确保视频已选中，记录也只有视频。
    """
    files = list(enumerate([
        "Show/Season 1/S01E01.mkv", "Show/Season 1/S01E01.ass", "Show/Season 1/S01E02.mkv",
        "Show/Season 1/S01E03.mkv", "Show/Fonts.7z",
    ]))

    for new_task in (False, True):
        assert torrent_rules.confirmed_target_files(
            files, episodes={1, 2}, media_extensions=_MEDIA, new_task=new_task,
        ) == _confirmed(0, 1, 2, 4)
    assert torrent_rules.confirmed_target_files(files, episodes=None, media_extensions=_MEDIA) == _confirmed(0, 2, 3)
    assert torrent_rules.confirmed_target_files([], episodes={1}, media_extensions=_MEDIA) == frozenset()
    assert torrent_rules.confirmed_target_files(files, episodes={9}, media_extensions=_MEDIA) == frozenset()


def test_new_whole_torrent_records_every_file_so_cancelled_attachments_stay_cancelled():
    """
    新建整包时下载器默认选中全部文件，记录全部文件（含字幕、字体）。之后用户在下载器里取消了 E01 字幕和字体，
    订阅再请求 E01 时它们按被取消的附件静默跳过，不会因为"从未记录"而被重新勾选。
    """
    names = ["S01E01.mkv", "S01E01.ass", "S01E02.mkv", "Fonts.7z"]
    confirmed = torrent_rules.confirmed_target_files(
        list(enumerate(names)), episodes=None, media_extensions=_MEDIA, new_task=True,
    )
    snapshot = _snapshot(("S01E01.mkv", True, True), ("S01E01.ass", False, False), ("S01E02.mkv", True, True),
                         ("Fonts.7z", False, False))

    decision = _decide(snapshot, episodes={1}, confirmed=confirmed)

    assert confirmed == _confirmed(0, 1, 2, 3)
    assert decision.action is DownloadTaskAction.REUSED
    assert decision.enable == () and decision.required == (0,)


def test_other_season_with_same_episode_number_is_not_treated_as_cancelled():
    """
    多季包里 S01E01 确认选中过，之后请求 S02E01：确认记录存的是文件身份，不会按集号把 S02E01 误判为被取消。
    """
    files = [(0, "Show/S01/Show.S01E01.mkv"), (1, "Show/S02/Show.S02E01.mkv")]
    confirmed = torrent_rules.confirmed_target_files(
        files, episodes={1}, media_extensions=_MEDIA, episode_selection=EpisodeSelection(season=1),
    )
    snapshot = _snapshot(("Show/S01/Show.S01E01.mkv", True, True), ("Show/S02/Show.S02E01.mkv", False, False))

    decision = _decide(snapshot, episodes={1}, confirmed=confirmed, episode_selection=EpisodeSelection(season=2))

    assert confirmed == _confirmed(0)
    assert decision.action is DownloadTaskAction.APPENDED
    assert decision.enable == (1,)


def test_changed_recognition_words_do_not_reinterpret_confirmed_files():
    """
    识别词改动后同一集号对应到别的文件，也不会把从未选中的文件误判为被取消：比对的是文件身份。
    """
    snapshot = _snapshot(("E01.mkv", True, True), ("E02.mkv", False, False))

    decision = _decide(snapshot, episodes={2}, confirmed=_confirmed(0), custom_words="E01 => E02")

    assert decision.rejection is None
    assert decision.enable == (1,)


def test_cancelled_target_is_rejected_unless_user_specified():
    """
    MP 确认选中过、现在被取消的目标主文件按取消拒绝；用户手动指定种子时这次的明确要求覆盖之前的取消，重新选中。
    外部添加的任务没有确认记录，按"从未选中"追加。
    """
    snapshot = _snapshot(("S01E01.mkv", False, False), ("S01E02.mkv", True, False))
    confirmed = _confirmed(0)

    rejected = _decide(snapshot, episodes={1}, confirmed=confirmed)
    explicit = _decide(snapshot, episodes={1}, confirmed=confirmed, user_specified=True)
    external = _decide(snapshot, episodes={1}, confirmed=None)

    assert rejected.rejection is ExistingTaskRejection.FILES_CANCELLED
    assert rejected.rejection.is_target_specific and rejected.rejection.is_resource_level
    for decision in (explicit, external):
        assert decision.action is DownloadTaskAction.APPENDED
        assert decision.enable == (0,)


def test_cancelled_attachment_is_skipped_silently_but_never_reselected():
    """
    曾被确认选中、后被取消的非视频附属文件不重新选中，也不因此拒绝；视频照常追加。
    """
    snapshot = _snapshot(("S01E01.mkv", True, True), ("S01E01.ass", False, True), ("S01E02.mkv", False, False))

    decision = _decide(snapshot, episodes={1, 2}, confirmed=_confirmed(0, 1))

    assert decision.action is DownloadTaskAction.APPENDED
    assert decision.enable == (2,)
    assert decision.required == (0, 2)


def test_cancelled_subtitle_does_not_block_a_paused_task_with_complete_video():
    """
    暂停的任务上 E05 视频已下完、确认选中过的字幕被取消（没下完）：字幕按附属文件静默跳过，按复用受理；
    不能因为字幕没下完就判成"目标仍需下载"而拒绝。
    """
    snapshot = _snapshot(("S01E05.mkv", True, True), ("S01E05.ass", False, False), state=ExistingTaskState.PAUSED)

    decision = _decide(snapshot, episodes={5}, confirmed=_confirmed(0, 1))

    assert decision.action is DownloadTaskAction.REUSED
    assert decision.enable == () and decision.required == (0,)


@pytest.mark.parametrize("cancelled", [{0, 1}, {1}])
def test_cancelled_tracks_of_audio_only_torrent_are_rejected(cancelled):
    """
    没有视频的种子（例如音乐专辑）以全部文件为主文件：曾确认选中的音轨被取消后按取消拒绝，
    不能因为"没有被取消的视频"而把缺轨判成复用成功。
    """
    files = [(f"{index:02d}.flac", index not in cancelled, index not in cancelled) for index in range(2)]

    decision = _decide(_snapshot(*files), confirmed=_confirmed(0, 1))

    assert decision.rejection is ExistingTaskRejection.FILES_CANCELLED
    assert decision.enable == ()


def test_cancelled_non_video_files_beside_videos_stay_attachments():
    """
    有视频的种子里，被取消的音频、字幕等非视频文件仍按附属文件静默跳过，不影响整包复用。
    """
    snapshot = _snapshot(("Movie.mkv", True, True), ("Movie.Commentary.flac", False, False))

    decision = _decide(snapshot, confirmed=_confirmed(0, 1))

    assert decision.action is DownloadTaskAction.REUSED
    assert decision.enable == ()


def test_unknown_states_are_never_guessed():
    """
    状态读不到时只有在仍需下载时才拒绝（目标已完成不需要知道状态）；文件列表读不到时一律拒绝。
    """
    files = (("S01E01.mkv", True, False),)

    assert _decide(_snapshot(*files, state=ExistingTaskState.UNKNOWN), episodes={1}).rejection is (
        ExistingTaskRejection.STATE_UNAVAILABLE
    )
    done = _snapshot(("S01E01.mkv", True, True), state=ExistingTaskState.UNKNOWN)
    assert _decide(done, episodes={1}).action is DownloadTaskAction.REUSED
    assert _decide(_snapshot(), episodes={1}).rejection is ExistingTaskRejection.FILES_UNAVAILABLE


def test_error_state_is_rejected_even_when_target_is_complete():
    """
    下载器报告出错（例如文件缺失）时进度不可信，即使显示已完成也拒绝。
    """
    snapshot = _snapshot(("S01E01.mkv", True, True), state=ExistingTaskState.ERROR)

    decision = _decide(snapshot, episodes={1})

    assert decision.rejection is ExistingTaskRejection.TASK_ERROR
    assert decision.rejection.is_resource_level
    assert not decision.rejection.is_target_specific


def test_missing_target_episodes_are_rejected_for_these_episodes_only():
    """
    目标集在文件列表里找不到时拒绝，冷却只针对这几集。
    """
    # 两个视频：只有一个视频时单集请求会走"唯一视频兜底"，不会判为找不到。
    decision = _decide(_snapshot(("S01E01.mkv", True, True), ("S01E03.mkv", True, True)), episodes={2})

    assert decision.rejection is ExistingTaskRejection.EPISODES_NOT_FOUND
    assert decision.rejection.is_target_specific


def test_rejection_messages_are_recognized_by_prefix():
    """
    下载器返回的失败说明以原因开头，下载链据此识别，不依赖细节文本。
    """
    message = ExistingTaskRejection.FILES_CANCELLED.describe("abc", "目标视频已被删除")

    assert ExistingTaskRejection.from_message(message) is ExistingTaskRejection.FILES_CANCELLED
    assert ExistingTaskRejection.from_message("添加种子任务失败：已有任务的文件") is None
    assert ExistingTaskRejection.from_message(None) is None
