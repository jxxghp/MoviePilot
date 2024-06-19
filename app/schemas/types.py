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
    # 插件需要重载
    PluginReload = "plugin.reload"
    # 插件动作
    PluginAction = "plugin.action"
    # 执行命令
    CommandExcute = "command.excute"
    # 站点已删除
    SiteDeleted = "site.deleted"
    # 站点已更新
    SiteUpdated = "site.updated"
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
    # 名称识别请求
    NameRecognize = "name.recognize"
    # 名称识别结果
    NameRecognizeResult = "name.recognize.result"
    # 订阅已添加
    SubscribeAdded = "subscribe.added"
    # 订阅已完成
    SubscribeComplete = "subscribe.complete"
    # 系统错误
    SystemError = "system.error"


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
    # 自定义占位符
    Customization = "Customization"
    # 自定义识别词
    CustomIdentifiers = "CustomIdentifiers"
    # 搜索优先级规则
    SearchFilterRules = "SearchFilterRules"
    # 订阅优先级规则
    SubscribeFilterRules = "SubscribeFilterRules"
    # 洗版规则
    BestVersionFilterRules = "BestVersionFilterRules"
    # 默认订阅过滤规则
    DefaultFilterRules = "DefaultFilterRules"
    # 默认搜索过滤规则
    DefaultSearchFilterRules = "DefaultSearchFilterRules"
    # 转移屏蔽词
    TransferExcludeWords = "TransferExcludeWords"
    # 插件安装统计
    PluginInstallReport = "PluginInstallReport"
    # 订阅统计
    SubscribeReport = "SubscribeReport"
    # 用户自定义CSS
    UserCustomCSS = "UserCustomCSS"
    # 下载目录定义
    DownloadDirectories = "DownloadDirectories"
    # 媒体库目录定义
    LibraryDirectories = "LibraryDirectories"
    # 阿里云盘认证参数
    UserAliyunParams = "UserAliyunParams"
    # 115网盘认证参数
    User115Params = "User115Params"


# 处理进度Key字典
class ProgressKey(Enum):
    # 搜索
    Search = "search"
    # 转移
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
    SiteMessage = "站点消息"
    # 媒体服务器通知
    MediaServer = "媒体服务器通知"
    # 处理失败需要人工干预
    Manual = "手动处理通知"
    # 插件消息
    Plugin = "插件消息"


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


# 用户配置Key字典
class UserConfigKey(Enum):
    # 监控面板
    Dashboard = "Dashboard"
