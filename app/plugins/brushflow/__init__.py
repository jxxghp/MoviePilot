from datetime import datetime, timedelta
from typing import Any, List, Dict, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler

from app.core.config import settings
from app.helper.sites import SitesHelper
from app.log import logger
from app.plugins import _PluginBase
from app.utils.string import StringUtils


class BrushFlow(_PluginBase):
    # 插件名称
    plugin_name = "站点刷流"
    # 插件描述
    plugin_desc = "自动托管刷流，将会默认提高对应站点的种子刷新频率。"
    # 插件图标
    plugin_icon = "fileupload.png"
    # 主题色
    plugin_color = "#EC5665"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "jxxghp"
    # 作者主页
    author_url = "https://github.com/jxxghp"
    # 插件配置项ID前缀
    plugin_config_prefix = "brushflow_"
    # 加载顺序
    plugin_order = 21
    # 可使用的用户级别
    auth_level = 3

    # 私有属性
    sites = None
    _cron = 10
    _scheduler = None
    _enabled = False
    _notify = True
    _onlyonce = False
    _brushsites = []
    _downloader = "qbittorrent"
    _disksize = 0
    _freeleech = "free"
    _maxupspeed = 0
    _maxdlspeed = 0
    _maxdlcount = 0
    _include = ""
    _exclude = ""
    _size = 0
    _seeder = 0
    _pubtime = 0
    _seed_time = 0
    _seed_ratio = 0
    _seed_size = 0
    _download_time = 0
    _seed_avgspeed = 0
    _seed_inactivetime = 0
    _up_speed = 0
    _dl_speed = 0
    _save_path = ""

    def init_plugin(self, config: dict = None):
        self.sites = SitesHelper()
        if config:
            self._enabled = config.get("enabled")
            self._notify = config.get("notify")
            self._onlyonce = config.get("onlyonce")
            self._brushsites = config.get("brushsites")
            self._downloader = config.get("downloader")
            self._disksize = config.get("disksize")
            self._freeleech = config.get("freeleech")
            self._maxupspeed = config.get("maxupspeed")
            self._maxdlspeed = config.get("maxdlspeed")
            self._maxdlcount = config.get("maxdlcount")
            self._include = config.get("include")
            self._exclude = config.get("exclude")
            self._size = config.get("size")
            self._seeder = config.get("seeder")
            self._pubtime = config.get("pubtime")
            self._seed_time = config.get("seed_time")
            self._seed_ratio = config.get("seed_ratio")
            self._seed_size = config.get("seed_size")
            self._download_time = config.get("download_time")
            self._seed_avgspeed = config.get("seed_avgspeed")
            self._seed_inactivetime = config.get("seed_inactivetime")
            self._up_speed = config.get("up_speed")
            self._dl_speed = config.get("dl_speed")
            self._save_path = config.get("save_path")

            # 停止现有任务
            self.stop_service()

            # 启动定时任务 & 立即运行一次
            if self.get_state() or self._onlyonce:
                # 检查配置
                if self._disksize and not StringUtils.is_number(self._disksize):
                    logger.error(f"保种体积设置错误：{self._disksize}")
                    self.systemmessage.put(f"保种体积设置错误：{self._disksize}")
                    return
                if self._maxupspeed and not StringUtils.is_number(self._maxupspeed):
                    logger.error(f"总上传带宽设置错误：{self._maxupspeed}")
                    self.systemmessage.put(f"总上传带宽设置错误：{self._maxupspeed}")
                    return
                if self._maxdlspeed and not StringUtils.is_number(self._maxdlspeed):
                    logger.error(f"总下载带宽设置错误：{self._maxdlspeed}")
                    self.systemmessage.put(f"总下载带宽设置错误：{self._maxdlspeed}")
                    return
                if self._maxdlcount and not StringUtils.is_number(self._maxdlcount):
                    logger.error(f"同时下载任务数设置错误：{self._maxdlcount}")
                    self.systemmessage.put(f"同时下载任务数设置错误：{self._maxdlcount}")
                    return
                if self._size and not StringUtils.is_number(self._size):
                    logger.error(f"种子大小设置错误：{self._size}")
                    self.systemmessage.put(f"种子大小设置错误：{self._size}")
                    return
                if self._seeder and not StringUtils.is_number(self._seeder):
                    logger.error(f"做种人数设置错误：{self._seeder}")
                    self.systemmessage.put(f"做种人数设置错误：{self._seeder}")
                    return
                if self._seed_time and not StringUtils.is_number(self._seed_time):
                    logger.error(f"做种时间设置错误：{self._seed_time}")
                    self.systemmessage.put(f"做种时间设置错误：{self._seed_time}")
                    return
                if self._seed_ratio and not StringUtils.is_number(self._seed_ratio):
                    logger.error(f"分享率设置错误：{self._seed_ratio}")
                    self.systemmessage.put(f"分享率设置错误：{self._seed_ratio}")
                    return
                if self._seed_size and not StringUtils.is_number(self._seed_size):
                    logger.error(f"上传量设置错误：{self._seed_size}")
                    self.systemmessage.put(f"上传量设置错误：{self._seed_size}")
                    return
                if self._download_time and not StringUtils.is_number(self._download_time):
                    logger.error(f"下载超时时间设置错误：{self._download_time}")
                    self.systemmessage.put(f"下载超时时间设置错误：{self._download_time}")
                    return
                if self._seed_avgspeed and not StringUtils.is_number(self._seed_avgspeed):
                    logger.error(f"平均上传速度设置错误：{self._seed_avgspeed}")
                    self.systemmessage.put(f"平均上传速度设置错误：{self._seed_avgspeed}")
                    return
                if self._seed_inactivetime and not StringUtils.is_number(self._seed_inactivetime):
                    logger.error(f"未活动时间设置错误：{self._seed_inactivetime}")
                    self.systemmessage.put(f"未活动时间设置错误：{self._seed_inactivetime}")
                    return
                if self._up_speed and not StringUtils.is_number(self._up_speed):
                    logger.error(f"单任务上传限速设置错误：{self._up_speed}")
                    self.systemmessage.put(f"单任务上传限速设置错误：{self._up_speed}")
                    return
                if self._dl_speed and not StringUtils.is_number(self._dl_speed):
                    logger.error(f"单任务下载限速设置错误：{self._dl_speed}")
                    self.systemmessage.put(f"单任务下载限速设置错误：{self._dl_speed}")
                    return

                # 检查必要条件
                if not self._brushsites or not self._downloader:
                    return

                # 启动任务
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                logger.info(f"站点刷流服务启动，周期：{self._cron}分钟")
                try:
                    self._scheduler.add_job(self.brush, 'interval', minutes=self._cron)
                except Exception as e:
                    logger.error(f"站点刷流服务启动失败：{e}")
                    self.systemmessage(f"站点刷流服务启动失败：{e}")
                    return
                if self._onlyonce:
                    logger.info(f"站点刷流服务启动，立即运行一次")
                    self._scheduler.add_job(self.brush, 'date',
                                            run_date=datetime.now(
                                                tz=pytz.timezone(settings.TZ)
                                            ) + timedelta(seconds=3))
                    # 关闭一次性开关
                    self._onlyonce = False
                    self.update_config({
                        "onlyonce": False,
                        "enabled": self._enabled,
                        "notify": self._notify,
                        "brushsites": self._brushsites,
                        "downloader": self._downloader,
                        "disksize": self._disksize,
                        "freeleech": self._freeleech,
                        "maxupspeed": self._maxupspeed,
                        "maxdlspeed": self._maxdlspeed,
                        "maxdlcount": self._maxdlcount,
                        "include": self._include,
                        "exclude": self._exclude,
                        "size": self._size,
                        "seeder": self._seeder,
                        "pubtime": self._pubtime,
                        "seed_time": self._seed_time,
                        "seed_ratio": self._seed_ratio,
                        "seed_size": self._seed_size,
                        "download_time": self._download_time,
                        "seed_avgspeed": self._seed_avgspeed,
                        "seed_inactivetime": self._seed_inactivetime,
                        "up_speed": self._up_speed,
                        "dl_speed": self._dl_speed,
                        "save_path": self._save_path
                    })
                if self._scheduler.get_jobs():
                    # 启动服务
                    self._scheduler.print_jobs()
                    self._scheduler.start()

    def get_state(self) -> bool:
        return True if self._enabled and self._brushsites and self._downloader else False

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        # 站点的可选项
        site_options = [{"title": site.get("name"), "value": site.get("id")}
                        for site in self.sites.get_indexers()]
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
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'chips': True,
                                            'multiple': True,
                                            'model': 'brushsites',
                                            'label': '刷流站点',
                                            'items': site_options
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
                                    "cols": 12,
                                    "md": 4
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'downloader',
                                            'label': '下载器',
                                            'items': [
                                                {'title': 'Qbittorrent', 'value': 'qbittorrent'},
                                                {'title': 'Transmission', 'value': 'transmission'}
                                            ]
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    "cols": 12,
                                    "md": 4
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'disksize',
                                            'label': '保种体积（GB）',
                                            'placeholder': '达到后停止新增任务'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    "cols": 12,
                                    "md": 4
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'freeleech',
                                            'label': '促销',
                                            'items': [
                                                {'title': '全部（包括普通）', 'value': ''},
                                                {'title': '免费', 'value': 'free'},
                                                {'title': '2X免费', 'value': '2xfree'},
                                            ]
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    "cols": 12,
                                    "md": 4
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'maxupspeed',
                                            'label': '总上传带宽（KB/s）',
                                            'placeholder': '达到后停止新增任务'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    "cols": 12,
                                    "md": 4
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'maxdlspeed',
                                            'label': '总下载带宽（KB/s）',
                                            'placeholder': '达到后停止新增任务'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    "cols": 12,
                                    "md": 4
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'maxdlcount',
                                            'label': '同时下载任务数',
                                            'placeholder': '达到后停止新增任务'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    "cols": 12,
                                    "md": 4
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'include',
                                            'label': '包含规则',
                                            'placeholder': '支持正式表达式'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    "cols": 12,
                                    "md": 4
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'exclude',
                                            'label': '排除规则',
                                            'placeholder': '支持正式表达式'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    "cols": 12,
                                    "md": 4
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'size',
                                            'label': '种子大小（GB）',
                                            'placeholder': '如：5 或 5-10'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    "cols": 12,
                                    "md": 4
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'seeder',
                                            'label': '做种人数',
                                            'placeholder': '如：5 或 5-10'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    "cols": 12,
                                    "md": 4
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'pubtime',
                                            'label': '发布时间（分钟）',
                                            'placeholder': '如：5 或 5-10'
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
                                    "cols": 12,
                                    "md": 4
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'seed_time',
                                            'label': '做种时间（小时）',
                                            'placeholder': '达到后删除任务'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    "cols": 12,
                                    "md": 4
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'seed_ratio',
                                            'label': '分享率',
                                            'placeholder': '达到后删除任务'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    "cols": 12,
                                    "md": 4
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'seed_size',
                                            'label': '上传量（GB）',
                                            'placeholder': '达到后删除任务'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    "cols": 12,
                                    "md": 4
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'download_time',
                                            'label': '下载超时时间（小时）',
                                            'placeholder': '达到后删除任务'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    "cols": 12,
                                    "md": 4
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'seed_avgspeed',
                                            'label': '平均上传速度（KB/s）',
                                            'placeholder': '低于时删除任务'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    "cols": 12,
                                    "md": 4
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'seed_inactivetime',
                                            'label': '未活动时间（分钟） ',
                                            'placeholder': '超过时删除任务'
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
                                    "cols": 12,
                                    "md": 4
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'up_speed',
                                            'label': '单任务上传限速（KB/s）',
                                            'placeholder': '种子上传限速'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    "cols": 12,
                                    "md": 4
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'dl_speed',
                                            'label': '单任务下载限速（KB/s）',
                                            'placeholder': '种子下载限速'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    "cols": 12,
                                    "md": 4
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'save_path',
                                            'label': '保存目录',
                                            'placeholder': '留空自动'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                ]
            }
        ], {
            "enabled": False,
            "notify": True,
            "onlyonce": False,
            "freeleech": "free"
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        退出插件
        """
        pass

    def brush(self):
        """
        执行刷流动作
        """
        pass
