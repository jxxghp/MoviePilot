from enum import Enum


class MediaType(Enum):
    MOVIE = '电影'
    TV = '电视剧'
    UNKNOWN = '未知'


class TorrentStatus(Enum):
    TRANSFER = "可转移"
    DOWNLOADING = "下载中"


# 可监听事件
class EventType(Enum):
    # 插件重载
    PluginReload = "plugin.reload"
    # 执行命令
    CommandExcute = "command.excute"
    # 站点签到
    SiteSignin = "site.signin"
    # 站点数据统计
    SiteStatistic = "site.statistic"
    # 站点删除
    SiteDeleted = "site.deleted"
    # 豆瓣想看
    DoubanSync = "douban.sync"
    # Webhook消息
    WebhookMessage = "webhook.message"
    # 转移完成
    TransferComplete = "transfer.complete"
    # 添加下载
    DownloadAdded = "download.added"
    # 删除历史记录
    HistoryDeleted = "history.deleted"
    # 删除媒体库文件
    MediaDeleted = "media.deleted"
    # 用户外来消息
    UserMessage = "user.message"
    # 通知消息
    NoticeMessage = "notice.message"


# 系统配置Key字典
class SystemConfigKey(Enum):
    # 用户已安装的插件
    UserInstalledPlugins = "UserInstalledPlugins"
    # 搜索结果
    SearchResults = "SearchResults"
    # 搜索站点范围
    IndexerSites = "IndexerSites"
    # 订阅站点范围
    RssSites = "RssSites"
    # 种子优先级规则
    TorrentsPriority = "TorrentsPriority"
    # 通知消息渠道设置
    NotificationChannels = "NotificationChannels"
    # 自定义制作组/字幕组
    CustomReleaseGroups = "CustomReleaseGroups"
    # 自定义识别词
    CustomIdentifiers = "CustomIdentifiers"
    # 搜索优先级规则
    SearchFilterRules = "SearchFilterRules"
    # 订阅优先级规则
    SubscribeFilterRules = "SubscribeFilterRules"
    # 洗版规则
    BestVersionFilterRules = "BestVersionFilterRules"
    # 默认包含与排除规则
    DefaultIncludeExcludeFilter = "DefaultIncludeExcludeFilter"
    # 转移屏蔽词
    TransferExcludeWords = "TransferExcludeWords"


# 处理进度Key字典
class ProgressKey(Enum):
    # 搜索
    Search = "search"
    # 转移
    FileTransfer = "filetransfer"


# 媒体图片类型
class MediaImageType(Enum):
    Poster = "poster"
    Backdrop = "backdrop"


# 消息类型
class NotificationType(Enum):
    # 资源下载
    Download = "资源下载"
    # 整理入库
    Organize = "整理入库"
    # 订阅
    Subscribe = "订阅"
    # 站点消息
    SiteMessage = "站点消息"
    # 媒体服务器通知
    MediaServer = "媒体服务器通知"
    # 处理失败需要人工干预
    Manual = "手动处理通知"


class MessageChannel(Enum):
    """
    消息渠道
    """
    Wechat = "微信"
    Telegram = "Telegram"
    Slack = "Slack"
    SynologyChat = "SynologyChat"
