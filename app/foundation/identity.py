import hashlib
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


def build_user_memory_key(userid: Optional[Union[str, int]]) -> Optional[str]:
    """
    为用户记忆目录生成不可逆且稳定的隔离键。

    用户 ID 可能来自消息渠道，不能直接拼接进文件路径；系统内部用户不拥有
    用户级记忆，返回 None 让调用方只使用公共记忆。
    """
    if userid is None or is_internal_user_id(userid):
        return None
    normalized = str(userid).strip()
    if not normalized:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
