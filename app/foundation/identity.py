from typing import Optional, Union

# 后台任务会话使用的内部占位用户ID。
# 它只用于在 agent/memory/session 侧标识“系统触发的任务”，
# 不能直接作为真实消息接收人下发到 Telegram/企业微信 等通知渠道。
SYSTEM_INTERNAL_USER_ID = "system"


def is_internal_user_id(userid: Optional[Union[str, int]]) -> bool:
    """
    判断是否为系统内部占位用户ID。
    """
    return (
        isinstance(userid, str)
        and userid.strip().lower() == SYSTEM_INTERNAL_USER_ID
    )


def normalize_internal_user_id(
    userid: Optional[Union[str, int]]
) -> Optional[Union[str, int]]:
    """
    将系统内部占位用户ID归一化为 None，避免被通知渠道误认为真实接收人。
    """
    if is_internal_user_id(userid):
        return None
    return userid
