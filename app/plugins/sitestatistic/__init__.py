import re
import warnings
from datetime import datetime, timedelta
from multiprocessing.dummy import Pool as ThreadPool
from threading import Lock
from typing import Optional, Any, List, Dict, Tuple

import pytz
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from ruamel.yaml import CommentedMap

from app import schemas
from app.core.config import settings
from app.core.event import Event
from app.core.event import eventmanager
from app.db.models.site import Site
from app.db.site_oper import SiteOper
from app.helper.browser import PlaywrightHelper
from app.helper.module import ModuleHelper
from app.helper.sites import SitesHelper
from app.log import logger
from app.plugins import _PluginBase
from app.plugins.sitestatistic.siteuserinfo import ISiteUserInfo
from app.schemas.types import EventType, NotificationType
from app.utils.http import RequestUtils
from app.utils.string import StringUtils
from app.utils.timer import TimerUtils

warnings.filterwarnings("ignore", category=FutureWarning)

lock = Lock()


class SiteStatistic(_PluginBase):
    # 插件名称
    plugin_name = "站点数据统计"
    # 插件描述
    plugin_desc = "自动统计和展示站点数据。"
    # 插件图标
    plugin_icon = "statistic.png"
    # 主题色
    plugin_color = "#324A5E"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "lightolly"
    # 作者主页
    author_url = "https://github.com/lightolly"
    # 插件配置项ID前缀
    plugin_config_prefix = "sitestatistic_"
    # 加载顺序
    plugin_order = 1
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    sites = None
    siteoper = None
    _scheduler: Optional[BackgroundScheduler] = None
    _last_update_time: Optional[datetime] = None
    _sites_data: dict = {}
    _site_schema: List[ISiteUserInfo] = None

    # 配置属性
    _enabled: bool = False
    _onlyonce: bool = False
    _cron: str = ""
    _notify: bool = False
    _queue_cnt: int = 5
    _statistic_type: str = None
    _statistic_sites: list = []

    def init_plugin(self, config: dict = None):
        self.sites = SitesHelper()
        self.siteoper = SiteOper()
        # 停止现有任务
        self.stop_service()

        # 配置
        if config:
            self._enabled = config.get("enabled")
            self._onlyonce = config.get("onlyonce")
            self._cron = config.get("cron")
            self._notify = config.get("notify")
            self._queue_cnt = config.get("queue_cnt")
            self._statistic_type = config.get("statistic_type") or "all"
            self._statistic_sites = config.get("statistic_sites") or []

            # 过滤掉已删除的站点
            all_sites = [site.id for site in self.siteoper.list_order_by_pri()] + [site.get("id") for site in
                                                                                   self.__custom_sites()]
            self._statistic_sites = [site_id for site_id in all_sites if site_id in self._statistic_sites]
            self.__update_config()

        if self._enabled or self._onlyonce:
            # 加载模块
            self._site_schema = ModuleHelper.load('app.plugins.sitestatistic.siteuserinfo',
                                                  filter_func=lambda _, obj: hasattr(obj, 'schema'))

            # 定时服务
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)

            self._site_schema.sort(key=lambda x: x.order)
            # 站点上一次更新时间
            self._last_update_time = None
            # 站点数据
            self._sites_data = {}

            # 立即运行一次
            if self._onlyonce:
                logger.info(f"站点数据统计服务启动，立即运行一次")
                self._scheduler.add_job(self.refresh_all_site_data, 'date',
                                        run_date=datetime.now(
                                            tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3)
                                        )
                # 关闭一次性开关
                self._onlyonce = False

                # 保存配置
                self.__update_config()

            # 周期运行
            if self._enabled and self._cron:
                try:
                    self._scheduler.add_job(func=self.refresh_all_site_data,
                                            trigger=CronTrigger.from_crontab(self._cron),
                                            name="站点数据统计")
                except Exception as err:
                    logger.error(f"定时任务配置错误：{str(err)}")
                    # 推送实时消息
                    self.systemmessage.put(f"执行周期配置错误：{str(err)}")
            else:
                triggers = TimerUtils.random_scheduler(num_executions=1,
                                                       begin_hour=0,
                                                       end_hour=1,
                                                       min_interval=1,
                                                       max_interval=60)
                for trigger in triggers:
                    self._scheduler.add_job(self.refresh_all_site_data, "cron",
                                            hour=trigger.hour, minute=trigger.minute,
                                            name="站点数据统计")

            # 启动任务
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """
        定义远程控制命令
        :return: 命令关键字、事件、描述、附带数据
        """
        return [{
            "cmd": "/site_statistic",
            "event": EventType.SiteStatistic,
            "desc": "站点数据统计",
            "category": "站点",
            "data": {}
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        """
        获取插件API
        [{
            "path": "/xx",
            "endpoint": self.xxx,
            "methods": ["GET", "POST"],
            "summary": "API说明"
        }]
        """
        return [{
            "path": "/refresh_by_domain",
            "endpoint": self.refresh_by_domain,
            "methods": ["GET"],
            "summary": "刷新站点数据",
            "description": "刷新对应域名的站点数据",
        }]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        # 站点的可选项（内置站点 + 自定义站点）
        customSites = self.__custom_sites()

        site_options = ([{"title": site.name, "value": site.id}
                        for site in self.siteoper.list_order_by_pri()]
                        + [{"title": site.get("name"), "value": site.get("id")}
                           for site in customSites])

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
                                    'md': 4
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
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'notify',
                                            'label': '发送通知',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'onlyonce',
                                            'label': '立即运行一次',
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
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '执行周期',
                                            'placeholder': '5位cron表达式，留空自动'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'queue_cnt',
                                            'label': '队列数量'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'statistic_type',
                                            'label': '统计类型',
                                            'items': [
                                                {'title': '全量', 'value': 'all'},
                                                {'title': '增量', 'value': 'add'}
                                            ]
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
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'chips': True,
                                            'multiple': True,
                                            'model': 'statistic_sites',
                                            'label': '统计站点',
                                            'items': site_options
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
            "onlyonce": False,
            "notify": True,
            "cron": "5 1 * * *",
            "queue_cnt": 5,
            "statistic_type": "all",
            "statistic_sites": []
        }

    def get_page(self) -> List[dict]:
        """
        拼装插件详情页面，需要返回页面配置，同时附带数据
        """
        #
        # 最近两天的日期数组
        date_list = [(datetime.now() - timedelta(days=i)).date() for i in range(2)]
        # 最近一天的签到数据
        stattistic_data: Dict[str, Dict[str, Any]] = {}
        for day in date_list:
            current_day = day.strftime("%Y-%m-%d")
            stattistic_data = self.get_data(current_day)
            if stattistic_data:
                break
        if not stattistic_data:
            return [
                {
                    'component': 'div',
                    'text': '暂无数据',
                    'props': {
                        'class': 'text-center',
                    }
                }
            ]
        # 数据按时间降序排序
        stattistic_data = dict(sorted(stattistic_data.items(),
                                      key=lambda item: item[1].get('upload') or 0,
                                      reverse=True))
        # 总上传量
        total_upload = sum([data.get("upload")
                            for data in stattistic_data.values() if data.get("upload")])
        # 总下载量
        total_download = sum([data.get("download")
                              for data in stattistic_data.values() if data.get("download")])
        # 总做种数
        total_seed = sum([data.get("seeding")
                          for data in stattistic_data.values() if data.get("seeding")])
        # 总做种体积
        total_seed_size = sum([data.get("seeding_size")
                               for data in stattistic_data.values() if data.get("seeding_size")])

        # 站点数据明细
        site_trs = [
            {
                'component': 'tr',
                'props': {
                    'class': 'text-sm'
                },
                'content': [
                    {
                        'component': 'td',
                        'props': {
                            'class': 'whitespace-nowrap break-keep text-high-emphasis'
                        },
                        'text': site
                    },
                    {
                        'component': 'td',
                        'text': data.get("username")
                    },
                    {
                        'component': 'td',
                        'text': data.get("user_level")
                    },
                    {
                        'component': 'td',
                        'props': {
                            'class': 'text-success'
                        },
                        'text': StringUtils.str_filesize(data.get("upload"))
                    },
                    {
                        'component': 'td',
                        'props': {
                            'class': 'text-error'
                        },
                        'text': StringUtils.str_filesize(data.get("download"))
                    },
                    {
                        'component': 'td',
                        'text': data.get('ratio')
                    },
                    {
                        'component': 'td',
                        'text': '{:,.1f}'.format(data.get('bonus') or 0)
                    },
                    {
                        'component': 'td',
                        'text': data.get('seeding')
                    },
                    {
                        'component': 'td',
                        'text': StringUtils.str_filesize(data.get('seeding_size'))
                    }
                ]
            } for site, data in stattistic_data.items() if not data.get("err_msg")
        ]

        # 拼装页面
        return [
            {
                'component': 'VRow',
                'content': [
                    # 总上传量
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                            'md': 3,
                            'sm': 6
                        },
                        'content': [
                            {
                                'component': 'VCard',
                                'props': {
                                    'variant': 'tonal',
                                },
                                'content': [
                                    {
                                        'component': 'VCardText',
                                        'props': {
                                            'class': 'd-flex align-center',
                                        },
                                        'content': [
                                            {
                                                'component': 'VAvatar',
                                                'props': {
                                                    'rounded': True,
                                                    'variant': 'text',
                                                    'class': 'me-3'
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VImg',
                                                        'props': {
                                                            'src': '/plugin_icon/upload.png'
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'div',
                                                'content': [
                                                    {
                                                        'component': 'span',
                                                        'props': {
                                                            'class': 'text-caption'
                                                        },
                                                        'text': '总上传量'
                                                    },
                                                    {
                                                        'component': 'div',
                                                        'props': {
                                                            'class': 'd-flex align-center flex-wrap'
                                                        },
                                                        'content': [
                                                            {
                                                                'component': 'span',
                                                                'props': {
                                                                    'class': 'text-h6'
                                                                },
                                                                'text': StringUtils.str_filesize(total_upload)
                                                            }
                                                        ]
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                ]
                            },
                        ]
                    },
                    # 总下载量
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                            'md': 3,
                            'sm': 6
                        },
                        'content': [
                            {
                                'component': 'VCard',
                                'props': {
                                    'variant': 'tonal',
                                },
                                'content': [
                                    {
                                        'component': 'VCardText',
                                        'props': {
                                            'class': 'd-flex align-center',
                                        },
                                        'content': [
                                            {
                                                'component': 'VAvatar',
                                                'props': {
                                                    'rounded': True,
                                                    'variant': 'text',
                                                    'class': 'me-3'
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VImg',
                                                        'props': {
                                                            'src': '/plugin_icon/download.png'
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'div',
                                                'content': [
                                                    {
                                                        'component': 'span',
                                                        'props': {
                                                            'class': 'text-caption'
                                                        },
                                                        'text': '总下载量'
                                                    },
                                                    {
                                                        'component': 'div',
                                                        'props': {
                                                            'class': 'd-flex align-center flex-wrap'
                                                        },
                                                        'content': [
                                                            {
                                                                'component': 'span',
                                                                'props': {
                                                                    'class': 'text-h6'
                                                                },
                                                                'text': StringUtils.str_filesize(total_download)
                                                            }
                                                        ]
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                ]
                            },
                        ]
                    },
                    # 总做种数
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                            'md': 3,
                            'sm': 6
                        },
                        'content': [
                            {
                                'component': 'VCard',
                                'props': {
                                    'variant': 'tonal',
                                },
                                'content': [
                                    {
                                        'component': 'VCardText',
                                        'props': {
                                            'class': 'd-flex align-center',
                                        },
                                        'content': [
                                            {
                                                'component': 'VAvatar',
                                                'props': {
                                                    'rounded': True,
                                                    'variant': 'text',
                                                    'class': 'me-3'
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VImg',
                                                        'props': {
                                                            'src': '/plugin_icon/seed.png'
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'div',
                                                'content': [
                                                    {
                                                        'component': 'span',
                                                        'props': {
                                                            'class': 'text-caption'
                                                        },
                                                        'text': '总做种数'
                                                    },
                                                    {
                                                        'component': 'div',
                                                        'props': {
                                                            'class': 'd-flex align-center flex-wrap'
                                                        },
                                                        'content': [
                                                            {
                                                                'component': 'span',
                                                                'props': {
                                                                    'class': 'text-h6'
                                                                },
                                                                'text': f'{"{:,}".format(total_seed)}'
                                                            }
                                                        ]
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                ]
                            },
                        ]
                    },
                    # 总做种体积
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                            'md': 3,
                            'sm': 6
                        },
                        'content': [
                            {
                                'component': 'VCard',
                                'props': {
                                    'variant': 'tonal',
                                },
                                'content': [
                                    {
                                        'component': 'VCardText',
                                        'props': {
                                            'class': 'd-flex align-center',
                                        },
                                        'content': [
                                            {
                                                'component': 'VAvatar',
                                                'props': {
                                                    'rounded': True,
                                                    'variant': 'text',
                                                    'class': 'me-3'
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VImg',
                                                        'props': {
                                                            'src': '/plugin_icon/database.png'
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'div',
                                                'content': [
                                                    {
                                                        'component': 'span',
                                                        'props': {
                                                            'class': 'text-caption'
                                                        },
                                                        'text': '总做种体积'
                                                    },
                                                    {
                                                        'component': 'div',
                                                        'props': {
                                                            'class': 'd-flex align-center flex-wrap'
                                                        },
                                                        'content': [
                                                            {
                                                                'component': 'span',
                                                                'props': {
                                                                    'class': 'text-h6'
                                                                },
                                                                'text': StringUtils.str_filesize(total_seed_size)
                                                            }
                                                        ]
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
                    },
                    # 各站点数据明细
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                        },
                        'content': [
                            {
                                'component': 'VTable',
                                'props': {
                                    'hover': True
                                },
                                'content': [
                                    {
                                        'component': 'thead',
                                        'content': [
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': '站点'
                                            },
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': '用户名'
                                            },
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': '用户等级'
                                            },
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': '上传量'
                                            },
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': '下载量'
                                            },
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': '分享率'
                                            },
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': '魔力值'
                                            },
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': '做种数'
                                            },
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': '做种体积'
                                            }
                                        ]
                                    },
                                    {
                                        'component': 'tbody',
                                        'content': site_trs
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ]

    def stop_service(self):
        """
        退出插件
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error("退出插件失败：%s" % str(e))

    def __build_class(self, html_text: str) -> Any:
        for site_schema in self._site_schema:
            try:
                if site_schema.match(html_text):
                    return site_schema
            except Exception as e:
                logger.error(f"站点匹配失败 {str(e)}")
        return None

    def build(self, site_info: CommentedMap) -> Optional[ISiteUserInfo]:
        """
        构建站点信息
        """
        site_cookie = site_info.get("cookie")
        if not site_cookie:
            return None
        site_name = site_info.get("name")
        url = site_info.get("url")
        proxy = site_info.get("proxy")
        ua = site_info.get("ua")
        # 会话管理
        with requests.Session() as session:
            proxies = settings.PROXY if proxy else None
            proxy_server = settings.PROXY_SERVER if proxy else None
            render = site_info.get("render")

            logger.debug(f"站点 {site_name} url={url} site_cookie={site_cookie} ua={ua}")
            if render:
                # 演染模式
                html_text = PlaywrightHelper().get_page_source(url=url,
                                                               cookies=site_cookie,
                                                               ua=ua,
                                                               proxies=proxy_server)
            else:
                # 普通模式
                res = RequestUtils(cookies=site_cookie,
                                   session=session,
                                   ua=ua,
                                   proxies=proxies
                                   ).get_res(url=url)
                if res and res.status_code == 200:
                    if re.search(r"charset=\"?utf-8\"?", res.text, re.IGNORECASE):
                        res.encoding = "utf-8"
                    else:
                        res.encoding = res.apparent_encoding
                    html_text = res.text
                    # 第一次登录反爬
                    if html_text.find("title") == -1:
                        i = html_text.find("window.location")
                        if i == -1:
                            return None
                        tmp_url = url + html_text[i:html_text.find(";")] \
                            .replace("\"", "") \
                            .replace("+", "") \
                            .replace(" ", "") \
                            .replace("window.location=", "")
                        res = RequestUtils(cookies=site_cookie,
                                           session=session,
                                           ua=ua,
                                           proxies=proxies
                                           ).get_res(url=tmp_url)
                        if res and res.status_code == 200:
                            if "charset=utf-8" in res.text or "charset=UTF-8" in res.text:
                                res.encoding = "UTF-8"
                            else:
                                res.encoding = res.apparent_encoding
                            html_text = res.text
                            if not html_text:
                                return None
                        else:
                            logger.error("站点 %s 被反爬限制：%s, 状态码：%s" % (site_name, url, res.status_code))
                            return None

                    # 兼容假首页情况，假首页通常没有 <link rel="search" 属性
                    if '"search"' not in html_text and '"csrf-token"' not in html_text:
                        res = RequestUtils(cookies=site_cookie,
                                           session=session,
                                           ua=ua,
                                           proxies=proxies
                                           ).get_res(url=url + "/index.php")
                        if res and res.status_code == 200:
                            if re.search(r"charset=\"?utf-8\"?", res.text, re.IGNORECASE):
                                res.encoding = "utf-8"
                            else:
                                res.encoding = res.apparent_encoding
                            html_text = res.text
                            if not html_text:
                                return None
                elif res is not None:
                    logger.error(f"站点 {site_name} 连接失败，状态码：{res.status_code}")
                    return None
                else:
                    logger.error(f"站点 {site_name} 无法访问：{url}")
                    return None
            # 解析站点类型
            if html_text:
                site_schema = self.__build_class(html_text)
                if not site_schema:
                    logger.error("站点 %s 无法识别站点类型" % site_name)
                    return None
                return site_schema(site_name, url, site_cookie, html_text, session=session, ua=ua, proxy=proxy)
            return None

    def refresh_by_domain(self, domain: str) -> schemas.Response:
        """
        刷新一个站点数据，可由API调用
        """
        site_info = self.sites.get_indexer(domain)
        if site_info:
            site_data = self.__refresh_site_data(site_info)
            if site_data:
                return schemas.Response(
                    success=True,
                    message=f"站点 {domain} 刷新成功",
                    data=site_data.to_dict()
                )
            return schemas.Response(
                success=False,
                message=f"站点 {domain} 刷新数据失败，未获取到数据"
            )
        return schemas.Response(
            success=False,
            message=f"站点 {domain} 不存在"
        )

    def __refresh_site_data(self, site_info: CommentedMap) -> Optional[ISiteUserInfo]:
        """
        更新单个site 数据信息
        :param site_info:
        :return:
        """
        site_name = site_info.get('name')
        site_url = site_info.get('url')
        if not site_url:
            return None
        unread_msg_notify = True
        try:
            site_user_info: ISiteUserInfo = self.build(site_info=site_info)
            if site_user_info:
                logger.debug(f"站点 {site_name} 开始以 {site_user_info.site_schema()} 模型解析")
                # 开始解析
                site_user_info.parse()
                logger.debug(f"站点 {site_name} 解析完成")

                # 获取不到数据时，仅返回错误信息，不做历史数据更新
                if site_user_info.err_msg:
                    self._sites_data.update({site_name: {"err_msg": site_user_info.err_msg}})
                    return None

                # 发送通知，存在未读消息
                self.__notify_unread_msg(site_name, site_user_info, unread_msg_notify)

                # 分享率接近1时，发送消息提醒
                if site_user_info.ratio and float(site_user_info.ratio) < 1:
                    self.post_message(mtype=NotificationType.SiteMessage,
                                      title=f"【站点分享率低预警】",
                                      text=f"站点 {site_user_info.site_name} 分享率 {site_user_info.ratio}，请注意！")

                self._sites_data.update(
                    {
                        site_name: {
                            "upload": site_user_info.upload,
                            "username": site_user_info.username,
                            "user_level": site_user_info.user_level,
                            "join_at": site_user_info.join_at,
                            "download": site_user_info.download,
                            "ratio": site_user_info.ratio,
                            "seeding": site_user_info.seeding,
                            "seeding_size": site_user_info.seeding_size,
                            "leeching": site_user_info.leeching,
                            "bonus": site_user_info.bonus,
                            "url": site_url,
                            "err_msg": site_user_info.err_msg,
                            "message_unread": site_user_info.message_unread
                        }
                    })
                return site_user_info

        except Exception as e:
            logger.error(f"站点 {site_name} 获取流量数据失败：{str(e)}")
        return None

    def __notify_unread_msg(self, site_name: str, site_user_info: ISiteUserInfo, unread_msg_notify: bool):
        if site_user_info.message_unread <= 0:
            return
        if self._sites_data.get(site_name, {}).get('message_unread') == site_user_info.message_unread:
            return
        if not unread_msg_notify:
            return

        # 解析出内容，则发送内容
        if len(site_user_info.message_unread_contents) > 0:
            for head, date, content in site_user_info.message_unread_contents:
                msg_title = f"【站点 {site_user_info.site_name} 消息】"
                msg_text = f"时间：{date}\n标题：{head}\n内容：\n{content}"
                self.post_message(mtype=NotificationType.SiteMessage, title=msg_title, text=msg_text)
        else:
            self.post_message(mtype=NotificationType.SiteMessage,
                              title=f"站点 {site_user_info.site_name} 收到 "
                                    f"{site_user_info.message_unread} 条新消息，请登陆查看")

    @eventmanager.register(EventType.SiteStatistic)
    def refresh(self, event: Event):
        """
        刷新站点数据
        """
        if event:
            logger.info("收到命令，开始刷新站点数据 ...")
            self.post_message(channel=event.event_data.get("channel"),
                              title="开始刷新站点数据 ...",
                              userid=event.event_data.get("user"))
        self.refresh_all_site_data()
        if event:
            self.post_message(channel=event.event_data.get("channel"),
                              title="站点数据刷新完成！", userid=event.event_data.get("user"))

    def refresh_all_site_data(self):
        """
        多线程刷新站点下载上传量，默认间隔6小时
        """
        if not self.sites.get_indexers():
            return

        logger.info("开始刷新站点数据 ...")

        with lock:

            all_sites = [site for site in self.sites.get_indexers() if not site.get("public")] + self.__custom_sites()
            # 没有指定站点，默认使用全部站点
            if not self._statistic_sites:
                refresh_sites = all_sites
            else:
                refresh_sites = [site for site in all_sites if
                                 site.get("id") in self._statistic_sites]
            if not refresh_sites:
                return

            # 并发刷新
            with ThreadPool(min(len(refresh_sites), int(self._queue_cnt or 5))) as p:
                p.map(self.__refresh_site_data, refresh_sites)

            # 通知刷新完成
            if self._notify:
                yesterday_sites_data = {}
                # 增量数据
                if self._statistic_type == "add":
                    last_update_time = self.get_data("last_update_time")
                    if last_update_time:
                        yesterday_sites_data = self.get_data(last_update_time) or {}

                messages = []
                # 按照上传降序排序
                sites = self._sites_data.keys()
                uploads = [self._sites_data[site].get("upload") or 0 if not yesterday_sites_data.get(site) else
                           (self._sites_data[site].get("upload") or 0) - (
                                   yesterday_sites_data[site].get("upload") or 0) for site in sites]
                downloads = [self._sites_data[site].get("download") or 0 if not yesterday_sites_data.get(site) else
                             (self._sites_data[site].get("download") or 0) - (
                                     yesterday_sites_data[site].get("download") or 0) for site in sites]
                data_list = sorted(list(zip(sites, uploads, downloads)),
                                   key=lambda x: x[1],
                                   reverse=True)
                # 总上传
                incUploads = 0
                # 总下载
                incDownloads = 0
                for data in data_list:
                    site = data[0]
                    upload = int(data[1])
                    download = int(data[2])
                    if upload > 0 or download > 0:
                        incUploads += int(upload)
                        incDownloads += int(download)
                        messages.append(f"【{site}】\n"
                                        f"上传量：{StringUtils.str_filesize(upload)}\n"
                                        f"下载量：{StringUtils.str_filesize(download)}\n"
                                        f"————————————")

                if incDownloads or incUploads:
                    messages.insert(0, f"【汇总】\n"
                                       f"总上传：{StringUtils.str_filesize(incUploads)}\n"
                                       f"总下载：{StringUtils.str_filesize(incDownloads)}\n"
                                       f"————————————")
                    self.post_message(mtype=NotificationType.SiteMessage,
                                      title="站点数据统计", text="\n".join(messages))

            # 获取今天的日期
            key = datetime.now().strftime('%Y-%m-%d')
            # 保存数据
            self.save_data(key, self._sites_data)

            # 更新时间
            self.save_data("last_update_time", key)
            logger.info("站点数据刷新完成")

    def __custom_sites(self) -> List[Any]:
        custom_sites = []
        custom_sites_config = self.get_config("CustomSites")
        if custom_sites_config and custom_sites_config.get("enabled"):
            custom_sites = custom_sites_config.get("sites")
        return custom_sites

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "cron": self._cron,
            "notify": self._notify,
            "queue_cnt": self._queue_cnt,
            "statistic_type": self._statistic_type,
            "statistic_sites": self._statistic_sites,
        })

    @eventmanager.register(EventType.SiteDeleted)
    def site_deleted(self, event):
        """
        删除对应站点选中
        """
        site_id = event.event_data.get("site_id")
        config = self.get_config()
        if config:
            statistic_sites = config.get("statistic_sites")
            if statistic_sites:
                if isinstance(statistic_sites, str):
                    statistic_sites = [statistic_sites]

                # 删除对应站点
                if site_id:
                    statistic_sites = [site for site in statistic_sites if int(site) != int(site_id)]
                else:
                    # 清空
                    statistic_sites = []

                # 若无站点，则停止
                if len(statistic_sites) == 0:
                    self._enabled = False

                self._statistic_sites = statistic_sites
                # 保存配置
                self.__update_config()
