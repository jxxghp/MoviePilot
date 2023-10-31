from typing import Any, List, Dict, Tuple

from app.chain.site import SiteChain
from app.core.event import eventmanager
from app.db.site_oper import SiteOper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, NotificationType
from app.utils.string import StringUtils


class SiteRefresh(_PluginBase):
    # 插件名称
    plugin_name = "站点自动更新"
    # 插件描述
    plugin_desc = "自动登录获取站点Cookie和User-Agent。"
    # 插件图标
    plugin_icon = "login.png"
    # 主题色
    plugin_color = "#99b3ff"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "siterefresh_"
    # 加载顺序
    plugin_order = 2
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    siteoper: SiteOper = None

    # 配置属性
    _enabled: bool = False
    _notify: bool = False
    """
    格式
    站点domain|用户名|用户密码
    """
    _siteconf: list = []

    def init_plugin(self, config: dict = None):
        self.siteoper = SiteOper()
        # 配置
        if config:
            self._enabled = config.get("enabled")
            self._notify = config.get("notify")
            self._siteconf = str(config.get("siteconf")).split('\n')

    def get_state(self) -> bool:
        return self._enabled

    @eventmanager.register(EventType.SiteLogin)
    def site_login(self, event):
        """
        开始站点登录
        """
        if not self.get_state():
            return

        # 站点id
        site_id = event.event_data.get("site_id")
        if not site_id:
            logger.error(f"未获取到site_id")
            return

        site = self.siteoper.get(site_id)
        if not site:
            logger.error(f"未获取到site_id {site_id} 对应的站点数据")
            return

        site_name = site.name
        logger.info(f"开始尝试登录站点 {site_name}")
        siteurl, siteuser, sitepwd = None, None, None
        # 判断site是否已配置用户名密码
        for site_conf in self._siteconf:
            if not site_conf:
                continue
            site_confs = str(site_conf).split("|")
            if len(site_confs) == 3:
                siteurl = site_confs[0]
                siteuser = site_confs[1]
                sitepwd = site_confs[2]
            else:
                logger.error(f"{site_conf}配置有误，已跳过")
                continue

            # 判断是否是目标域名
            if str(siteurl) in StringUtils.get_url_domain(site.url):
                # 找到目标域名配置，跳出循环
                break

        # 开始登录更新cookie和ua
        if siteurl and siteuser and sitepwd:
            state, messages = SiteChain().update_cookie(site_info=site,
                                                        username=siteuser,
                                                        password=sitepwd)
            if state:
                logger.info(f"站点{site_name}自动更新Cookie和Ua成功")
            else:
                logger.error(f"站点{site_name}自动更新Cookie和Ua失败")

            if self._notify:
                self.post_message(mtype=NotificationType.SiteMessage,
                                  title=f"站点 {site_name} Cookie已失效。",
                                  text=f"自动更新Cookie和Ua{'成功' if state else '失败'}")
        else:
            logger.error(f"未获取到站点{site_name}配置，已跳过")

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
                            },
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
                                            'model': 'notify',
                                            'label': '开启通知',
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
                                            'model': 'siteconf',
                                            'label': '站点配置',
                                            'rows': 5,
                                            'placeholder': '每一行一个站点，配置方式：\n'
                                                           '域名domain|用户名|用户密码\n'
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
                                            'text': '站点签到提示Cookie过期时自动触发。'
                                                    '不支持开启两步认证的站点。'
                                                    '不是所有站点都支持，失败请手动更新。'
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
            "notify": False,
            "siteconf": ""
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        退出插件
        """
        pass
