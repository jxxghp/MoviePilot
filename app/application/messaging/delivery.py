"""
把旧平台结果映射到统一的确认语义。
"""

import json
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from hashlib import sha256

from app.schemas.channel import NotificationRule, NotificationSendTime
from app.schemas.common import JsonData
from app.schemas.delivery import DeliveryPartPlan, DeliveryPartSubmitter, DeliveryResult, DeliveryStatus
from app.schemas.message import Message, MessageResponse
from app.schemas.system import NotificationConf
from app.schemas.types import NotificationChannel

_CONNECTION_PLAN_KEY = "_frozen_connections"
_RULE_PLAN_KEY = "_frozen_notification_rules"

type FrozenNotificationRules = Mapping[str, tuple[NotificationRule, ...]]

_frozen_rules: ContextVar[FrozenNotificationRules | None] = ContextVar("frozen_notification_rules", default=None)
_DELIVERY_PLAN_KEY = "_frozen_delivery_plan"
_ATTACHMENT_LAYOUT_KEY = "_attachment_layout_v2"
_LEGACY_NOTICE_CLAIM_KEY = "_legacy_notice_claimed"

_sending_windows: ContextVar[tuple[NotificationSendTime | None, ...] | None] = ContextVar(
    "notification_sending_windows", default=None,
)


def notification_rule_time_allows(rule: NotificationRule, now: datetime | None = None) -> bool:
    """
    使用系统本地时区判断规则的发送窗口。
    """
    return rule.send_time is None or rule.send_time.allows(now or datetime.now())


def notification_rules_are_frozen() -> bool:
    """
    判断当前派发是否沿用持久化规则快照。
    """
    return _frozen_rules.get() is not None


@contextmanager
def notification_sending_windows(rules: tuple[NotificationRule, ...]) -> Iterator[None]:
    """
    同一目标命中多条规则时，任一时间窗口允许即可发送。
    """
    token = _sending_windows.set(tuple(rule.send_time for rule in rules))
    try:
        yield
    finally:
        _sending_windows.reset(token)


def _sending_time_allows() -> bool:
    """
    在实际提交分段前再次核对窗口，防止重试或分段发送越过结束时间。
    """
    windows = _sending_windows.get()
    return windows is None or any(window is None or window.allows(datetime.now()) for window in windows)


_progress_observer: ContextVar[Callable[[tuple[DeliveryResult, ...]], None] | None] = ContextVar(
    "notification_progress_observer", default=None,
)


_part_reservation: ContextVar[Callable[[DeliveryResult, bool], DeliveryResult | None] | None] = ContextVar(
    "notification_part_reservation", default=None,
)
_part_planner: ContextVar[Callable[[tuple[DeliveryResult, ...]], None] | None] = ContextVar(
    "notification_part_planner", default=None,
)
_plan_freezer: ContextVar[Callable[[tuple[DeliveryResult, ...]], None] | None] = ContextVar(
    "notification_plan_freezer", default=None,
)
_preflight_parts: ContextVar[dict[str, DeliveryResult] | None] = ContextVar("notification_preflight", default=None)
_legacy_attachment_layout: ContextVar[bool] = ContextVar("legacy_attachment_layout", default=False)
_legacy_notice_claim: ContextVar[Callable[[], bool] | None] = ContextVar("legacy_notice_claim", default=None)


@contextmanager
def observe_notification_progress(
    observer: Callable[[tuple[DeliveryResult, ...]], None], *,
    reserve: Callable[[DeliveryResult, bool], DeliveryResult | None] | None = None,
    plan: Callable[[tuple[DeliveryResult, ...]], None] | None = None,
    freeze: Callable[[tuple[DeliveryResult, ...]], None] | None = None,
    claim_legacy_notice: Callable[[], bool] | None = None,
    legacy_attachment_layout: bool = False,
    rules: FrozenNotificationRules | None = None,
) -> Iterator[None]:
    """
    将严格通知的实际回执交给当前持有租约的 Outbox 处理器。
    """
    token = _progress_observer.set(observer)
    part_token = _part_reservation.set(reserve)
    plan_token = _part_planner.set(plan)
    freezer_token = _plan_freezer.set(freeze)
    legacy_token = _legacy_notice_claim.set(claim_legacy_notice)
    attachment_token = _legacy_attachment_layout.set(legacy_attachment_layout)
    rule_token = _frozen_rules.set(rules)
    try:
        yield
    finally:
        _frozen_rules.reset(rule_token)
        _legacy_attachment_layout.reset(attachment_token)
        _legacy_notice_claim.reset(legacy_token)
        _plan_freezer.reset(freezer_token)
        _part_planner.reset(plan_token)
        _part_reservation.reset(part_token)
        _progress_observer.reset(token)


def legacy_attachment_layout_active() -> bool:
    """
    已冻结的旧意图使用原附件布局恢复。
    """
    return _legacy_attachment_layout.get()


def uses_legacy_attachment_layout(snapshot: dict[str, JsonData]) -> bool:
    """
    判断已有投递计划是否早于统一附件布局。
    """
    return not notification_plan_incomplete(snapshot) and snapshot.get(_ATTACHMENT_LAYOUT_KEY) is not True


def claim_legacy_notification_dispatch() -> bool:
    """
    在宿主分段首次派发前认领旧事件与插件回调，普通消息维持即时派发。
    """
    claim = _legacy_notice_claim.get()
    return claim() if claim is not None else True


def reserve_legacy_notification_dispatch(
    snapshot: dict[str, JsonData], *, allow_first_dispatch: bool,
) -> tuple[bool, dict[str, JsonData]]:
    """
    旧接口无法确认是否已产生外部副作用，同一次持久通知最多启动一次旧派发。
    """
    claimed = snapshot.get(_LEGACY_NOTICE_CLAIM_KEY)
    if claimed is True or (claimed is not False and not allow_first_dispatch):
        return False, snapshot
    return True, {**snapshot, _LEGACY_NOTICE_CLAIM_KEY: True}


def publish_notification_progress(results: list[DeliveryResult]) -> None:
    """
    在严格派发结算前提交已获得的回执，即使随后另一个目标失败。
    """
    observer = _progress_observer.get()
    if observer is not None and results:
        observer(tuple(results))


def merge_notification_progress(
    snapshot: dict[str, JsonData], results: tuple[DeliveryResult, ...],
) -> dict[str, JsonData]:
    """
    对同一意图累计回执，保留首次已确认结果；无目标的回执仅用于连接诊断。
    """
    merged = dict(snapshot)
    for result in results:
        if result.code in {"unplanned_target", "content_changed", "unverified_identity"}:
            continue
        key = notification_part_key(result)
        previous = merged.get(key)
        if isinstance(previous, dict) and previous.get("status") == "confirmed":
            continue
        if key not in merged and len(merged) >= 4096:
            raise ValueError("一次 Outbox 通知的投递回执超过上限")
        entry = result.model_dump(mode="json")
        if isinstance(previous, dict) and previous.get("content_digest") and not result.content_digest:
            entry["content_digest"] = previous["content_digest"]
        merged[key] = entry
    return merged


def notification_part_key(result: DeliveryResult) -> str:
    """
    在同一业务意图内识别同一个平台连接、目标和分段。
    """
    return sha256(json.dumps((
        result.channel.value, result.connection_id, result.identity_namespace, result.target_id, result.part,
    )).encode()).hexdigest()


def reserve_notification_part(part: DeliveryResult, should_submit: bool) -> DeliveryResult | None:
    """
    非持久发送可直接提交，持久发送先由当前 Outbox 认领保护。
    """
    if notification_preflight_active():
        return part.model_copy(update={"status": "confirmed"})
    reserve = _part_reservation.get()
    if not _sending_time_allows():
        skipped = part.model_copy(update={"status": "skipped", "code": "outside_rule_time"})
        return (reserve(skipped, False) if reserve is not None else None) or skipped
    return reserve(part, should_submit) if reserve is not None else None


def plan_notification_parts(parts: tuple[DeliveryResult, ...]) -> None:
    """
    发送前一次保存该渠道已经解析出的完整分段清单。

    :param parts: 同一次派发确定的连接、目标和内容摘要
    """
    preflight = _preflight_parts.get()
    if preflight is not None:
        for part in parts:
            key = notification_part_key(part)
            if key in preflight and preflight[key].content_digest != part.content_digest:
                raise ValueError("同一通知分段存在不同内容")
            if key not in preflight and len(preflight) >= 4095:
                raise ValueError("一次 Outbox 通知的投递清单超过上限")
            preflight[key] = part
        return
    planner = _part_planner.get()
    if planner is not None and parts:
        planner(parts)


def managed_notification_dispatch() -> bool:
    """
    判断当前派发是否拥有可持久化的分段认领。
    """
    return _part_reservation.get() is not None


def notification_preflight_required() -> bool:
    """
    判断当前派发是否需要在网络发送前冻结跨连接清单。
    """
    return _plan_freezer.get() is not None


def notification_preflight_active() -> bool:
    """
    判断本次调用是否只允许准备投递分段。
    """
    return _preflight_parts.get() is not None


@contextmanager
def collect_notification_preflight() -> Iterator[None]:
    """
    收集宿主渠道的分段清单，在全部准备完毕后一次持久化。
    """
    preflight: dict[str, DeliveryResult] = {}
    token = _preflight_parts.set(preflight)
    try:
        yield
    finally:
        _preflight_parts.reset(token)
    freezer = _plan_freezer.get()
    if freezer is not None:
        freezer(tuple(preflight.values()))


class NotificationParts:
    """
    把同一连接上的平台发送分段绑定到稳定身份及内容摘要。
    """

    def __init__(
        self, channel: NotificationChannel, connection_id: str, identity_namespace: str | None, *,
        part_offset: int = 0,
    ) -> None:
        """
        :param channel: 出站渠道
        :param connection_id: 稳定连接 ID
        :param identity_namespace: 连接下目标标识的归属
        :param part_offset: 组合附件当前部分在整条通知中的分段起点
        """
        self.channel = channel
        self.connection_id = connection_id
        self.identity_namespace = identity_namespace
        self._part_offset = part_offset
        self._pending_receipts: dict[tuple[str, int], DeliveryResult] = {}

    def _describe(self, target: str, part: int, content: str) -> DeliveryResult:
        """
        生成清单和发送阶段共同使用的识别信息。
        """
        return DeliveryResult(
            channel=self.channel, connection_id=self.connection_id, identity_namespace=self.identity_namespace,
            target_id=target, part=self._part_offset + part, status="unknown", content_digest=sha256(content.encode()).hexdigest(),
        )

    def plan(self, parts: DeliveryPartPlan) -> None:
        """
        保存当前连接已解析的全部目标和分段。
        """
        plan_notification_parts(tuple(self._describe(target, part, content) for target, part, content in parts))

    def record(
        self, message: Message, target: str | None, part: int, status: DeliveryStatus, code: str | None,
    ) -> None:
        """
        将完整的平台回执归入当前消息。未提交的分段单独记录原因。
        """
        recorded = self._pending_receipts.pop((target, part), None) if target is not None else None
        if recorded is None:
            recorded = DeliveryResult(
                channel=self.channel, connection_id=self.connection_id, identity_namespace=self.identity_namespace,
                target_id=target, part=self._part_offset + part, status=status, code=code,
            )
        message.record_delivery_result(recorded)

    def send(
        self, target: str, part: int, content: str, should_submit: bool,
        submit: DeliveryPartSubmitter,
    ) -> tuple[DeliveryStatus, str | None]:
        """
        按稳定目标和内容恢复既有结果，或提交一个新的平台请求。
        """
        result = self.send_result(target, part, content, should_submit, submit)
        self._pending_receipts[target, part] = result
        return result.status, result.code

    def send_result(
        self, target: str, part: int, content: str, should_submit: bool,
        submit: DeliveryPartSubmitter,
    ) -> DeliveryResult:
        """
        返回完整回执，包括平台消息标识和限流等待时间。
        """
        candidate = self._describe(target, part, content)
        recorded = reserve_notification_part(candidate, should_submit)
        if recorded is not None:
            return recorded
        if not should_submit:
            return candidate.model_copy(update={
                "status": "retryable_failure", "code": "previous_part_unconfirmed",
            })
        result = submit()
        delivery = candidate.model_copy(update={
            "status": result[0], "code": result[1],
            "message_id": result[2] if len(result) >= 3 else None,
            "retry_after": result[3] if len(result) == 4 else None,
        })
        publish_notification_progress([delivery])
        return delivery


def freeze_notification_parts(
    snapshot: dict[str, JsonData], parts: tuple[DeliveryResult, ...],
) -> dict[str, JsonData]:
    """
    保留已有回执，只为此前未登记的目标记录待发送分段。

    :param snapshot: 已持久化的投递进度
    :param parts: 待冻结的投递分段
    :return: 含新分段的进度快照
    """
    return merge_notification_progress(snapshot, tuple(
        part.model_copy(update={"status": "retryable_failure", "code": "not_submitted"})
        for part in parts if notification_part_key(part) not in snapshot
    ))


def seal_notification_plan(snapshot: dict[str, JsonData], parts: tuple[DeliveryResult, ...]) -> dict[str, JsonData]:
    """
    原子保存清单与准备完成标记，空计划也要标记以保留既有派发边界。
    """
    if not notification_plan_incomplete(snapshot):
        raise ValueError("已有投递进度，无法再次冻结通知清单")
    return {
        **freeze_notification_parts(snapshot, parts),
        _DELIVERY_PLAN_KEY: True, _LEGACY_NOTICE_CLAIM_KEY: False, _ATTACHMENT_LAYOUT_KEY: True,
    }


def notification_plan_incomplete(snapshot: dict[str, JsonData]) -> bool:
    """
    此意图只有连接范围时，宿主尚可为首次实际发送准备清单。
    """
    return not (snapshot.keys() - {_CONNECTION_PLAN_KEY, _RULE_PLAN_KEY})


def notification_rules_for_dispatch(config: NotificationConf) -> tuple[NotificationRule, ...] | None:
    """
    重试沿用此连接最初保存的投递规则。
    """
    frozen = _frozen_rules.get()
    return frozen.get(config.id) if frozen is not None else config.notification_rules


def read_or_freeze_notification_rules(
    snapshot: dict[str, JsonData], candidates: FrozenNotificationRules | None,
) -> tuple[FrozenNotificationRules | None, dict[str, JsonData]]:
    """
    首次保存已选连接的规则以便重试，已有分段的旧意图仍由原清单限制。
    """
    if _RULE_PLAN_KEY in snapshot:
        stored = snapshot[_RULE_PLAN_KEY]
        if not isinstance(stored, list):
            raise ValueError("已保存的通知投递规则格式无效")
        frozen: dict[str, tuple[NotificationRule, ...]] = {}
        for item in stored:
            if not (isinstance(item, list) and len(item) == 2 and isinstance(item[0], str) and isinstance(item[1], list)):
                raise ValueError("已保存的通知投递规则格式无效")
            frozen[item[0]] = tuple(NotificationRule.model_validate(rule) for rule in item[1])
        return frozen, snapshot
    if not candidates or not notification_plan_incomplete(snapshot):
        return None, snapshot
    serialized: list[JsonData] = [
        [key, [rule.model_dump(mode="json") for rule in rules]] for key, rules in candidates.items()
    ]
    return candidates, {**snapshot, _RULE_PLAN_KEY: serialized}


def read_or_freeze_notification_connections(
    snapshot: dict[str, JsonData], candidates: frozenset[tuple[str, str]] | None,
) -> tuple[frozenset[tuple[str, str]], dict[str, JsonData]]:
    """
    首次派发保存连接范围，恢复沿用原计划或已有回执的身份。

    :param snapshot: 当前 Outbox 的进度
    :param candidates: 首次尝试前看到的连接集合，恢复时为空
    :return: 已冻结的连接集合及持久化快照
    """
    if _CONNECTION_PLAN_KEY in snapshot:
        stored = snapshot[_CONNECTION_PLAN_KEY]
        if not isinstance(stored, list):
            raise ValueError("通知的连接计划格式无效")
        return frozenset(_validated_connection_binding(binding) for binding in stored), snapshot
    if candidates is None:
        candidates = _legacy_notification_connections(snapshot)
        if not candidates:
            raise ValueError("历史通知的连接身份无法确认")
    return candidates, {**snapshot, _CONNECTION_PLAN_KEY: [list(pair) for pair in sorted(candidates)]}


def _legacy_notification_connections(snapshot: dict[str, JsonData]) -> frozenset[tuple[str, str]]:
    """
    从旧版确认或占位记录恢复已经记录过的连接身份。
    """
    bindings: set[tuple[str, str]] = set()
    for item in snapshot.values():
        if not isinstance(item, dict):
            continue
        connection_id = item.get("connection_id")
        identity_namespace = item.get("identity_namespace")
        if isinstance(connection_id, str) and isinstance(identity_namespace, str):
            bindings.add((connection_id, identity_namespace))
    return frozenset(bindings)


def _validated_connection_binding(binding: JsonData) -> tuple[str, str]:
    """
    解析已保存的连接身份对，格式错误时暂停派发。
    """
    if not isinstance(binding, list) or len(binding) != 2:
        raise ValueError("通知的连接身份格式无效")
    connection_id, identity_namespace = binding
    if not isinstance(connection_id, str) or not isinstance(identity_namespace, str):
        raise ValueError("通知的连接身份格式无效")
    return connection_id, identity_namespace


def claim_notification_part(
    snapshot: dict[str, JsonData], part: DeliveryResult, *, initial_attempt: bool, should_submit: bool,
    planned_only: bool = False,
) -> tuple[DeliveryResult | None, dict[str, JsonData]]:
    """
    已完成或结果未知的分段不再提交，恢复时不接纳新目标或身份。
    """
    if not initial_attempt and not part.identity_namespace:
        return part.model_copy(update={"status": "permanent_failure", "code": "unverified_identity"}), snapshot
    key = notification_part_key(part)
    previous = snapshot.get(key)
    if isinstance(previous, dict):
        recorded = DeliveryResult.model_validate(previous)
        if recorded.status != "retryable_failure":
            return recorded, snapshot
        if recorded.content_digest and recorded.content_digest != part.content_digest:
            # 只终止确定未发出的旧分段。已确认或未知的原请求保留实际状态。
            rejected = recorded.model_copy(update={"status": "permanent_failure", "code": "content_changed"})
            return rejected, {**snapshot, key: rejected.model_dump(mode="json")}
    elif planned_only or not initial_attempt:
        blocked = part.model_copy(update={"status": "permanent_failure", "code": "unplanned_target"})
        return blocked, snapshot
    if part.status == "skipped" and part.code == "outside_rule_time":
        return part, merge_notification_progress(snapshot, (part,))
    if not should_submit:
        deferred = part.model_copy(update={"status": "retryable_failure", "code": "previous_part_unconfirmed"})
        return deferred, merge_notification_progress(snapshot, (deferred,))
    in_flight = part.model_copy(update={"status": "unknown", "code": "submission_unconfirmed"})
    return None, merge_notification_progress(snapshot, (in_flight,))


def pending_notification_parts(snapshot: dict[str, JsonData]) -> frozenset[str]:
    """
    找出仍需处理或查证且受分段保护的投递。
    """
    return frozenset(
        key for key, entry in snapshot.items()
        if isinstance(entry, dict) and entry.get("content_digest")
        and entry.get("status") in {"unknown", "retryable_failure"}
    )


def delivery_status(result: object) -> DeliveryStatus:
    """
    只采用底层实际提供的接受或拒绝证据。

    :param result: 传输层原始结果，或兼容平台代理的返回值
    :return: 平台明确确认或当前无法区分的结果
    """
    if isinstance(result, MessageResponse):
        return "confirmed" if result.success else "retryable_failure"
    if result is True:
        return "confirmed"
    if result is False:
        return "retryable_failure"
    if isinstance(result, tuple) and result:
        return delivery_status(result[0])
    if isinstance(result, Mapping):
        for key in ("success", "ok"):
            if key in result:
                return delivery_status(result[key])
        if result.get("message_id") is not None:
            return "confirmed"
    return "unknown"


def platform_message_id(result: object) -> str | None:
    """
    提取平台发送确认中的消息引用，仅供相同连接和目标对账。
    """
    if isinstance(result, MessageResponse):
        value = result.message_id
    elif isinstance(result, Mapping):
        value = result.get("message_id")
    elif isinstance(result, tuple) and len(result) > 1:
        return platform_message_id(result[1])
    else:
        return None
    return str(value) if type(value) in {str, int} else None
