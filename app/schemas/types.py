from enum import Enum
from typing import Optional


# 媒体类型
class MediaType(Enum):
    MOVIE = '电影'
    TV = '电视剧'
    COLLECTION = '系列'
    UNKNOWN = '未知'

    @staticmethod
    def from_agent(key: str) -> Optional["MediaType"]:
        """'movie' -> MediaType.MOVIE, 'tv' -> MediaType.TV, 否则 None"""
        _map = {"movie": MediaType.MOVIE, "tv": MediaType.TV}
        return _map.get(key.strip().lower() if key else "")

    def to_agent(self) -> str:
        """MediaType.MOVIE -> 'movie', MediaType.TV -> 'tv', 其他返回 .value"""
        return {MediaType.MOVIE: "movie", MediaType.TV: "tv"}.get(self, self.value)


def media_type_to_agent(value) -> Optional[str]:
    """将 MediaType 枚举或中文字符串统一转为 'movie'/'tv'"""
    if isinstance(value, MediaType):
        return value.to_agent()
    if isinstance(value, str):
        mt = MediaType.from_agent(value)
        return mt.to_agent() if mt else value
    return None


# 排序类型枚举
class SortType(Enum):
    TIME = "time"  # 按时间排序
    COUNT = "count"  # 按人数排序
    RATING = "rating"  # 按评分排序


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
    # 媒体文件整理完成
    TransferComplete = "transfer.complete"
    # 媒体文件整理失败
    TransferFailed = "transfer.failed"
    # 字幕整理完成
    SubtitleTransferComplete = "transfer.subtitle.complete"
    # 字幕整理失败
    SubtitleTransferFailed = "transfer.subtitle.failed"
    # 音频文件整理完成
    AudioTransferComplete = "transfer.audio.complete"
    # 音频文件整理失败
    AudioTransferFailed = "transfer.audio.failed"
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
    # 配置项更新
    ConfigChanged = "config.updated"
    # 消息交互动作
    MessageAction = "message.action"
    # 执行工作流
    WorkflowExecute = "workflow.execute"
    # Agent Tokens 用量
    AgentTokensUsage = "agent.tokens.usage"


# EventType中文名称翻译字典
EVENT_TYPE_NAMES = {
    EventType.PluginReload: "插件重载",
    EventType.PluginAction: "触发插件动作",
    EventType.PluginTriggered: "触发插件事件",
    EventType.CommandExcute: "执行命令",
    EventType.SiteDeleted: "站点已删除",
    EventType.SiteUpdated: "站点已更新",
    EventType.SiteRefreshed: "站点已刷新",
    EventType.TransferComplete: "整理完成",
    EventType.TransferFailed: "整理失败",
    EventType.SubtitleTransferComplete: "字幕整理完成",
    EventType.SubtitleTransferFailed: "字幕整理失败",
    EventType.AudioTransferComplete: "音频整理完成",
    EventType.AudioTransferFailed: "音频整理失败",
    EventType.DownloadAdded: "添加下载",
    EventType.HistoryDeleted: "删除历史记录",
    EventType.DownloadFileDeleted: "删除下载源文件",
    EventType.DownloadDeleted: "删除下载任务",
    EventType.UserMessage: "收到用户消息",
    EventType.WebhookMessage: "收到Webhook消息",
    EventType.NoticeMessage: "发送消息通知",
    EventType.SubscribeAdded: "添加订阅",
    EventType.SubscribeModified: "订阅已调整",
    EventType.SubscribeDeleted: "订阅已删除",
    EventType.SubscribeComplete: "订阅已完成",
    EventType.SystemError: "系统错误",
    EventType.MetadataScrape: "刮削元数据",
    EventType.ModuleReload: "模块重载",
    EventType.ConfigChanged: "配置项更新",
    EventType.MessageAction: "消息交互动作",
    EventType.WorkflowExecute: "执行工作流",
    EventType.AgentTokensUsage: "Agent Tokens 用量",
}


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
    # 整理重命名上下文构建
    TransferRenameBuild = "transfer.rename.build"
    # 整理拦截
    TransferIntercept = "transfer.intercept"
    # 整理覆盖检查
    TransferOverwriteCheck = "transfer.overwrite.check"
    # 资源选择
    ResourceSelection = "resource.selection"
    # 资源下载
    ResourceDownload = "resource.download"
    # 探索数据源
    DiscoverSource = "discover.source"
    # 媒体识别转换
    MediaRecognizeConvert = "media.recognize.convert"
    # 推荐数据源
    RecommendSource = "recommend.source"
    # 工作流执行
    WorkflowExecution = "workflow.execution"
    # 存储操作选择
    StorageOperSelection = "storage.operation"
    # Agent LLM 供应商选择
    AgentLLMProvider = "agent.llm.provider"
    # 订阅总集数刷新
    SubscribeEpisodesRefresh = "subscribe.episodes.refresh"
    # 订阅完成检查
    SubscribeCompletionCheck = "subscribe.completion.check"


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
    # 集数定位规则词表
    EpisodeFormatRuleTable = "EpisodeFormatRuleTable"
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
    # 插件文件夹分组配置
    PluginFolders = "PluginFolders"
    # 默认电影订阅规则
    DefaultMovieSubscribeConfig = "DefaultMovieSubscribeConfig"
    # 默认电视剧订阅规则
    DefaultTvSubscribeConfig = "DefaultTvSubscribeConfig"
    # 用户站点认证参数
    UserSiteAuthParams = "UserSiteAuthParams"
    # Follow订阅分享者
    FollowSubscribers = "FollowSubscribers"
    # 通知发送时间
    NotificationSendTime = "NotificationSendTime"
    # AI智能体配置
    AIAgentConfig = "AIAgentConfig"
    # 通知消息格式模板
    NotificationTemplates = "NotificationTemplates"
    # 刮削开关设置
    ScrapingSwitchs = "ScrapingSwitchs"
    # 插件安装统计
    PluginInstallReport = "PluginInstallReport"
    # 配置向导状态
    SetupWizardState = "SetupWizardState"
    # 绿联影视登录会话缓存
    UgreenSessionCache = "UgreenSessionCache"


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
    # 智能体消息
    Agent = "智能体"
    # 其它消息
    Other = "其它"


class ContentType(str, Enum):
    """
    消息内容类型
    操作状态的通知消息类型标识
    """
    # 订阅添加成功
    SubscribeAdded = "subscribeAdded"
    # 订阅完成
    SubscribeComplete = "subscribeComplete"
    # 入库成功
    OrganizeSuccess = "organizeSuccess"
    # 下载开始(添加下载任务成功)
    DownloadAdded = "downloadAdded"


# 消息渠道
class MessageChannel(Enum):
    """
    消息渠道
    """
    Wechat = "微信"
    Feishu = "飞书"
    WechatClawBot = "微信ClawBot"
    Telegram = "Telegram"
    Slack = "Slack"
    Discord = "Discord"
    SynologyChat = "SynologyChat"
    VoceChat = "VoceChat"
    Web = "Web"
    WebPush = "WebPush"
    QQ = "QQ"


# 下载器类型
class DownloaderType(Enum):
    # Qbittorrent
    Qbittorrent = "Qbittorrent"
    # Transmission
    Transmission = "Transmission"
    # Rtorrent
    Rtorrent = "Rtorrent"
    # Aria2
    # Aria2 = "Aria2"


# 媒体服务器类型
class MediaServerType(Enum):
    # Emby
    Emby = "Emby"
    # 极影视
    ZSpace = "ZSpace"
    # Jellyfin
    Jellyfin = "Jellyfin"
    # Plex
    Plex = "Plex"
    # 飞牛影视
    TrimeMedia = "TrimeMedia"
    # 绿联影视
    Ugreen = "Ugreen"


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
    SMB = "smb"


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
    # PostgreSQL
    PostgreSQL = "PostgreSQL"
    # Redis
    Redis = "Redis"


class NameValueEnum(Enum):
    """支持通过 name 或 value 实例化的枚举基类"""

    @classmethod
    def _missing_(cls, value):
        if isinstance(value, str):
            for member in cls:
                if member.name.lower() == value.lower() or member.value == value:
                    return member
        return None


# 刮削策略
class ScrapingPolicy(NameValueEnum):
    MISSINGONLY = "仅缺失"
    SKIP = "跳过"
    OVERWRITE = "覆盖"


# 刮削目标类型
class ScrapingTarget(NameValueEnum):
    MOVIE = "电影"
    TV = "电视剧"
    SEASON = "季"
    EPISODE = "集"


# 刮削元数据类型
class ScrapingMetadata(NameValueEnum):
    NFO = "NFO"
    POSTER = "海报"
    BACKDROP = "背景图"
    LOGO = "Logo"
    BANNER = "横幅图"
    THUMB = "缩略图"
    DISC = "光盘图"
    CLEARART = "透明艺术图"
    LANDSCAPE = "横版缩略图"
