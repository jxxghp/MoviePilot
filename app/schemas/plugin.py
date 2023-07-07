from pydantic import BaseModel


class Plugin(BaseModel):
    """
    插件信息
    """
    id: str = None
    # 插件名称
    plugin_name: str = None
    # 插件描述
    plugin_desc: str = None
    # 插件图标
    plugin_icon: str = None
    # 主题色
    plugin_color: str = None
    # 插件版本
    plugin_version: str = None
    # 插件作者
    plugin_author: str = None
    # 作者主页
    author_url: str = None
    # 插件配置项ID前缀
    plugin_config_prefix: str = None
    # 加载顺序
    plugin_order: int = 0
    # 可使用的用户级别
    auth_level: int = 0
    # 是否已安装
    installed: bool = False
