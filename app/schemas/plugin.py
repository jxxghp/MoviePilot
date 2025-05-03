from typing import Optional, List

from pydantic import BaseModel, Field


class Plugin(BaseModel):
    """
    插件信息
    """
    id: str = None
    # 插件名称
    plugin_name: Optional[str] = None
    # 插件描述
    plugin_desc: Optional[str] = None
    # 插件图标
    plugin_icon: Optional[str] = None
    # 插件版本
    plugin_version: Optional[str] = None
    # 插件标签
    plugin_label: Optional[str] = None
    # 插件作者
    plugin_author: Optional[str] = None
    # 作者主页
    author_url: Optional[str] = None
    # 插件配置项ID前缀
    plugin_config_prefix: Optional[str] = None
    # 加载顺序
    plugin_order: Optional[int] = 0
    # 可使用的用户级别
    auth_level: Optional[int] = 0
    # 是否已安装
    installed: Optional[bool] = False
    # 运行状态
    state: Optional[bool] = False
    # 是否有详情页面
    has_page: Optional[bool] = False
    # 是否有新版本
    has_update: Optional[bool] = False
    # 是否本地
    is_local: Optional[bool] = False
    # 仓库地址
    repo_url: Optional[str] = None
    # 安装次数
    install_count: Optional[int] = 0
    # 更新记录
    history: Optional[dict] = Field(default_factory=dict)
    # 添加时间，值越小表示越靠后发布
    add_time: Optional[int] = 0
    # 插件公钥
    plugin_public_key: Optional[str] = None


class PluginDashboard(Plugin):
    """
    插件仪表盘
    """
    id: Optional[str] = None
    # 名称
    name: Optional[str] = None
    # 仪表板key
    key: Optional[str] = None
    # 演染模式
    render_mode: Optional[str] = Field(default="vuetify")
    # 全局配置
    attrs: Optional[dict] = Field(default_factory=dict)
    # col列数
    cols: Optional[dict] = Field(default_factory=dict)
    # 页面元素
    elements: Optional[List[dict]] = Field(default_factory=list)
    # 页面地址
    component_url: Optional[str] = None
