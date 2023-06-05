from enum import Enum


class MediaType(Enum):
    MOVIE = '电影'
    TV = '电视剧'
    UNKNOWN = '未知'


# 可监听事件
class EventType(Enum):
    # 插件重载
    PluginReload = "plugin.reload"
    # 执行命令
    CommandExcute = "command.excute"


# 系统配置Key字典
class SystemConfigKey(Enum):
    # 用户已安装的插件
    UserInstalledPlugins = "UserInstalledPlugins"
