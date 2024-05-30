
from pydantic import BaseModel


class Plugin(BaseModel):
    """
    插件信息
    """
    id: str = None
    # 插件名称
    plugin_name: str | None = None
    # 插件描述
    plugin_desc: str | None = None
    # 插件图标
    plugin_icon: str | None = None
    # 插件版本
    plugin_version: str | None = None
    # 插件标签
    plugin_label: str | None = None
    # 插件作者
    plugin_author: str | None = None
    # 作者主页
    author_url: str | None = None
    # 插件配置项ID前缀
    plugin_config_prefix: str | None = None
    # 加载顺序
    plugin_order: int | None = 0
    # 可使用的用户级别
    auth_level: int | None = 0
    # 是否已安装
    installed: bool | None = False
    # 运行状态
    state: bool | None = False
    # 是否有详情页面
    has_page: bool | None = False
    # 是否有新版本
    has_update: bool | None = False
    # 是否本地
    is_local: bool | None = False
    # 仓库地址
    repo_url: str | None = None
    # 安装次数
    install_count: int | None = 0
    # 更新记录
    history: dict | None = {}
    # 添加时间，值越小表示越靠后发布
    add_time: int | None = 0


class PluginDashboard(Plugin):
    """
    插件仪表盘
    """
    id: str | None = None
    # 名称
    name: str | None = None
    # 仪表板key
    key: str | None = None
    # 全局配置
    attrs: dict | None = {}
    # col列数
    cols: dict | None = {}
    # 页面元素
    elements: list[dict] | None = []
