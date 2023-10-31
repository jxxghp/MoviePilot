from typing import List, Tuple, Dict, Any

from python_hosts import Hosts, HostsEntry

from app.core.event import eventmanager
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType
from app.utils.ip import IpUtils
from app.utils.system import SystemUtils


class CustomHosts(_PluginBase):
    # 插件名称
    plugin_name = "自定义Hosts"
    # 插件描述
    plugin_desc = "修改系统hosts文件，加速网络访问。"
    # 插件图标
    plugin_icon = "hosts.png"
    # 主题色
    plugin_color = "#02C4E0"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "customhosts_"
    # 加载顺序
    plugin_order = 10
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _hosts = []
    _enabled = False

    def init_plugin(self, config: dict = None):
        # 读取配置
        if config:
            self._enabled = config.get("enabled")
            self._hosts = config.get("hosts")
            if isinstance(self._hosts, str):
                self._hosts = str(self._hosts).split('\n')
            if self._enabled and self._hosts:
                # 排除空的host
                new_hosts = []
                for host in self._hosts:
                    if host and host != '\n':
                        new_hosts.append(host.replace("\n", "") + "\n")
                self._hosts = new_hosts

                # 添加到系统
                error_flag, error_hosts = self.__add_hosts_to_system(self._hosts)
                self._enabled = self._enabled and not error_flag

                # 更新错误Hosts
                self.update_config({
                    "hosts": ''.join(self._hosts),
                    "err_hosts": error_hosts,
                    "enabled": self._enabled
                })

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        return [
                   {
                       'component': 'VForm',
                       'content': [
                           {
                               'component': 'VRow',
                               'content': [
                                   {
                                       'component': 'VCol',
                                       'props': {
                                           'cols': 12,
                                           'md': 6
                                       },
                                       'content': [
                                           {
                                               'component': 'VSwitch',
                                               'props': {
                                                   'model': 'enabled',
                                                   'label': '启用插件',
                                               }
                                           }
                                       ]
                                   }
                               ]
                           },
                           {
                               'component': 'VRow',
                               'content': [
                                   {
                                       'component': 'VCol',
                                       'props': {
                                           'cols': 12
                                       },
                                       'content': [
                                           {
                                               'component': 'VTextarea',
                                               'props': {
                                                   'model': 'hosts',
                                                   'label': '自定义hosts',
                                                   'rows': 10,
                                                   'placeholder': '每行一个配置，格式为：ip host1 host2 ...'
                                               }
                                           }
                                       ]
                                   }
                               ]
                           },
                           {
                               'component': 'VRow',
                               'content': [
                                   {
                                       'component': 'VCol',
                                       'props': {
                                           'cols': 12
                                       },
                                       'content': [
                                           {
                                               'component': 'VTextarea',
                                               'props': {
                                                   'model': 'err_hosts',
                                                   'readonly': True,
                                                   'label': '错误hosts',
                                                   'rows': 2,
                                                   'placeholder': '错误的hosts配置会展示在此处，请修改上方hosts重新提交（错误的hosts不会写入系统hosts文件）'
                                               }
                                           }
                                       ]
                                   }
                               ]
                           },
                           {
                               'component': 'VRow',
                               'content': [
                                   {
                                       'component': 'VCol',
                                       'props': {
                                           'cols': 12,
                                       },
                                       'content': [
                                           {
                                               'component': 'VAlert',
                                               'props': {
                                                   'type': 'info',
                                                   'variant': 'tonal',
                                                   'text': 'host格式ip host，中间有空格！！！'
                                                           '（注：容器运行则更新容器hosts！非宿主机！）'
                                               }
                                           }
                                       ]
                                   }
                               ]
                           }
                       ]
                   }
               ], {
                   "enabled": False,
                   "hosts": "",
                   "err_hosts": ""
               }

    def get_page(self) -> List[dict]:
        pass

    @staticmethod
    def __read_system_hosts():
        """
        读取系统hosts对象
        """
        # 获取本机hosts路径
        if SystemUtils.is_windows():
            hosts_path = r"c:\windows\system32\drivers\etc\hosts"
        else:
            hosts_path = '/etc/hosts'
        # 读取系统hosts
        return Hosts(path=hosts_path)

    def __add_hosts_to_system(self, hosts):
        """
        添加hosts到系统
        """
        # 系统hosts对象
        system_hosts = self.__read_system_hosts()
        # 过滤掉插件添加的hosts
        orgin_entries = []
        for entry in system_hosts.entries:
            if entry.entry_type == "comment" and entry.comment == "# CustomHostsPlugin":
                break
            orgin_entries.append(entry)
        system_hosts.entries = orgin_entries
        # 新的有效hosts
        new_entrys = []
        # 新的错误的hosts
        err_hosts = []
        err_flag = False
        for host in hosts:
            if not host:
                continue
            host_arr = str(host).split()
            try:
                host_entry = HostsEntry(entry_type='ipv4' if IpUtils.is_ipv4(str(host_arr[0])) else 'ipv6',
                                        address=host_arr[0],
                                        names=host_arr[1:])
                new_entrys.append(host_entry)
            except Exception as err:
                err_hosts.append(host + "\n")
                logger.error(f"[HOST] 格式转换错误：{str(err)}")
                # 推送实时消息
                self.systemmessage.put(f"[HOST] 格式转换错误：{str(err)}")

        # 写入系统hosts
        if new_entrys:
            try:
                # 添加分隔标识
                system_hosts.add([HostsEntry(entry_type='comment', comment="# CustomHostsPlugin")])
                # 添加新的Hosts
                system_hosts.add(new_entrys)
                system_hosts.write()
                logger.info("更新系统hosts文件成功")
            except Exception as err:
                err_flag = True
                logger.error(f"更新系统hosts文件失败：{str(err) or '请检查权限'}")
                # 推送实时消息
                self.systemmessage.put(f"更新系统hosts文件失败：{str(err) or '请检查权限'}")
        return err_flag, err_hosts

    def stop_service(self):
        """
        退出插件
        """
        pass

    @eventmanager.register(EventType.PluginReload)
    def reload(self, event):
        """
        响应插件重载事件
        """
        plugin_id = event.event_data.get("plugin_id")
        if not plugin_id:
            return
        if plugin_id != self.__class__.__name__:
            return
        return self.init_plugin(self.get_config())
