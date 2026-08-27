"""3.0.17 收口整理规划状态与执行恢复证据。

Revision ID: f6d8b0c2e4a7
Revises: e5c7a9b1d3f6
Create Date: 2026-08-27
"""

import hashlib
import json
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "f6d8b0c2e4a7"
down_revision = "e5c7a9b1d3f6"
branch_labels = None
depends_on = None

_PENDING_TABLE = "transferpending"
_STEP_TABLE = "transferexecutionstep"
_RECEIPT_TABLE = "transfersettlementreceipt"
_HISTORY_TABLE = "transferhistory"
_ALLOWED_EXECUTION_STATES = {
    "not_started",
    "running",
    "retry_wait",
    "settling",
    "failed",
    "manual_review",
}
_REVIEW_DIAGNOSTIC = "升级检测到不完整执行状态，需人工确认后再处理"
_REVIEW_STEP_KIND = "legacy_execution_review"
_REVIEW_STEP_ORDINAL = 2_147_483_647
_FALLBACK_TIME = "1970-01-01 00:00:00"


def _table_names() -> set[str]:
    """返回当前数据库的表名集合。"""
    return set(sa.inspect(op.get_bind()).get_table_names())


def _canonical_json(payload: dict[str, Any]) -> str:
    """生成与运行时一致的稳定 JSON 表达。"""
    return json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _fingerprint(payload: dict[str, Any]) -> str:
    """计算版本化 JSON 的 SHA-256 指纹。"""
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _is_nonempty_string(value: object) -> bool:
    """判断值是否为非空文本。"""
    return isinstance(value, str) and bool(value)


def _is_source_fileitem(value: object) -> bool:
    """判断 JSON 对象是否包含可恢复的源文件身份。"""
    return (
        isinstance(value, dict)
        and _is_nonempty_string(value.get("storage"))
        and _is_nonempty_string(value.get("path"))
    )


def _is_optional_mapping(value: object) -> bool:
    """判断值是否为 JSON 对象或空值。"""
    return value is None or isinstance(value, dict)


def _is_optional_string(value: object) -> bool:
    """判断值是否为字符串或空值。"""
    return value is None or isinstance(value, str)


def _is_planning_input(
        value: object,
        *,
        expected_fingerprint: object,
) -> bool:
    """按第一版运行时契约校验规划输入及其持久指纹。"""
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        return False
    if not _is_source_fileitem(value.get("source_fileitem")):
        return False
    if not all(
            _is_optional_mapping(value.get(field_name))
            for field_name in ("meta", "mediainfo", "target_directory")
    ):
        return False
    if not all(
            _is_optional_string(value.get(field_name))
            for field_name in (
                "target_storage",
                "target_path",
                "requested_transfer_type",
                "media_source",
                "media_id",
                "media_type",
                "overwrite_mode",
            )
    ):
        return False
    if not all(
            isinstance(value.get(field_name, default), bool)
            for field_name, default in (
                ("need_scrape", False),
                ("need_rename", True),
                ("need_notify", True),
                ("preview", False),
            )
    ):
        return False
    episodes = value.get("episodes_info", [])
    if (
            not isinstance(episodes, list)
            or not all(isinstance(item, dict) for item in episodes)
            or not isinstance(value.get("options", {}), dict)
    ):
        return False
    try:
        return _fingerprint(value) == expected_fingerprint
    except (TypeError, ValueError):
        return False


def _is_provider_reference(value: object) -> bool:
    """判断旧 provider 引用是否包含稳定身份与固定方法。"""
    return (
        isinstance(value, dict)
        and _is_nonempty_string(value.get("plugin_id"))
        and _is_nonempty_string(value.get("plugin_name"))
        and value.get("method", "transfer") == "transfer"
    )


def _is_provider_invocation(value: object) -> bool:
    """按第一版旧 ABI 契约校验 provider 调用快照。"""
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        return False
    if not _is_source_fileitem(value.get("fileitem")):
        return False
    for field_name in ("meta", "mediainfo", "target_directory"):
        field_value = value.get(field_name)
        if field_value is not None and not isinstance(field_value, dict):
            return False
    for field_name in ("meta_kind", "mediainfo_kind"):
        field_value = value.get(field_name)
        if field_value is not None and not _is_nonempty_string(field_value):
            return False
    for field_name in ("target_storage", "target_path", "transfer_type"):
        field_value = value.get(field_name)
        if field_value is not None and not isinstance(field_value, str):
            return False
    for field_name in (
            "scrape",
            "library_type_folder",
            "library_category_folder",
    ):
        field_value = value.get(field_name)
        if field_value is not None and not isinstance(field_value, bool):
            return False
    episodes = value.get("episodes_info", [])
    return (
        isinstance(value.get("preview", False), bool)
        and isinstance(episodes, list)
        and all(isinstance(item, dict) for item in episodes)
    )


def _is_plan_item(value: object, *, sequence: int) -> bool:
    """判断计划叶子项是否具有连续序号和完整源目标身份。"""
    return (
        isinstance(value, dict)
        and value.get("sequence") == sequence
        and _is_source_fileitem(value.get("source_fileitem"))
        and _is_nonempty_string(value.get("target_storage"))
        and _is_nonempty_string(value.get("target_path"))
        and _is_nonempty_string(value.get("action", "transfer"))
    )


def _classify_checkpoint(
        *,
        checkpoint_version: object,
        checkpoint_payload: object,
        input_fingerprint: object,
) -> str | None:
    """把完整计划检查点分类为宿主计划或 provider 待执行态。"""
    if (
            checkpoint_version != 1
            or not isinstance(checkpoint_payload, dict)
            or checkpoint_payload.get("schema_version") != 1
            or not _is_planning_input(
                checkpoint_payload.get("planning_input"),
                expected_fingerprint=input_fingerprint,
            )
    ):
        return None
    providers = checkpoint_payload.get("legacy_transfer_providers", [])
    if (
            not isinstance(providers, list)
            or not all(_is_provider_reference(item) for item in providers)
    ):
        return None
    if not all(
            _is_optional_mapping(checkpoint_payload.get(field_name))
            for field_name in ("resolved_meta", "resolved_mediainfo")
    ):
        return None
    if not all(
            value is None or _is_nonempty_string(value)
            for value in (
                checkpoint_payload.get("resolved_meta_kind"),
                checkpoint_payload.get("resolved_mediainfo_kind"),
            )
    ):
        return None
    resolved_episodes = checkpoint_payload.get("resolved_episodes_info", [])
    if (
            not isinstance(resolved_episodes, list)
            or not all(isinstance(item, dict) for item in resolved_episodes)
            or not all(
                isinstance(checkpoint_payload.get(field_name, default), bool)
                for field_name, default in (
                    ("pre_execution_cleanup_completed", False),
                    ("need_scrape", False),
                    ("need_rename", False),
                    ("need_notify", True),
                    ("preview", False),
                )
            )
            or not _is_optional_string(checkpoint_payload.get("overwrite_mode"))
            or not _is_optional_string(checkpoint_payload.get("skip_reason"))
            or not _is_optional_string(checkpoint_payload.get("rejection_error"))
    ):
        return None
    invocation = checkpoint_payload.get("provider_invocation")
    if invocation is not None:
        if (
                not providers
                or not _is_provider_invocation(invocation)
                or not invocation.get("meta")
                or not _is_nonempty_string(invocation.get("meta_kind"))
                or not invocation.get("mediainfo")
                or not _is_nonempty_string(invocation.get("mediainfo_kind"))
                or not checkpoint_payload.get("resolved_meta")
                or not _is_nonempty_string(checkpoint_payload.get("resolved_meta_kind"))
                or not checkpoint_payload.get("resolved_mediainfo")
                or not _is_nonempty_string(
                    checkpoint_payload.get("resolved_mediainfo_kind")
                )
                or checkpoint_payload.get("pre_execution_cleanup_completed", False)
                or any(checkpoint_payload.get(field_name) for field_name in (
                    "target_storage",
                    "root_target_path",
                    "final_target_path",
                    "resolved_transfer_type",
                    "items",
                    "skip_reason",
                ))
        ):
            return None
        return "provider_pending"
    items = checkpoint_payload.get("items", [])
    rejection_error = checkpoint_payload.get("rejection_error")
    if (
            not isinstance(items, list)
            or not all(
                _is_plan_item(item, sequence=sequence)
                for sequence, item in enumerate(items)
            )
            or not all(
                _is_nonempty_string(checkpoint_payload.get(field_name))
                for field_name in (
                    "target_storage",
                    "root_target_path",
                    "final_target_path",
                    "resolved_transfer_type",
                )
            )
            or (
                not items
                and not checkpoint_payload.get("preview", False)
                and not _is_nonempty_string(checkpoint_payload.get("skip_reason"))
                and not _is_nonempty_string(rejection_error)
            )
            or (
                rejection_error is not None
                and (
                    not _is_nonempty_string(rejection_error)
                    or not rejection_error.strip()
                    or bool(items)
                )
            )
    ):
        return None
    return "planned"


def _normalize_legacy_planning_states() -> None:
    """把旧 planning manual_review 恢复为运行时可领取的稳定状态。"""
    pending = sa.table(
        _PENDING_TABLE,
        sa.column("id", sa.Integer()),
        sa.column("state", sa.String(32)),
        sa.column("input_fingerprint", sa.String(64)),
        sa.column("checkpoint_version", sa.Integer()),
        sa.column("checkpoint_payload", sa.JSON()),
        sa.column("planned_at", sa.String(40)),
    )
    bind = op.get_bind()
    rows = bind.execute(
        sa.select(
            pending.c.id,
            pending.c.state,
            pending.c.input_fingerprint,
            pending.c.checkpoint_version,
            pending.c.checkpoint_payload,
        ).where(pending.c.state.in_((
            "accepted",
            "planned",
            "provider_pending",
            "manual_review",
        )))
    ).mappings().all()
    for row in rows:
        state = _classify_checkpoint(
            checkpoint_version=row["checkpoint_version"],
            checkpoint_payload=row["checkpoint_payload"],
            input_fingerprint=row["input_fingerprint"],
        )
        target_state = state
        values: dict[str, object] = {"state": target_state or "accepted"}
        if target_state is None:
            values.update({
                "checkpoint_version": None,
                "checkpoint_payload": sa.null(),
                "planned_at": None,
            })
        bind.execute(
            pending.update().where(pending.c.id == row["id"]).values(**values)
        )


def _is_execution_checkpoint(row: dict[str, Any]) -> bool:
    """校验执行检查点三元组的完整性、版本和内容指纹。"""
    values = (
        row["execution_version"],
        row["execution_payload"],
        row["execution_fingerprint"],
    )
    if all(value is None for value in values):
        return True
    if (
            row["execution_version"] != 1
            or not isinstance(row["execution_payload"], dict)
            or not _is_nonempty_string(row["execution_fingerprint"])
    ):
        return False
    payload = row["execution_payload"]
    operation_ids = payload.get("operation_ids")
    skip_reason = payload.get("skip_reason")
    execution_result = payload.get("payload")
    if (
            payload.get("schema_version") != 1
            or not isinstance(execution_result, dict)
            or not isinstance(operation_ids, list)
            or not all(_is_nonempty_string(item) for item in operation_ids)
            or len(operation_ids) != len(set(operation_ids))
            or (not operation_ids and not _is_nonempty_string(skip_reason))
            or (skip_reason is not None and not isinstance(skip_reason, str))
    ):
        return False
    outcome = execution_result.get("outcome")
    if outcome not in {"succeeded", "failed", "overwrite_skipped"}:
        return False
    transferinfo = execution_result.get("transferinfo")
    if transferinfo is not None:
        if not isinstance(transferinfo, dict):
            return False
        if (
                bool(transferinfo.get("success")) != (outcome == "succeeded")
                or bool(transferinfo.get("overwrite_skipped"))
                != (outcome == "overwrite_skipped")
        ):
            return False
    elif outcome == "overwrite_skipped":
        return False
    try:
        return _fingerprint(payload) == row["execution_fingerprint"]
    except (TypeError, ValueError):
        return False


def _has_failed_receipt(row: dict[str, Any]) -> bool:
    """判断失败 pending 是否具有匹配历史与 append-only 回执。"""
    if (
            _RECEIPT_TABLE not in _table_names()
            or _HISTORY_TABLE not in _table_names()
            or not isinstance(row["terminal_history_id"], int)
            or not isinstance(row["settlement_revision"], int)
            or row["settlement_revision"] <= 0
    ):
        return False
    receipts = sa.table(
        _RECEIPT_TABLE,
        sa.column("task_id", sa.String(64)),
        sa.column("history_id", sa.Integer()),
        sa.column("settlement_revision", sa.Integer()),
        sa.column("outcome", sa.String(16)),
    )
    history = sa.table(
        _HISTORY_TABLE,
        sa.column("id", sa.Integer()),
    )
    bind = op.get_bind()
    receipt_exists = bind.execute(
        sa.select(sa.literal(1)).select_from(receipts).where(
            receipts.c.task_id == row["task_id"],
            receipts.c.history_id == row["terminal_history_id"],
            receipts.c.settlement_revision == row["settlement_revision"],
            receipts.c.outcome == "failed",
        ).limit(1)
    ).first() is not None
    history_exists = bind.execute(
        sa.select(sa.literal(1)).select_from(history).where(
            history.c.id == row["terminal_history_id"]
        ).limit(1)
    ).first() is not None
    return receipt_exists and history_exists


def _checkpoint_steps_complete(row: dict[str, Any]) -> bool:
    """判断执行检查点引用的每个外部操作是否已有确定结果。"""
    payload = row["execution_payload"]
    if not isinstance(payload, dict):
        return False
    operation_ids = payload.get("operation_ids")
    if not isinstance(operation_ids, list):
        return False
    if not operation_ids:
        return True
    steps = sa.table(
        _STEP_TABLE,
        sa.column("task_id", sa.String(64)),
        sa.column("operation_id", sa.String(64)),
        sa.column("state", sa.String(32)),
    )
    completed = set(op.get_bind().execute(
        sa.select(steps.c.operation_id).where(
            steps.c.task_id == row["task_id"],
            steps.c.operation_id.in_(operation_ids),
            steps.c.state.in_(("succeeded", "failed")),
        )
    ).scalars().all())
    return completed == set(operation_ids)


def _has_manual_review_step(task_id: str) -> bool:
    """判断人工复核态是否具有运行时可发现的对应步骤证据。"""
    steps = sa.table(
        _STEP_TABLE,
        sa.column("task_id", sa.String(64)),
        sa.column("state", sa.String(32)),
    )
    return op.get_bind().execute(
        sa.select(sa.literal(1)).select_from(steps).where(
            steps.c.task_id == task_id,
            steps.c.state == "manual_review",
        ).limit(1)
    ).first() is not None


def _execution_issue(row: dict[str, Any]) -> str | None:
    """返回阻止执行状态安全恢复的持久不变量缺口。"""
    state = row["execution_state"]
    if state not in _ALLOWED_EXECUTION_STATES:
        return f"未知执行状态: {state}"
    checkpoint_valid = _is_execution_checkpoint(row)
    has_checkpoint = row["execution_payload"] is not None
    if not checkpoint_valid:
        return "执行检查点三元组不完整或内容无效"
    if state != "not_started" and row["state"] not in {
            "planned",
            "provider_pending",
    }:
        return "执行态缺少完整 planned 或 provider_pending 计划"
    lease_values = (
        row["lease_owner"],
        row["lease_token"],
        row["lease_expires_at"],
    )
    if state != "manual_review" and any(lease_values) and not all(lease_values):
        return "执行租约身份不完整"
    if state == "not_started" and has_checkpoint:
        return "未开始态错误携带执行检查点"
    if state == "running" and has_checkpoint:
        return "运行态错误携带终态执行检查点"
    if state == "manual_review" and has_checkpoint:
        return "人工复核态错误携带终态执行检查点"
    if state == "manual_review" and not _has_manual_review_step(row["task_id"]):
        return "人工复核态缺少步骤证据"
    if state == "retry_wait" and not row["retry_due_at"]:
        return "重试等待态缺少到期时间"
    if state == "retry_wait" and any(lease_values):
        return "重试等待态错误持有执行租约"
    if state == "retry_wait" and has_checkpoint and (
            not _checkpoint_steps_complete(row) or not _has_failed_receipt(row)
    ):
        return "终态重试缺少匹配步骤、历史或结算回执"
    if state == "settling" and (
            not has_checkpoint or not _checkpoint_steps_complete(row)
    ):
        return "结算态缺少完整检查点或步骤结果"
    if state == "failed" and (
            not has_checkpoint
            or any(lease_values)
            or not _checkpoint_steps_complete(row)
            or not _has_failed_receipt(row)
    ):
        return "失败终态缺少匹配步骤、检查点、历史或结算回执"
    return None


def _review_operation_id(task_id: str) -> str:
    """生成 3.0.17 数据修复步骤的确定性身份。"""
    return hashlib.sha256(
        f"moviepilot:3.0.17:execution-review:{task_id}".encode("utf-8")
    ).hexdigest()


def _append_review_error(last_error: object, issue: str) -> str:
    """保留原始错误并追加可识别的迁移诊断。"""
    diagnostic = f"{_REVIEW_DIAGNOSTIC}（{issue}）"
    if not _is_nonempty_string(last_error):
        return diagnostic
    if _REVIEW_DIAGNOSTIC in last_error:
        return last_error
    return f"{last_error}\n{diagnostic}"


def _next_review_ordinal(task_id: str) -> int:
    """选择不会覆盖既有步骤证据的高位人工复核序号。"""
    steps = sa.table(
        _STEP_TABLE,
        sa.column("task_id", sa.String(64)),
        sa.column("ordinal", sa.Integer()),
    )
    ordinals = set(op.get_bind().execute(
        sa.select(steps.c.ordinal).where(steps.c.task_id == task_id)
    ).scalars().all())
    ordinal = _REVIEW_STEP_ORDINAL
    while ordinal in ordinals:
        ordinal -= 1
    return ordinal


def _insert_review_step(row: dict[str, Any], *, issue: str) -> None:
    """把非法执行组合冻结为可人工判定的 synthetic 审计步骤。"""
    operation_id = _review_operation_id(row["task_id"])
    steps = sa.table(
        _STEP_TABLE,
        sa.column("task_id", sa.String(64)),
        sa.column("operation_id", sa.String(64)),
        sa.column("checkpoint_fingerprint", sa.String(64)),
        sa.column("ordinal", sa.Integer()),
        sa.column("phase", sa.String(32)),
        sa.column("kind", sa.String(32)),
        sa.column("state", sa.String(32)),
        sa.column("attempt_token", sa.String(64)),
        sa.column("attempt_count", sa.Integer()),
        sa.column("intent_version", sa.Integer()),
        sa.column("intent_payload", sa.JSON()),
        sa.column("result_version", sa.Integer()),
        sa.column("result_payload", sa.JSON()),
        sa.column("last_error", sa.Text()),
        sa.column("prepared_at", sa.String(40)),
        sa.column("started_at", sa.String(40)),
        sa.column("completed_at", sa.String(40)),
        sa.column("updated_at", sa.String(40)),
    )
    bind = op.get_bind()
    if bind.execute(
            sa.select(sa.literal(1)).select_from(steps).where(
                steps.c.operation_id == operation_id
            ).limit(1)
    ).first() is not None:
        return
    evidence = {
        "schema_version": 1,
        "origin": "3.0.17_migration",
        "diagnostic": issue,
        "legacy_state": row["state"],
        "legacy_execution_state": row["execution_state"],
        "execution_version": row["execution_version"],
        "execution_payload": row["execution_payload"],
        "execution_fingerprint": row["execution_fingerprint"],
        "settlement_revision": row["settlement_revision"],
        "terminal_history_id": row["terminal_history_id"],
        "lease_owner": row["lease_owner"],
        "lease_token": row["lease_token"],
    }
    evidence_time = row["updated_at"] or row["created_at"] or _FALLBACK_TIME
    bind.execute(steps.insert().values(
        task_id=row["task_id"],
        operation_id=operation_id,
        checkpoint_fingerprint=_fingerprint(evidence),
        ordinal=_next_review_ordinal(row["task_id"]),
        phase="legacy_upgrade",
        kind=_REVIEW_STEP_KIND,
        state="manual_review",
        attempt_token=None,
        attempt_count=row["attempt_count"] or 0,
        intent_version=1,
        intent_payload=evidence,
        result_version=None,
        result_payload=None,
        last_error=_append_review_error(row["last_error"], issue),
        prepared_at=evidence_time,
        started_at=None,
        completed_at=None,
        updated_at=evidence_time,
    ))


def _reconcile_execution_states() -> None:
    """隔离非法执行组合，并统一清除人工复核态残留租约。"""
    pending = sa.table(
        _PENDING_TABLE,
        sa.column("id", sa.Integer()),
        sa.column("task_id", sa.String(64)),
        sa.column("state", sa.String(32)),
        sa.column("created_at", sa.String(40)),
        sa.column("updated_at", sa.String(40)),
        sa.column("last_error", sa.Text()),
        sa.column("lease_owner", sa.String(128)),
        sa.column("lease_token", sa.String(64)),
        sa.column("lease_expires_at", sa.String(40)),
        sa.column("heartbeat_at", sa.String(40)),
        sa.column("attempt_count", sa.Integer()),
        sa.column("execution_state", sa.String(32)),
        sa.column("execution_version", sa.Integer()),
        sa.column("execution_payload", sa.JSON()),
        sa.column("execution_fingerprint", sa.String(64)),
        sa.column("retry_due_at", sa.String(40)),
        sa.column("settlement_revision", sa.Integer()),
        sa.column("terminal_history_id", sa.Integer()),
    )
    bind = op.get_bind()
    rows = [
        dict(row)
        for row in bind.execute(sa.select(pending)).mappings().all()
    ]
    for row in rows:
        issue = _execution_issue(row)
        if issue is not None:
            _insert_review_step(row, issue=issue)
            bind.execute(
                pending.update().where(pending.c.id == row["id"]).values(
                    execution_state="manual_review",
                    execution_version=None,
                    execution_payload=sa.null(),
                    execution_fingerprint=None,
                    lease_owner=None,
                    lease_token=None,
                    lease_expires_at=None,
                    heartbeat_at=None,
                    retry_due_at=None,
                    last_error=_append_review_error(row["last_error"], issue),
                )
            )
        elif row["execution_state"] == "manual_review":
            bind.execute(
                pending.update().where(pending.c.id == row["id"]).values(
                    lease_owner=None,
                    lease_token=None,
                    lease_expires_at=None,
                    heartbeat_at=None,
                )
            )


def upgrade() -> None:
    """归一旧人工态并隔离无法安全自动恢复的执行组合。"""
    tables = _table_names()
    if _PENDING_TABLE not in tables:
        return
    if _STEP_TABLE not in tables:
        raise RuntimeError("缺少 3.0.16 transferexecutionstep 表，拒绝执行 3.0.17")
    _normalize_legacy_planning_states()
    _reconcile_execution_states()


def downgrade() -> None:
    """保留更安全的状态归一和人工证据，结构由 3.0.16 负责降级。"""
