"""
正式版本通知配置的只读兼容字段；不用于运行时授权。
"""

from app.schemas.types import NotificationChannel

# 类型、渠道、旧普通用户键、旧管理员键和升级时保留的主用户键。
LEGACY_CHANNELS = {
    "telegram": (NotificationChannel.Telegram.value, "TELEGRAM_USERS", "TELEGRAM_ADMINS", "TELEGRAM_CHAT_ID"),
    "wechat": (NotificationChannel.Wechat.value, "", "WECHAT_ADMINS", "WECHAT_BOT_CHAT_ID"),
    "wechatclawbot": (
        NotificationChannel.WechatClawBot.value,
        "",
        "WECHATCLAWBOT_ADMINS",
        "WECHATCLAWBOT_DEFAULT_TARGET",
    ),
    "feishu": (NotificationChannel.Feishu.value, "", "FEISHU_ADMINS", "FEISHU_OPEN_ID"),
    "qqbot": (NotificationChannel.QQ.value, "", "QQBOT_ADMINS", "QQ_OPENID"),
    "discord": (NotificationChannel.Discord.value, "", "DISCORD_ADMINS", ""),
    "slack": (NotificationChannel.Slack.value, "", "SLACK_ADMINS", ""),
    "vocechat": (NotificationChannel.VoceChat.value, "", "VOCECHAT_ADMINS", ""),
    "synologychat": (NotificationChannel.SynologyChat.value, "", "SYNOLOGYCHAT_ADMINS", ""),
}
