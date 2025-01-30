from enum import Enum


# 媒体类型
class MediaType(Enum):
    MOVIE = '电影'
    TV = '电视剧'
    COLLECTION = '系列'
    UNKNOWN = '未知'


# 种子状态
class TorrentStatus(Enum):
    TRANSFER = "可转移"
    DOWNLOADING = "下载中"


# 异步广播事件
class EventType(Enum):
    # 插件需要重载
    PluginReload = "plugin.reload"
    # 触发插件动作
    PluginAction = "plugin.action"
    # 插件触发事件
    PluginTriggered = "plugin.triggered"
    # 执行命令
    CommandExcute = "command.excute"
    # 站点已删除
    SiteDeleted = "site.deleted"
    # 站点已更新
    SiteUpdated = "site.updated"
    # 站点已刷新
    SiteRefreshed = "site.refreshed"
    # 转移完成
    TransferComplete = "transfer.complete"
    # 下载已添加
    DownloadAdded = "download.added"
    # 删除历史记录
    HistoryDeleted = "history.deleted"
    # 删除下载源文件
    DownloadFileDeleted = "downloadfile.deleted"
    # 删除下载任务
    DownloadDeleted = "download.deleted"
    # 收到用户外来消息
    UserMessage = "user.message"
    # 收到Webhook消息
    WebhookMessage = "webhook.message"
    # 发送消息通知
    NoticeMessage = "notice.message"
    # 订阅已添加
    SubscribeAdded = "subscribe.added"
    # 订阅已调整
    SubscribeModified = "subscribe.modified"
    # 订阅已删除
    SubscribeDeleted = "subscribe.deleted"
    # 订阅已完成
    SubscribeComplete = "subscribe.complete"
    # 系统错误
    SystemError = "system.error"
    # 刮削元数据
    MetadataScrape = "metadata.scrape"
    # 模块需要重载
    ModuleReload = "module.reload"


# 同步链式事件
class ChainEventType(Enum):
    # 名称识别
    NameRecognize = "name.recognize"
    # 认证验证
    AuthVerification = "auth.verification"
    # 认证拦截
    AuthIntercept = "auth.intercept"
    # 命令注册
    CommandRegister = "command.register"
    # 整理重命名
    TransferRename = "transfer.rename"
    # 整理拦截
    TransferIntercept = "transfer.intercept"
    # 资源选择
    ResourceSelection = "resource.selection"
    # 资源下载
    ResourceDownload = "resource.download"


# 系统配置Key字典
class SystemConfigKey(Enum):
    # 下载器配置
    Downloaders = "Downloaders"
    # 媒体服务器配置
    MediaServers = "MediaServers"
    # 消息通知配置
    Notifications = "Notifications"
    # 通知场景开关设置
    NotificationSwitchs = "NotificationSwitchs"
    # 目录配置
    Directories = "Directories"
    # 存储配置
    Storages = "Storages"
    # 搜索站点范围
    IndexerSites = "IndexerSites"
    # 订阅站点范围
    RssSites = "RssSites"
    # 自定义制作组/字幕组
    CustomReleaseGroups = "CustomReleaseGroups"
    # 自定义占位符
    Customization = "Customization"
    # 自定义识别词
    CustomIdentifiers = "CustomIdentifiers"
    # 转移屏蔽词
    TransferExcludeWords = "TransferExcludeWords"
    # 种子优先级规则
    TorrentsPriority = "TorrentsPriority"
    # 用户自定义规则
    CustomFilterRules = "CustomFilterRules"
    # 用户规则组
    UserFilterRuleGroups = "UserFilterRuleGroups"
    # 搜索默认过滤规则组
    SearchFilterRuleGroups = "SearchFilterRuleGroups"
    # 订阅默认过滤规则组
    SubscribeFilterRuleGroups = "SubscribeFilterRuleGroups"
    # 订阅默认参数
    SubscribeDefaultParams = "SubscribeDefaultParams"
    # 洗版默认过滤规则组
    BestVersionFilterRuleGroups = "BestVersionFilterRuleGroups"
    # 订阅统计
    SubscribeReport = "SubscribeReport"
    # 用户自定义CSS
    UserCustomCSS = "UserCustomCSS"
    # 用户已安装的插件
    UserInstalledPlugins = "UserInstalledPlugins"
    # 插件安装统计
    PluginInstallReport = "PluginInstallReport"
    # 默认电影订阅规则
    DefaultMovieSubscribeConfig = "DefaultMovieSubscribeConfig"
    # 默认电视剧订阅规则
    DefaultTvSubscribeConfig = "DefaultTvSubscribeConfig"
    # 用户站点认证参数
    UserSiteAuthParams = "UserSiteAuthParams"
    # Follow订阅分享者
    FollowSubscribers = "FollowSubscribers"


# 处理进度Key字典
class ProgressKey(Enum):
    # 搜索
    Search = "search"
    # 整理
    FileTransfer = "filetransfer"
    # 批量重命名
    BatchRename = "batchrename"


# 媒体图片类型
class MediaImageType(Enum):
    Poster = "poster_path"
    Backdrop = "backdrop_path"


# 消息类型
class NotificationType(Enum):
    # 资源下载
    Download = "资源下载"
    # 整理入库
    Organize = "整理入库"
    # 订阅
    Subscribe = "订阅"
    # 站点消息
    SiteMessage = "站点"
    # 媒体服务器通知
    MediaServer = "媒体服务器"
    # 处理失败需要人工干预
    Manual = "手动处理"
    # 插件消息
    Plugin = "插件"
    # 其它消息
    Other = "其它"


# 消息渠道
class MessageChannel(Enum):
    """
    消息渠道
    """
    Wechat = "微信"
    Telegram = "Telegram"
    Slack = "Slack"
    SynologyChat = "SynologyChat"
    VoceChat = "VoceChat"
    Web = "Web"
    WebPush = "WebPush"


# 下载器类型
class DownloaderType(Enum):
    # Qbittorrent
    Qbittorrent = "Qbittorrent"
    # Transmission
    Transmission = "Transmission"
    # Aria2
    # Aria2 = "Aria2"


# 媒体服务器类型
class MediaServerType(Enum):
    # Emby
    Emby = "Emby"
    # Jellyfin
    Jellyfin = "Jellyfin"
    # Plex
    Plex = "Plex"


# 识别器类型
class MediaRecognizeType(Enum):
    # 豆瓣
    Douban = "豆瓣"
    # TMDB
    TMDB = "TheMovieDb"
    # TVDB
    TVDB = "TheTvDb"
    # bangumi
    Bangumi = "Bangumi"


# 其他杂项模块类型
class OtherModulesType(Enum):
    # 字幕
    Subtitle = "站点字幕"
    # Fanart
    Fanart = "Fanart"
    # 文件整理
    FileManager = "文件整理"
    # 过滤器
    Filter = "过滤器"
    # 站点索引
    Indexer = "站点索引"


# 用户配置Key字典
class UserConfigKey(Enum):
    # 监控面板
    Dashboard = "Dashboard"


# 支持的存储类型
class StorageSchema(Enum):
    # 存储类型
    Local = "local"
    Alipan = "alipan"
    U115 = "u115"
    Rclone = "rclone"
    Alist = "alist"


# 模块类型
class ModuleType(Enum):
    # 下载器
    Downloader = "downloader"
    # 媒体服务器
    MediaServer = "mediaserver"
    # 消息服务
    Notification = "notification"
    # 媒体识别
    MediaRecognize = "mediarecognize"
    # 站点索引
    Indexer = "indexer"
    # 其它
    Other = "other"
