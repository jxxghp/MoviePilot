"""
人工确认媒体身份的一次性整理授权。

自动重试预算只限制"同一个任务在被反复机器重试"，不限制"人工已经指定了确定身份"。
本模块把管理用户在整理历史页点击「智能助手整理」这一动作固化为可审计的凭据：
绑定历史、任务和结算版本，消费一次即失效，因此不会复活无限自动重试。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

# 与 pending 租约列共享的固定宽度 UTC 编码，可直接按字典序比较。
UTC_TIME_FORMAT = "%Y-%m-%d %H:%M:%S.%f"
# 授权只在用户等待助手分析这段时间内有效，避免长期留存的越权窗口。
MANUAL_IDENTITY_AUTHORIZATION_TTL_SECONDS = 30 * 60
# 记录签发入口，便于事后区分人工点击与未来的其它授权来源。
MANUAL_IDENTITY_AUTHORIZATION_SOURCE = "history_ai_redo"


def format_utc(value: datetime) -> str:
    """
    按租约列格式编码 UTC 时间。

    :param value: 待编码的时间
    :return: 固定宽度、可按字典序比较的 UTC 字符串
    """
    return value.astimezone(timezone.utc).strftime(UTC_TIME_FORMAT)


def authorization_expiry(
        now_utc: str,
        ttl_seconds: int = MANUAL_IDENTITY_AUTHORIZATION_TTL_SECONDS,
) -> str:
    """
    由签发时刻计算授权到期时刻。

    :param now_utc: 签发时刻的 UTC 编码
    :param ttl_seconds: 有效期秒数，必须为正
    :return: 到期时刻的 UTC 编码
    """
    if ttl_seconds <= 0:
        raise ValueError("授权有效期必须为正数")
    issued = datetime.strptime(now_utc, UTC_TIME_FORMAT).replace(tzinfo=timezone.utc)
    return format_utc(issued + timedelta(seconds=ttl_seconds))


@dataclass(frozen=True)
class ManualIdentityAuthorization:
    """
    一条已签发授权在读取时的快照。
    """

    history_id: int
    task_id: str
    settlement_revision: int
    issued_by: str
    issued_at: str
    expires_at: str
    used_at: Optional[str] = None
    used_by: Optional[str] = None
    media_source: Optional[str] = None
    media_id: Optional[str] = None


def authorization_rejection(
        authorization: Optional[ManualIdentityAuthorization],
        *,
        history_id: int,
        task_id: str,
        settlement_revision: int,
        now_utc: str,
) -> Optional[str]:
    """
    核对授权是否可用于本次身份修正。

    只回答"人工是否已经为这条失败历史、这个结算版本确认过身份"，不评价失败阶段或
    文件副作用；那些限制由调用方原有的安全检查继续负责。

    :param authorization: 待核对的授权快照；没有签发过时为 None
    :param history_id: 当前失败回执的历史标识
    :param task_id: 当前失败任务标识
    :param settlement_revision: 当前结算版本
    :param now_utc: 当前 UTC 时间编码
    :return: 不可用原因；可用时为 None
    """
    if authorization is None:
        return "整理任务自动重试次数已耗尽，请人工处理"
    if (authorization.history_id != history_id
            or authorization.task_id != task_id
            or authorization.settlement_revision != settlement_revision):
        return "人工授权与当前整理任务或结算版本不匹配，请重新点击智能助手整理"
    if authorization.used_at:
        return "这次人工授权已用于一次身份修正，请重新点击智能助手整理"
    if authorization.expires_at <= now_utc:
        return "人工授权已过期，请重新点击智能助手整理"
    return None


def authorization_actor_name(current_user: object) -> str:
    """
    归一化签发者名称，保证审计列非空且可读。

    :param current_user: 已通过管理权限校验的用户对象
    :return: 展示用签发者名称
    """
    name = getattr(current_user, "name", None)
    if isinstance(name, str) and name.strip():
        return name.strip()[:128]
    user_id = getattr(current_user, "id", None)
    return f"#{user_id}" if user_id else "unknown"
