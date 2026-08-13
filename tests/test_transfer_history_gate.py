"""
覆盖 app/helper/transferhistory.py 的整理历史查重闸。

监控分发（app/monitor/dispatcher.py）与整理链计划整理段（app/chain/transfer.py）
共用这套判定，本文件只测判定本身的真值表与查询辅助函数，不涉及调用方。
"""
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.helper import transferhistory as transferhistory_helper
from app.helper.transferhistory import (
    HistoryGateAction,
    clear_transfer_failures,
    coerce_size,
    describe_history_gate,
    evaluate_history_gate,
    failed_retry_count,
    history_src_size,
    is_skip_action,
    max_failed_retries,
    record_transfer_failure,
    resolve_history,
)


def make_history(status: bool, size=1024, has_src_fileitem: bool = True,
                  history_id: int = 1, src_fileitem_override=None,
                  src=None, src_storage=None, modify_time=None, fileid=None):
    """构造用于查重闸判定的整理记录替身。"""
    if src_fileitem_override is not None:
        src_fileitem = src_fileitem_override
    elif has_src_fileitem:
        src_fileitem = {
            "size": size,
            "modify_time": modify_time,
            "fileid": fileid,
        }
    else:
        src_fileitem = None
    return SimpleNamespace(id=history_id, status=status, src_fileitem=src_fileitem,
                            src=src, src_storage=src_storage)


def _reset_failed_retries(src_path, storage=None):
    """清空失败重试计数，隔离用例之间共享的模块级计数缓存。"""
    clear_transfer_failures(src_path, storage)


# ---------------------------------------------------------------------------
# evaluate_history_gate 真值表：无记录 / 成功记录
# ---------------------------------------------------------------------------


def test_evaluate_history_gate_passes_when_no_record():
    """没有整理记录时应放行整理。"""
    action = evaluate_history_gate(None, file_size=1024)

    assert action == HistoryGateAction.PASS_NO_RECORD


def test_evaluate_history_gate_passes_when_success_size_changed():
    """成功记录但源文件大小已变化时应放行，交由 overwrite_mode 决断。"""
    history = make_history(status=True, size=1024)

    action = evaluate_history_gate(history, file_size=2048)

    assert action == HistoryGateAction.PASS_SIZE_CHANGED


def test_evaluate_history_gate_skips_when_success_size_unchanged():
    """成功记录且源文件大小未变化时应跳过。"""
    history = make_history(status=True, size=1024)

    action = evaluate_history_gate(history, file_size=1024)

    assert action == HistoryGateAction.SKIP


def test_evaluate_history_gate_skips_when_recorded_size_missing():
    """成功记录缺少大小信息（如蓝光目录）时无法比对，保守跳过。"""
    history = make_history(status=True, has_src_fileitem=False)

    action = evaluate_history_gate(history, file_size=1024)

    assert action == HistoryGateAction.SKIP


def test_evaluate_history_gate_skips_when_current_size_is_none():
    """当前文件大小取不到时无法比对，保守跳过。"""
    history = make_history(status=True, size=1024)

    action = evaluate_history_gate(history, file_size=None)

    assert action == HistoryGateAction.SKIP


def test_evaluate_history_gate_skips_when_src_fileitem_is_not_dict():
    """src_fileitem 历史数据异常（不是字典）时无法比对，保守跳过。"""
    history = make_history(status=True, src_fileitem_override="not-a-dict")

    action = evaluate_history_gate(history, file_size=1024)

    assert action == HistoryGateAction.SKIP


def test_evaluate_history_gate_size_changed_ignores_failed_retry_budget(monkeypatch):
    """
    重试次数上限只影响失败记录的判定：即便同路径的失败计数早已超过上限，
    成功记录 + 源文件大小变化时仍应放行，交由 overwrite_mode 决断。
    """
    monkeypatch.setattr(settings, "TRANSFER_MAX_FAILED_RETRIES", 2)
    src_path = "/downloads/gate-test-size-changed-ignores-budget.mkv"
    _reset_failed_retries(src_path, "local")
    try:
        for _ in range(5):
            record_transfer_failure(src_path, "local")
        assert failed_retry_count(src_path, "local") > max_failed_retries()

        history = make_history(status=True, size=1024, src=src_path, src_storage="local")
        action = evaluate_history_gate(history, file_size=2048)

        assert action == HistoryGateAction.PASS_SIZE_CHANGED
    finally:
        _reset_failed_retries(src_path, "local")


# ---------------------------------------------------------------------------
# evaluate_history_gate 真值表：失败记录 —— 有界重试
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("retry_count, expected", [
    (0, HistoryGateAction.PASS_FAILED),
    (1, HistoryGateAction.PASS_FAILED),
    (2, HistoryGateAction.PASS_FAILED),
    (3, HistoryGateAction.SKIP_RETRY_EXHAUSTED),
    (4, HistoryGateAction.SKIP_RETRY_EXHAUSTED),
])
def test_evaluate_history_gate_failed_record_retry_budget_truth_table(monkeypatch, retry_count, expected):
    """失败记录按有界重试判定：未达上限（含 0）放行重试，达到或超过上限后跳过。"""
    monkeypatch.setattr(settings, "TRANSFER_MAX_FAILED_RETRIES", 3)
    history = make_history(status=False, size=1024)

    action = evaluate_history_gate(history, file_size=1024, retry_count=retry_count)

    assert action == expected


def test_evaluate_history_gate_failed_record_queries_realtime_count_when_omitted(monkeypatch):
    """retry_count 省略（为 None）时应按 history.src / history.src_storage 实时查询失败计数。"""
    monkeypatch.setattr(settings, "TRANSFER_MAX_FAILED_RETRIES", 2)
    src_path = "/downloads/gate-test-realtime-count.mkv"
    _reset_failed_retries(src_path, "local")
    try:
        history = make_history(status=False, size=1024, src=src_path, src_storage="local")

        # 计数为 0：未达上限（2），放行重试
        assert evaluate_history_gate(history, file_size=1024) == HistoryGateAction.PASS_FAILED

        # 累计一次失败，计数为 1：仍未达上限
        record_transfer_failure(src_path, "local")
        assert evaluate_history_gate(history, file_size=1024) == HistoryGateAction.PASS_FAILED

        # 累计第二次失败，计数为 2：达到上限，跳过
        record_transfer_failure(src_path, "local")
        assert evaluate_history_gate(history, file_size=1024) == HistoryGateAction.SKIP_RETRY_EXHAUSTED
    finally:
        _reset_failed_retries(src_path, "local")


def test_evaluate_history_gate_explicit_retry_count_overrides_realtime_lookup(monkeypatch):
    """显式传入 retry_count 时不应再触发实时查询，不受计数器实际状态影响。"""
    monkeypatch.setattr(settings, "TRANSFER_MAX_FAILED_RETRIES", 3)
    src_path = "/downloads/gate-test-explicit-overrides-realtime.mkv"
    _reset_failed_retries(src_path, "local")
    try:
        for _ in range(10):
            record_transfer_failure(src_path, "local")
        history = make_history(status=False, size=1024, src=src_path, src_storage="local")

        # 即使实时计数早已超限，显式传入的低 retry_count 仍应放行
        action = evaluate_history_gate(history, file_size=1024, retry_count=0)

        assert action == HistoryGateAction.PASS_FAILED
    finally:
        _reset_failed_retries(src_path, "local")


def test_failed_history_new_size_passes_and_resets_retry_budget(monkeypatch):
    """失败预算耗尽后同路径文件大小变化时，应放行新版本并从第 1 次失败重新计数。"""
    monkeypatch.setattr(settings, "TRANSFER_MAX_FAILED_RETRIES", 2)
    src_path = "/downloads/gate-test-failed-new-size.mkv"
    _reset_failed_retries(src_path, "local")
    try:
        history = make_history(
            status=False,
            size=1024,
            src=src_path,
            src_storage="local",
        )
        record_transfer_failure(src_path, "local", file_size=1024)
        record_transfer_failure(src_path, "local", file_size=1024)
        assert evaluate_history_gate(history, file_size=1024) == HistoryGateAction.SKIP_RETRY_EXHAUSTED

        assert (
            evaluate_history_gate(history, file_size=2048)
            == HistoryGateAction.PASS_FAILED_VERSION_CHANGED
        )
        assert record_transfer_failure(src_path, "local", file_size=2048) == 1
        assert failed_retry_count(src_path, "local", file_size=2048) == 1
    finally:
        _reset_failed_retries(src_path, "local")


@pytest.mark.parametrize(
    "current_fields",
    [
        {"file_modify_time": 200.0, "fileid": "same-id"},
        {"file_modify_time": 100.0, "fileid": "new-id"},
    ],
)
def test_failed_history_same_size_new_fingerprint_passes(current_fields, monkeypatch):
    """大小相同但修改时间或文件 ID 改变时，也应视为失败文件的新版本。"""
    monkeypatch.setattr(settings, "TRANSFER_MAX_FAILED_RETRIES", 1)
    src_path = "/downloads/gate-test-failed-new-fingerprint.mkv"
    _reset_failed_retries(src_path, "local")
    try:
        history = make_history(
            status=False,
            size=1024,
            modify_time=100.0,
            fileid="same-id",
            src=src_path,
            src_storage="local",
        )
        record_transfer_failure(
            src_path,
            "local",
            file_size=1024,
            file_modify_time=100.0,
            fileid="same-id",
        )
        assert evaluate_history_gate(history, file_size=1024) == HistoryGateAction.SKIP_RETRY_EXHAUSTED

        assert (
            evaluate_history_gate(history, file_size=1024, **current_fields)
            == HistoryGateAction.PASS_FAILED_VERSION_CHANGED
        )
    finally:
        _reset_failed_retries(src_path, "local")


def test_legacy_integer_retry_count_is_upgraded_after_new_version_failure(monkeypatch):
    """Redis 中遗留的整数计数不得阻断新版本，并应在下次失败时升级为指纹状态。"""
    monkeypatch.setattr(settings, "TRANSFER_MAX_FAILED_RETRIES", 2)
    src_path = "/downloads/gate-test-legacy-retry-state.mkv"
    _reset_failed_retries(src_path, "local")
    try:
        key = transferhistory_helper.failed_retry_key(src_path, "local")
        transferhistory_helper._failed_retry_counts[key] = 2
        history = make_history(
            status=False,
            size=1024,
            src=src_path,
            src_storage="local",
        )
        assert evaluate_history_gate(history, file_size=1024) == HistoryGateAction.SKIP_RETRY_EXHAUSTED
        assert (
            evaluate_history_gate(history, file_size=2048)
            == HistoryGateAction.PASS_FAILED_VERSION_CHANGED
        )

        assert record_transfer_failure(src_path, "local", file_size=2048) == 1
        assert failed_retry_count(src_path, "local", file_size=2048) == 1
    finally:
        _reset_failed_retries(src_path, "local")


# ---------------------------------------------------------------------------
# max_failed_retries 钳制
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw, expected", [
    (-1, 1),
    (0, 1),
    (1, 1),
    (3, 3),
    (10, 10),
    (11, 10),
])
def test_max_failed_retries_clamps_numeric_values(monkeypatch, raw, expected):
    """合法区间外的配置值应被钳制到 [1, 10]，区间内的值原样返回。"""
    monkeypatch.setattr(settings, "TRANSFER_MAX_FAILED_RETRIES", raw)

    assert max_failed_retries() == expected


def test_max_failed_retries_falls_back_when_non_integer(monkeypatch):
    """非整数配置（如解析失败的字符串）应回退为下界 1。"""
    monkeypatch.setattr(settings, "TRANSFER_MAX_FAILED_RETRIES", "abc")

    assert max_failed_retries() == 1


# ---------------------------------------------------------------------------
# 失败计数器：record_transfer_failure / failed_retry_count / clear_transfer_failures
# ---------------------------------------------------------------------------


def test_record_transfer_failure_returns_incrementing_count():
    """连续记录失败应返回递增的累计次数。"""
    src_path = "/downloads/gate-test-incrementing-count.mkv"
    _reset_failed_retries(src_path, "local")
    try:
        assert record_transfer_failure(src_path, "local") == 1
        assert record_transfer_failure(src_path, "local") == 2
        assert record_transfer_failure(src_path, "local") == 3
        assert failed_retry_count(src_path, "local") == 3
    finally:
        _reset_failed_retries(src_path, "local")


def test_clear_transfer_failures_resets_count_to_zero():
    """清空后应归零，且不再影响后续查询。"""
    src_path = "/downloads/gate-test-clear-resets.mkv"
    _reset_failed_retries(src_path, "local")
    try:
        record_transfer_failure(src_path, "local")
        record_transfer_failure(src_path, "local")
        assert failed_retry_count(src_path, "local") == 2

        clear_transfer_failures(src_path, "local")

        assert failed_retry_count(src_path, "local") == 0
    finally:
        _reset_failed_retries(src_path, "local")


def test_failed_retry_count_isolates_different_storages_for_same_path():
    """相同源路径但不同 storage 的失败计数应互不影响。"""
    src_path = "/downloads/gate-test-storage-isolation.mkv"
    _reset_failed_retries(src_path, "local")
    _reset_failed_retries(src_path, "alist")
    try:
        record_transfer_failure(src_path, "local")
        record_transfer_failure(src_path, "local")
        record_transfer_failure(src_path, "alist")

        assert failed_retry_count(src_path, "local") == 2
        assert failed_retry_count(src_path, "alist") == 1
    finally:
        _reset_failed_retries(src_path, "local")
        _reset_failed_retries(src_path, "alist")


def test_failed_retry_count_defaults_to_zero_without_record():
    """未记录过失败的路径应返回 0。"""
    src_path = "/downloads/gate-test-no-record-yet.mkv"
    _reset_failed_retries(src_path, "local")

    assert failed_retry_count(src_path, "local") == 0


# ---------------------------------------------------------------------------
# is_skip_action
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action, expected", [
    (HistoryGateAction.PASS_NO_RECORD, False),
    (HistoryGateAction.PASS_FAILED, False),
    (HistoryGateAction.PASS_FAILED_VERSION_CHANGED, False),
    (HistoryGateAction.PASS_SIZE_CHANGED, False),
    (HistoryGateAction.SKIP_RETRY_EXHAUSTED, True),
    (HistoryGateAction.SKIP, True),
])
def test_is_skip_action(action, expected):
    """跳过整理的判定应只对 SKIP 与 SKIP_RETRY_EXHAUSTED 返回 True。"""
    assert is_skip_action(action) is expected


# ---------------------------------------------------------------------------
# coerce_size
# ---------------------------------------------------------------------------


def test_coerce_size_returns_none_for_none():
    """None 应原样返回 None，表示不可比对。"""
    assert coerce_size(None) is None


def test_coerce_size_returns_none_for_non_numeric_string():
    """非数字字符串无法转换，应返回 None。"""
    assert coerce_size("not-a-number") is None


def test_coerce_size_truncates_float():
    """浮点数应按 int() 截断转换。"""
    assert coerce_size(1024.9) == 1024


def test_coerce_size_parses_numeric_string():
    """数字字符串应正确转换为整数。"""
    assert coerce_size("2048") == 2048


# ---------------------------------------------------------------------------
# resolve_history
# ---------------------------------------------------------------------------


def test_resolve_history_upgrades_failed_hit_to_success_record():
    """get_by_src 命中失败记录且存在成功记录时，应返回成功记录。"""
    failed_history = make_history(status=False, history_id=1)
    success_history = make_history(status=True, history_id=2)
    oper = SimpleNamespace(
        get_by_src=lambda src, storage=None: failed_history,
        get_success_by_src=lambda src, storage=None: success_history,
    )

    history = resolve_history("/downloads/a.mkv", storage="local", transfer_history_oper=oper)

    assert history is success_history


def test_resolve_history_keeps_failed_hit_when_no_success_record():
    """get_by_src 命中失败记录但不存在成功记录时，应返回原失败记录。"""
    failed_history = make_history(status=False, history_id=1)
    oper = SimpleNamespace(
        get_by_src=lambda src, storage=None: failed_history,
        get_success_by_src=lambda src, storage=None: None,
    )

    history = resolve_history("/downloads/a.mkv", storage="local", transfer_history_oper=oper)

    assert history is failed_history


def test_resolve_history_does_not_query_success_when_already_successful():
    """get_by_src 直接命中成功记录时，不应再查询 get_success_by_src。"""
    success_history = make_history(status=True, history_id=3)
    success_query_calls = []

    def get_success_by_src(src, storage=None):
        success_query_calls.append(src)
        return success_history

    oper = SimpleNamespace(
        get_by_src=lambda src, storage=None: success_history,
        get_success_by_src=get_success_by_src,
    )

    history = resolve_history("/downloads/a.mkv", storage="local", transfer_history_oper=oper)

    assert history is success_history
    assert success_query_calls == []


def test_resolve_history_returns_none_when_no_record():
    """没有命中任何记录时应返回 None，不触发额外查询。"""
    success_query_calls = []

    def get_success_by_src(src, storage=None):
        success_query_calls.append(src)
        return None

    oper = SimpleNamespace(
        get_by_src=lambda src, storage=None: None,
        get_success_by_src=get_success_by_src,
    )

    history = resolve_history("/downloads/a.mkv", storage="local", transfer_history_oper=oper)

    assert history is None
    assert success_query_calls == []


# ---------------------------------------------------------------------------
# describe_history_gate
# ---------------------------------------------------------------------------


def test_describe_history_gate_reports_no_record():
    """没有记录时应给出明确的无记录说明。"""
    assert describe_history_gate(None, file_size=1024) == "无整理记录"


def test_describe_history_gate_includes_status_and_sizes():
    """说明文案应包含记录状态、记录中的大小与当前大小两个数值。"""
    history = make_history(status=True, size=1024, history_id=9)

    description = describe_history_gate(history, file_size=2048)

    assert "成功记录 #9" in description
    assert str(history_src_size(history)) in description
    assert str(coerce_size(2048)) in description
    assert "1024" in description
    assert "2048" in description


def test_describe_history_gate_reports_failed_status_with_retry_progress(monkeypatch):
    """失败记录的说明文案应标注记录号，并包含「已重试 n/max 次」的实时计数。"""
    monkeypatch.setattr(settings, "TRANSFER_MAX_FAILED_RETRIES", 3)
    src_path = "/downloads/gate-test-describe-failed.mkv"
    _reset_failed_retries(src_path, "local")
    try:
        record_transfer_failure(src_path, "local")
        record_transfer_failure(src_path, "local")
        history = make_history(status=False, size=1024, history_id=5,
                                src=src_path, src_storage="local")

        description = describe_history_gate(history, file_size=1024)

        assert "失败记录 #5" in description
        assert "已重试" in description
        assert "2/3" in description
    finally:
        _reset_failed_retries(src_path, "local")


def test_describe_history_gate_reports_incomparable_sizes():
    """两侧大小都取不到时应说明大小不可比对。"""
    history = make_history(status=True, has_src_fileitem=False, history_id=7)

    description = describe_history_gate(history, file_size=None)

    assert description == "成功记录 #7，大小不可比对"


def test_clear_transfer_failures_is_safe_when_no_count_recorded():
    """
    清空从未失败过的源路径不得抛异常。

    整理成功回调会对每个文件无条件清零，而绝大多数文件从未失败过；
    底层 CacheBackend.pop 把「default 为 None」当成「未提供 default」，
    键不存在时会抛 KeyError，一旦回归就会让每一次首次成功整理都失败。
    """
    clear_transfer_failures("/downloads/gate-test-never-failed.mkv", "local")

    assert failed_retry_count("/downloads/gate-test-never-failed.mkv", "local") == 0


def test_clear_transfer_failures_is_safe_for_empty_path():
    """源路径为空时清零应静默返回，不得抛异常。"""
    clear_transfer_failures(None, None)
    clear_transfer_failures("", "local")
