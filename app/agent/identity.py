"""
Agent 渠道绑定与自主任务执行账号的解析。

渠道发送者标识只用于绑定查询，执行账号始终使用持久化用户主键。
"""

from app.agent.tools.base import run_agent_blocking
from app.application.security.user import ChainUserRepository, UserSnapshot
from app.schemas.types import NotificationChannel


class AgentIdentityError(ValueError):
    """
    Agent 无法确认唯一且启用的真实执行账号。
    """


async def resolve_channel_user(
    users: ChainUserRepository,
    user_id: str,
    channel: str | None,
) -> UserSnapshot | None:
    """
    按渠道稳定标识查找唯一启用的绑定用户，不使用昵称作为认证依据。

    :param users: 宿主注入的用户查询仓储
    :param user_id: 渠道发送者的原始标识
    :param channel: 消息渠道名称；未知渠道返回 None
    :return: 唯一匹配的用户，未绑定或存在歧义时返回 None
    """
    keys = {
        NotificationChannel.Telegram.value: ("telegram_userid",),
        NotificationChannel.Discord.value: ("discord_userid",),
        NotificationChannel.Wechat.value: ("wechat_userid",),
        NotificationChannel.Feishu.value: ("feishu_userid", "feishu_openid"),
        NotificationChannel.WechatClawBot.value: ("wechatclawbot_userid",),
        NotificationChannel.Slack.value: ("slack_userid",),
        NotificationChannel.VoceChat.value: ("vocechat_userid",),
        NotificationChannel.SynologyChat.value: ("synologychat_userid",),
        NotificationChannel.QQ.value: ("qq_userid", "qq_openid"),
    }.get(channel or "")
    if not keys or not user_id:
        return None
    username = await run_agent_blocking(
        "db", users.find_name_by_bindings,
        {key: user_id for key in keys}, match_any=True,
    )
    return await users.async_get_by_name(username) if username else None


async def require_execution_user(
    users: ChainUserRepository | None,
    execution_user_id: int | None,
    expected_identity: str | None = None,
) -> UserSnapshot:
    """
    按已固定的执行用户主键复核账号，拒绝用户名回收和管理员回退。

    :param users: 宿主用户仓储；未装配时拒绝执行
    :param execution_user_id: 固定的用户主键；旧任务为空时须先从原会话更新绑定
    :param expected_identity: 固定的账号不可变身份；提供时拒绝主键被新账号复用
    :return: 当前仍然存在且启用的执行用户及实时权限
    :raises AgentIdentityError: 身份缺失、账号删除或停用
    """
    if execution_user_id is None:
        raise AgentIdentityError(
            "任务尚未绑定执行账号，请先确认 MoviePilot 账号的渠道绑定，"
            "再从创建任务的原会话使用 agent_task update 更新任务以绑定执行账号，或删除后重新创建。"
        )
    if users is None:
        raise AgentIdentityError("Agent 用户数据上下文未装配，无法校验任务执行账号。")
    user = await users.async_get_by_id(execution_user_id)
    if user is None:
        raise AgentIdentityError("任务执行账号已删除，请从原会话删除任务后重新创建；不会回退超级管理员。")
    if expected_identity is not None and user.notification_identity != expected_identity:
        raise AgentIdentityError("任务执行账号已被替换，请从原会话删除任务后重新创建。")
    if not user.is_active:
        raise AgentIdentityError("任务执行账号已停用，请联系管理员启用后重试。")
    return user


async def resolve_task_user(
    users: ChainUserRepository,
    *,
    user_id: str,
    username: str | None,
    channel: str | None,
    source: str | None,
    is_admin: bool,
    execution_user_id: int | None = None,
    execution_user_identity: str = "",
) -> UserSnapshot:
    """
    创建或显式更新旧任务时，将可信入口身份转换为真实用户。

    :param users: 宿主用户查询仓储
    :param user_id: 会话归属标识；渠道入口为渠道发送者 ID
    :param username: 入口用户名；渠道昵称不参与绑定解析
    :param channel: 原始入口渠道
    :param source: 原始入口来源
    :param is_admin: 可信入口注入的管理员标记，仅已认证集成可借用宿主身份
    :param execution_user_id: 当前自主任务的固定执行主体；嵌套创建时继续沿用
    :param execution_user_identity: 当前自主任务已固定的账号不可变身份
    :return: 唯一启用的真实用户
    :raises AgentIdentityError: 无法确定唯一启用用户
    """
    if execution_user_id is not None:
        return await require_execution_user(users, execution_user_id, execution_user_identity)
    integration = source == "api" or (
        channel == NotificationChannel.Web.value
        and source in {"openai", "openai.responses", "anthropic"}
    )
    if integration:
        if not is_admin:
            raise AgentIdentityError("API 集成未通过管理员身份验证，无法创建计划任务。")
        from app.application.security.auth import build_superuser_token_payload

        payload = await run_agent_blocking("db", build_superuser_token_payload)
        return await require_execution_user(users, int(payload.sub) if payload.sub is not None else None)
    if channel in {NotificationChannel.Web.value, NotificationChannel.WebAgent.value}:
        user = await users.async_get_by_id(int(user_id)) if user_id.isdigit() else None
    elif channel:
        user = await resolve_channel_user(users, user_id, channel)
    else:
        user = await users.async_get_by_name(username) if username else None
    if user is None:
        raise AgentIdentityError(
            "无法确定唯一且已启用的 MoviePilot 执行账号。请打开右上角头像 → 个人信息 → 账号绑定，"
            "填写当前渠道发送者的完整 ID，并由管理员确认没有重复绑定且账号已启用后重试。"
        )
    return await require_execution_user(users, user.id)
