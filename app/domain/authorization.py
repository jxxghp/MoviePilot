"""
通知渠道授权的旧配置映射与权限范围。

旧投递接收人只在升级时用于保留已有管理员身份，运行时不再据此授权。
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

# 新成员默认拥有的业务能力；定时任务和管理员能力必须单独授予。
DEFAULT_MEMBER_SCOPES = ("search", "subscribe", "download")
# 授权系统接受的完整能力集合，管理员拥有全部能力。
AUTHORIZATION_SCOPES = (*DEFAULT_MEMBER_SCOPES, "schedule")
# 原账号绑定字段用于升级时找到原业务归属，不使用昵称推断账号。
CHANNEL_BINDINGS = {
    NotificationChannel.Telegram.value: ("telegram_userid",),
    NotificationChannel.Wechat.value: ("wechat_userid",),
    NotificationChannel.WechatClawBot.value: ("wechatclawbot_userid",),
    NotificationChannel.Feishu.value: ("feishu_userid", "feishu_openid"),
    NotificationChannel.QQ.value: ("qq_userid", "qq_openid"),
    NotificationChannel.Discord.value: ("discord_userid",),
    NotificationChannel.Slack.value: ("slack_userid",),
    NotificationChannel.VoceChat.value: ("vocechat_userid",),
    NotificationChannel.SynologyChat.value: ("synologychat_userid",),
}

# 普通渠道成员仅开放明确审计过的数据归属操作，其余能力由管理员执行。
CHANNEL_OPERATION_SCOPES = {
    "media.recognize": "search",
    "search.torrents": "search",
    "download.add": "download",
    "download.artist_collection": "download",
    "download.history.list": "download",
    "download.tasks.active": "download",
    "download.clients": "download",
    "download.paths": "download",
    "subscription.add": "subscribe",
    "subscription.update": "subscribe",
    "subscription.search": "subscribe",
    "subscription.list": "subscribe",
    "subscription.get": "subscribe",
    "subscription.find": "subscribe",
    "subscription.history": "subscribe",
    "subscription.delete": "subscribe",
    "subscription.delete_by_media": "subscribe",
    "subscription.status.update": "subscribe",
    "subscription.reset": "subscribe",
    "subscription.files": "subscribe",
    "subscription.execution.list": "subscribe",
    "subscription.execution.get": "subscribe",
    "subscription.execution.cancel": "subscribe",
}

# 已审计的公共资料查询，已授权成员也可以在固定自身身份下读取。
CHANNEL_PUBLIC_OPERATIONS = frozenset(
    {
        "media.search",
        "media.person.search",
        "media.person.credits",
        "media.episode_schedule",
        "media.detail",
        "media.episode_groups",
        "media.episode_group.seasons",
        "media.seasons",
        "music.recognize",
        "music.explore",
        "music.album.get",
        "music.album.related",
        "music.artist.get",
        "music.artist.albums",
        "music.artist.related",
        "recommendation.list",
        "subscription.shares",
        "subscription.popular",
        "subscription.share.statistics",
        "plugin.statistics",
    }
)
