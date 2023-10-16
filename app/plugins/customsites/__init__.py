from typing import Any, List, Dict, Tuple
from urllib.parse import urlparse

from app.core.config import settings
from app.core.event import EventManager
from app.helper.cookiecloud import CookieCloudHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType


class CustomSites(_PluginBase):
    # 插件名称
    plugin_name = "自定义站点"
    # 插件描述
    plugin_desc = "增加自定义站点为签到和统计使用。"
    # 插件图标
    plugin_icon = "world.png"
    # 主题色
    plugin_color = "#9AC16C"
    # 插件版本
    plugin_version = "0.1"
    # 插件作者
    plugin_author = "lightolly"
    # 作者主页
    author_url = "https://github.com/lightolly"
    # 插件配置项ID前缀
    plugin_config_prefix = "customsites_"
    # 加载顺序
    plugin_order = 0
    # 可使用的用户级别
    auth_level = 2

    # 自定义站点起始 id
    site_id_base = 60000
    site_id_alloc = site_id_base

    # 私有属性
    cookie_cloud: CookieCloudHelper = None

    # 配置属性
    _enabled: bool = False
    """
    {
    "id": "站点ID",
    "name": "站点名称",
    "url": "站点地址",
    "cookie": "站点Cookie",
    "ua": "User-Agent",
    "proxy": "是否使用代理",
    "render": "是否仿真",
    }
    """
    _sites: list[Dict] = []
    """
    格式
    站点名称|url|是否仿真
    """
    _site_urls: str = ""

    def init_plugin(self, config: dict = None):
        self.cookie_cloud = CookieCloudHelper(
            server=settings.COOKIECLOUD_HOST,
            key=settings.COOKIECLOUD_KEY,
            password=settings.COOKIECLOUD_PASSWORD
        )

        del_sites = []
        sites = []
        new_site_urls = []
        # 配置
        if config:
            self._enabled = config.get("enabled", False)
            self._sites = config.get("sites", [])
            self._site_urls = config.get("site_urls", "")

            if not self._enabled:
                return

            site_urls = self._site_urls.splitlines()
            # 只保留 匹配site_urls的 sites
            urls = [site_url.split('|')[1] for site_url in site_urls]
            for site in self._sites:
                if site.get("url") not in urls:
                    del_sites.append(site)
                else:
                    sites.append(site)

            for item in site_urls:
                _, url, _ = item.split("|")
                if url in [site.get("url") for site in self._sites]:
                    continue
                else:
                    new_site_urls.append(item)

            # 获取待分配的最大ID
            alloc_ids = [site.get("id") for site in self._sites if site.get("id")] + [self.site_id_base]
            self.site_id_alloc = max(alloc_ids) + 1

            # 补全 site_id
            for item in new_site_urls:
                site_name, item, site_render = item.split("|")
                sites.append({
                    "id": self.site_id_alloc,
                    "name": site_name,
                    "url": item,
                    "render": True if site_render.upper() == 'Y' else False,
                    "cookie": "",
                })
                self.site_id_alloc += 1
            self._sites = sites
            # 保存配置
            self.sync_cookie()
            self.__update_config()

        # 通知站点删除
        for site in del_sites:
            self.delete_site(site.get("id"))
            logger.info(f"删除站点 {site.get('name')}")

    def get_state(self) -> bool:
        return self._enabled

    def __update_config(self):
        # 保存配置
        self.update_config(
            {
                "enabled": self._enabled,
                "sites": self._sites,
                "site_urls": self._site_urls
            }
        )

    def __get_site_by_domain(self, domain):
        for site in self._sites:
            site_domain = urlparse(site.get("url")).netloc
            if site_domain.endswith(domain):
                return site
        return None

    def sync_cookie(self):
        """
        通过CookieCloud同步站点Cookie
        """
        logger.info("开始同步CookieCloud站点 ...")
        cookies, msg = self.cookie_cloud.download()
        if not cookies:
            logger.error(f"CookieCloud同步失败：{msg}")
            return
        # 保存Cookie或新增站点
        _update_count = 0
        for domain, cookie in cookies.items():
            # 获取站点信息
            site_info = self.__get_site_by_domain(domain)
            if site_info:
                # 更新站点Cookie
                logger.info(f"更新站点 {domain} Cookie ...")
                site_info.update({"cookie": cookie})
                _update_count += 1

        # 处理完成
        ret_msg = f"更新了{_update_count}个站点，总{len(self._sites)}个站点"
        logger.info(f"自定义站点 Cookie同步成功：{ret_msg}")

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
                                            'model': 'site_urls',
                                            'label': '站点列表',
                                            'rows': 5,
                                            'placeholder': '每一行一个站点，配置方式：\n'
                                                           '站点名称|站点地址|是否仿真(Y/N)\n'
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
            "site_urls": [],
            "sites": self._sites
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        退出插件
        """
        pass

    @staticmethod
    def delete_site(site_id):
        """
        删除站点通知
        """
        # 插件站点删除
        EventManager().send_event(EventType.SiteDeleted,
                                  {
                                      "site_id": site_id
                                  })
