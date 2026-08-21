"""123 云盘存储扩展。

本插件是存储族声明式注册的参考实现：它只声明「有一个叫 p123 的存储类型，后端类是
谁，配置是什么形状，界面长什么样」，剩下的全部交给宿主——按用户配的每一份账号扇出
一个具名实例，按存储令牌把请求路由到对应实例，实例停用时回收登记。

因此本插件不做两件事：

- 不用 ``get_module()`` 往宿主的方法分发面上挂 ``list_files``/``upload_file`` 这类方法。
  存储能力由 `P123Storage` 按 `StorageBase` 的契约提供，宿主自己知道该调谁。
- 不监听 ``StorageOperSelection`` 事件去抢认领。认领是地址问题，由存储令牌回答：
  ``p123@主号:/媒体库`` 指的是哪一个实例，宿主查登记表即可，用不着广播一圈看谁应声。

存储路径带实例名：``p123@主号:/媒体库`` 指名为「主号」的那份配置，裸令牌 ``p123:``
指本类型的默认实例。令牌的拆分与拼接一律用 `app.schemas.FileURI` 的工具，插件自己
不解析——``@`` 与 ``:`` 的优先级、Windows 盘符、非法实例名各有判据，各写一份必然分叉。
"""

from typing import Any, Dict, List, Optional, Tuple

from app.sdk.declarations import ServiceInstanceDeclaration
from app.sdk.extension import _PluginBase

from .config import STORAGE_CONFIG_SCHEMA, plugin_config_form, storage_config_form
from .storage import STORAGE_ID, P123Storage

# 存储类型在设置页与文件浏览器里的展示名称
STORAGE_NAME = "123云盘"

# 存储类型在前端的展示图标
STORAGE_ICON = "mdi-cloud-outline"


class P123Disk(_PluginBase):
    """把 123 云盘接入存储体系的插件。"""

    # 插件名称
    plugin_name = "123云盘存储"
    # 插件描述
    plugin_desc = "把 123 云盘接入存储体系，可配置多个账号，支持浏览、整理与上传下载。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/DDSRem-Dev/MoviePilot-Plugins/main/icons/P123Disk.png"
    # 插件版本
    plugin_version = "2.0.0"
    # 插件作者
    plugin_author = "DDSRem"
    # 作者主页
    author_url = "https://github.com/DDSRem"
    # 插件配置项ID前缀
    plugin_config_prefix = "p123disk_"
    # 加载顺序
    plugin_order = 99
    # 可使用的用户级别
    auth_level = 1

    # 插件启用开关，用户尚未配置时按停用处理
    _enabled: bool = False

    def init_plugin(self, config: dict = None) -> None:
        """
        生效插件配置

        插件配置里只有启用开关：123 云盘的账号属于存储实例，由用户在存储设置里按实例
        填写，插件不代为创建，也不把某一个账号当成「本插件的账号」。

        :param config: 插件配置
        """
        self._enabled = bool((config or {}).get("enabled"))

    def get_state(self) -> bool:
        """
        获取插件启用状态

        :return: 插件已启用时为 True
        """
        return self._enabled

    def get_api(self) -> Optional[List[Dict[str, Any]]]:
        """
        注册插件 API

        存储的浏览、整理与登录都走宿主既有的存储接口，本插件不需要自己的端点。

        :return: 恒为 None，表示不注册任何端点
        """
        return None

    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        """
        拼装插件配置页面

        :return: (组件树, 默认数据) 二元组
        """
        return plugin_config_form()

    def get_page(self) -> Optional[List[dict]]:
        """
        拼装插件详情页面

        存储的文件浏览由宿主的存储页承担，本插件没有自己的详情页。

        :return: 恒为 None，表示没有详情页
        """
        return None

    def provides_service_instances(self) -> Optional[List[ServiceInstanceDeclaration]]:
        """
        声明本插件提供的存储类型

        ``multi_instance=True``：用户配的第二份指的不是同一个东西——两个 123 云盘账号
        各有自己的空间、配额与文件树，甲账号里的 ``/媒体库`` 与乙账号里的 ``/媒体库``
        是两个不同的目录。主号存正片、备号存备份是这个网盘上的常见用法，因此每一份
        配置都要成为一个可独立寻址的实例。

        ``impl`` 在存储族里回答的是「按令牌取用时后端类是谁」，不是「怎么构造」：构造
        走宿主默认工厂，它按实例归属交付后端、配置由后端自己按令牌懒读，因此这里既不
        写工厂，也不把账号密码塞进声明。

        :return: 存储类型声明列表
        """
        return [
            ServiceInstanceDeclaration(
                capability="storage",
                type=STORAGE_ID,
                name=STORAGE_NAME,
                icon=STORAGE_ICON,
                multi_instance=True,
                impl=P123Storage,
                config_form=storage_config_form(),
                config_schema=STORAGE_CONFIG_SCHEMA,
            )
        ]

    def stop_service(self) -> None:
        """
        停止插件

        存储后端的登记由宿主按登记方回收，连接随存储对象一起释放，插件自身不持有
        需要显式关闭的资源。
        """
