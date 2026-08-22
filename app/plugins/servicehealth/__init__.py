"""服务实例健康探针。

本插件是智能体工具族声明式注册的参考实现：它只声明「有一个叫
``query_service_instance_health`` 的工具，实现类是谁，它做什么」，剩下的全部交给宿主——
按声明登记进工具目录、按标签决定哪些场景选得到它、按会话上下文注入调用者身份与权限。

工具本身回答的是一个宿主自己答不上来的问题：**用户配的那几台下载器、媒体服务器和消息
渠道，现在还连得上吗**。宿主的重连回路在后台按十分钟一轮跑，它把结果写进日志而不是交给
用户；配置页只显示「配了什么」，不显示「现在通不通」。工具把这条状态取出来，让用户能直接
问「我的 emby 掉线了吗」。

状态从哪来不是本插件自己定的：下载器与媒体服务器族必须实现 ``is_inactive``/``reconnect``、
消息通知族必须实现 ``get_state``，这三条是宿主对服务实例的必填契约，由
`app.sdk.service_instances` 交出。本插件按契约取只读的那一个来探，因此新增一族服务时，
只要那一族的必填集里有可读的状态方法，探针的口径不必跟着改。
"""

from typing import Any, Dict, List, Optional, Tuple

from app.sdk.declarations import AgentToolDeclaration
from app.sdk.extension import _PluginBase

from .probe import TOOL_DESCRIPTION, TOOL_NAME, ServiceInstanceHealthTool


class ServiceHealth(_PluginBase):
    """把服务实例在线状态接进智能体工具的插件。"""

    # 插件名称
    plugin_name = "服务实例健康探针"
    # 插件描述
    plugin_desc = "让智能体能查询已配置的下载器、媒体服务器与消息渠道当前是否连得上。"
    # 插件图标，取值是图标库里的文件名或一个绝对地址；本参考实现不自带图标，留空由前端兜底
    plugin_icon = None
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "MoviePilot"
    # 作者主页
    author_url = "https://github.com/jxxghp/MoviePilot"
    # 插件配置项ID前缀
    plugin_config_prefix = "servicehealth_"
    # 加载顺序
    plugin_order = 31
    # 可使用的用户级别
    auth_level = 1

    # 插件启用开关，用户尚未配置时按停用处理
    _enabled: bool = False

    def init_plugin(self, config: dict = None) -> None:
        """
        生效插件配置

        插件配置里只有启用开关：工具查的是用户在下载器、媒体服务器与通知设置里配好的实例，
        本插件不代为保存任何服务配置。

        :param config: 插件配置
        """
        self._enabled = bool((config or {}).get("enabled"))

    def get_state(self) -> bool:
        """
        返回插件启用状态

        :return: 插件是否已启用
        """
        return self._enabled

    def get_api(self) -> Optional[List[Dict[str, Any]]]:
        """
        注册插件 API

        工具由智能体调用，不经 HTTP 端点，本插件不需要自己的接口。

        :return: 恒为 None，表示不注册任何端点
        """
        return None

    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        """
        拼装插件配置页面

        :return: ``(组件树, 默认数据)`` 二元组
        """
        return [
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [
                            {
                                "component": "VSwitch",
                                "props": {
                                    "model": "enabled",
                                    "label": "启用插件",
                                    "hint": "关闭后智能体不再提供服务实例状态查询",
                                    "persistent-hint": True,
                                },
                            }
                        ],
                    }
                ],
            }
        ], {"enabled": False}

    def get_page(self) -> Optional[List[dict]]:
        """
        拼装插件详情页面

        实例状态经智能体对话交付，本插件没有自己的详情页。

        :return: 恒为 None，表示没有详情页
        """
        return None

    def provides_agent_tools(self) -> Optional[List[AgentToolDeclaration]]:
        """
        声明本插件提供的智能体工具

        ``name`` 与 ``description`` 写在声明上而不是只留在实现类里：宿主换实现语言、扩展
        改为独立进程时，这两个字段随其余声明数据原样成为握手报文，``impl`` 不参与传输。
        实现类的同名字段是宿主取不到声明字段时的兜底，两处取自同一组模块级常量。

        :return: 智能体工具声明列表
        """
        return [
            AgentToolDeclaration(
                name=TOOL_NAME,
                description=TOOL_DESCRIPTION,
                impl=ServiceInstanceHealthTool,
            )
        ]

    def stop_service(self) -> None:
        """
        停止插件

        工具实例由宿主按会话构造与释放，插件自身不持有需要显式关闭的资源。

        :return: 无返回值
        """
        return None
