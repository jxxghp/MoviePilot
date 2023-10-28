import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.chain.transfer import TransferChain
from app.core.config import settings
from app.core.event import eventmanager
from app.db.downloadhistory_oper import DownloadHistoryOper
from app.db.transferhistory_oper import TransferHistoryOper
from app.plugins import _PluginBase
from typing import Any, List, Dict, Tuple, Optional
from app.log import logger
from app.schemas import NotificationType, DownloadHistory
from app.schemas.types import EventType


class AutoClean(_PluginBase):
    # 插件名称
    plugin_name = "定时清理媒体库"
    # 插件描述
    plugin_desc = "定时清理用户下载的种子、源文件、媒体库文件。"
    # 插件图标
    plugin_icon = "clean.png"
    # 主题色
    plugin_color = "#3377ed"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "autoclean_"
    # 加载顺序
    plugin_order = 23
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    _enabled = False
    # 任务执行间隔
    _cron = None
    _type = None
    _onlyonce = False
    _notify = False
    _cleantype = None
    _cleandate = None
    _cleanuser = None
    _downloadhis = None
    _transferhis = None

    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        # 停止现有任务
        self.stop_service()

        if config:
            self._enabled = config.get("enabled")
            self._cron = config.get("cron")
            self._onlyonce = config.get("onlyonce")
            self._notify = config.get("notify")
            self._cleantype = config.get("cleantype")
            self._cleandate = config.get("cleandate")
            self._cleanuser = config.get("cleanuser")

            # 加载模块
        if self._enabled:
            self._downloadhis = DownloadHistoryOper()
            self._transferhis = TransferHistoryOper()
            # 定时服务
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)

            if self._cron:
                try:
                    self._scheduler.add_job(func=self.__clean,
                                            trigger=CronTrigger.from_crontab(self._cron),
                                            name="定时清理媒体库")
                except Exception as err:
                    logger.error(f"定时任务配置错误：{str(err)}")

            if self._onlyonce:
                logger.info(f"定时清理媒体库服务启动，立即运行一次")
                self._scheduler.add_job(func=self.__clean, trigger='date',
                                        run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                        name="定时清理媒体库")
                # 关闭一次性开关
                self._onlyonce = False
                self.update_config({
                    "onlyonce": False,
                    "cron": self._cron,
                    "cleantype": self._cleantype,
                    "cleandate": self._cleandate,
                    "enabled": self._enabled,
                    "cleanuser": self._cleanuser,
                    "notify": self._notify,
                })

            # 启动任务
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    def __get_clean_date(self, deltatime: str = None):
        # 清理日期
        current_time = datetime.now()
        if deltatime:
            days_ago = current_time - timedelta(days=int(deltatime))
        else:
            days_ago = current_time - timedelta(days=int(self._cleandate))
        return days_ago.strftime("%Y-%m-%d")

    def __clean(self):
        """
        定时清理媒体库
        """
        if not self._cleandate:
            logger.error("未配置媒体库全局清理时间，停止运行")
            return

        # 查询用户清理日期之前的下载历史，不填默认清理全部用户的下载
        if not self._cleanuser:
            clean_date = self.__get_clean_date()
            downloadhis_list = self._downloadhis.list_by_user_date(date=clean_date)
            logger.info(f'获取到日期 {clean_date} 之前的下载历史 {len(downloadhis_list)} 条')
            self.__clean_history(date=clean_date, clean_type=self._cleantype, downloadhis_list=downloadhis_list)

        # 根据填写的信息判断怎么清理
        else:
            # username:days#cleantype
            clean_type = self._cleantype
            clean_date = self._cleandate

            # 1.3.7版本及之前处理多位用户
            if str(self._cleanuser).count(','):
                for username in str(self._cleanuser).split(","):
                    downloadhis_list = self._downloadhis.list_by_user_date(date=clean_date,
                                                                           username=username)
                    logger.info(
                        f'获取到用户 {username} 日期 {clean_date} 之前的下载历史 {len(downloadhis_list)} 条')
                    self.__clean_history(date=clean_date, clean_type=self._cleantype, downloadhis_list=downloadhis_list)
                return

            for userinfo in str(self._cleanuser).split("\n"):
                if userinfo.count('#'):
                    clean_type = userinfo.split('#')[1]
                    username_and_days = userinfo.split('#')[0]
                else:
                    username_and_days = userinfo
                if username_and_days.count(':'):
                    clean_date = username_and_days.split(':')[1]
                    username = username_and_days.split(':')[0]
                else:
                    username = userinfo

                # 转strftime
                clean_date = self.__get_clean_date(clean_date)
                logger.info(f'{username} 使用 {clean_type} 清理方式，清理 {clean_date} 之前的下载历史')
                downloadhis_list = self._downloadhis.list_by_user_date(date=clean_date,
                                                                       username=username)
                logger.info(
                    f'获取到用户 {username} 日期 {clean_date} 之前的下载历史 {len(downloadhis_list)} 条')
                self.__clean_history(date=clean_date, clean_type=clean_type,
                                     downloadhis_list=downloadhis_list)

    def __clean_history(self, date: str, clean_type: str, downloadhis_list: List[DownloadHistory]):
        """
        清理下载历史、转移记录
        """
        if not downloadhis_list:
            logger.warn(f"未获取到日期 {date} 之前的下载记录，停止运行")
            return

        # 读取历史记录
        pulgin_history = self.get_data('history') or []

        # 创建一个字典来保存分组结果
        downloadhis_grouped_dict: Dict[tuple, List[DownloadHistory]] = defaultdict(list)
        # 遍历DownloadHistory对象列表
        for downloadhis in downloadhis_list:
            # 获取type和tmdbid的值
            dtype = downloadhis.type
            tmdbid = downloadhis.tmdbid

            # 将DownloadHistory对象添加到对应分组的列表中
            downloadhis_grouped_dict[(dtype, tmdbid)].append(downloadhis)

        # 输出分组结果
        for key, downloadhis_list in downloadhis_grouped_dict.items():
            logger.info(f"开始清理 {key}")
            del_transferhis_cnt = 0
            del_media_name = downloadhis_list[0].title
            del_media_user = downloadhis_list[0].username
            del_media_type = downloadhis_list[0].type
            del_media_year = downloadhis_list[0].year
            del_media_season = downloadhis_list[0].seasons
            del_media_episode = downloadhis_list[0].episodes
            del_image = downloadhis_list[0].image
            for downloadhis in downloadhis_list:
                if not downloadhis.download_hash:
                    logger.debug(f'下载历史 {downloadhis.id} {downloadhis.title} 未获取到download_hash，跳过处理')
                    continue
                # 根据hash获取转移记录
                transferhis_list = self._transferhis.list_by_hash(download_hash=downloadhis.download_hash)
                if not transferhis_list:
                    logger.warn(f"下载历史 {downloadhis.download_hash} 未查询到转移记录，跳过处理")
                    continue

                for history in transferhis_list:
                    # 册除媒体库文件
                    if clean_type in ["dest", "all"]:
                        TransferChain().delete_files(Path(history.dest))
                        # 删除记录
                        self._transferhis.delete(history.id)
                    # 删除源文件
                    if clean_type in ["src", "all"]:
                        TransferChain().delete_files(Path(history.src))
                        # 发送事件
                        eventmanager.send_event(
                            EventType.DownloadFileDeleted,
                            {
                                "src": history.src
                            }
                        )

                # 累加删除数量
                del_transferhis_cnt += len(transferhis_list)

            if del_transferhis_cnt:
                # 发送消息
                if self._notify:
                    self.post_message(
                        mtype=NotificationType.MediaServer,
                        title="【定时清理媒体库任务完成】",
                        text=f"清理媒体名称 {del_media_name}\n"
                             f"下载媒体用户 {del_media_user}\n"
                             f"删除历史记录 {del_transferhis_cnt}")

                pulgin_history.append({
                    "type": del_media_type,
                    "title": del_media_name,
                    "year": del_media_year,
                    "season": del_media_season,
                    "episode": del_media_episode,
                    "image": del_image,
                    "del_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time()))
                })

        # 保存历史
        self.save_data("history", pulgin_history)

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
                                            'model': 'onlyonce',
                                            'label': '立即运行一次',
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
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '执行周期',
                                            'placeholder': '0 0 ? ? ?'
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
                                            'model': 'cleantype',
                                            'label': '全局清理方式',
                                            'items': [
                                                {'title': '媒体库文件', 'value': 'dest'},
                                                {'title': '源文件', 'value': 'src'},
                                                {'title': '所有文件', 'value': 'all'},
                                            ]
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
                                            'model': 'cleandate',
                                            'label': '全局清理日期',
                                            'placeholder': '清理多少天之前的下载记录（天）'
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
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'cleanuser',
                                            'label': '清理配置',
                                            'rows': 6,
                                            'placeholder': '每一行一个配置，支持以下几种配置方式，清理方式支持 src、desc、all 分别对应源文件，媒体库文件，所有文件\n'
                                                           '用户名缺省默认清理所有用户(慎重留空)，清理天数缺省默认使用全局清理天数，清理方式缺省默认使用全局清理方式\n'
                                                           '用户名/插件名（豆瓣想看、豆瓣榜单、RSS订阅）\n'
                                                           '用户名#清理方式\n'
                                                           '用户名:清理天数\n'
                                                           '用户名:清理天数#清理方式',
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
            "notify": False,
            "cleantype": "dest",
            "cron": "",
            "cleanuser": "",
            "cleandate": 30
        }

    def get_page(self) -> List[dict]:
        """
        拼装插件详情页面，需要返回页面配置，同时附带数据
        """
        # 查询同步详情
        historys = self.get_data('history')
        if not historys:
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
        historys = sorted(historys, key=lambda x: x.get('del_time'), reverse=True)
        # 拼装页面
        contents = []
        for history in historys:
            htype = history.get("type")
            title = history.get("title")
            year = history.get("year")
            season = history.get("season")
            episode = history.get("episode")
            image = history.get("image")
            del_time = history.get("del_time")

            if season:
                sub_contents = [
                    {
                        'component': 'VCardText',
                        'props': {
                            'class': 'pa-0 px-2'
                        },
                        'text': f'类型：{htype}'
                    },
                    {
                        'component': 'VCardText',
                        'props': {
                            'class': 'pa-0 px-2'
                        },
                        'text': f'标题：{title}'
                    },
                    {
                        'component': 'VCardText',
                        'props': {
                            'class': 'pa-0 px-2'
                        },
                        'text': f'年份：{year}'
                    },
                    {
                        'component': 'VCardText',
                        'props': {
                            'class': 'pa-0 px-2'
                        },
                        'text': f'季：{season}'
                    },
                    {
                        'component': 'VCardText',
                        'props': {
                            'class': 'pa-0 px-2'
                        },
                        'text': f'集：{episode}'
                    },
                    {
                        'component': 'VCardText',
                        'props': {
                            'class': 'pa-0 px-2'
                        },
                        'text': f'时间：{del_time}'
                    }
                ]
            else:
                sub_contents = [
                    {
                        'component': 'VCardText',
                        'props': {
                            'class': 'pa-0 px-2'
                        },
                        'text': f'类型：{htype}'
                    },
                    {
                        'component': 'VCardText',
                        'props': {
                            'class': 'pa-0 px-2'
                        },
                        'text': f'标题：{title}'
                    },
                    {
                        'component': 'VCardText',
                        'props': {
                            'class': 'pa-0 px-2'
                        },
                        'text': f'年份：{year}'
                    },
                    {
                        'component': 'VCardText',
                        'props': {
                            'class': 'pa-0 px-2'
                        },
                        'text': f'时间：{del_time}'
                    }
                ]

            contents.append(
                {
                    'component': 'VCard',
                    'content': [
                        {
                            'component': 'div',
                            'props': {
                                'class': 'd-flex justify-space-start flex-nowrap flex-row',
                            },
                            'content': [
                                {
                                    'component': 'div',
                                    'content': [
                                        {
                                            'component': 'VImg',
                                            'props': {
                                                'src': image,
                                                'height': 120,
                                                'width': 80,
                                                'aspect-ratio': '2/3',
                                                'class': 'object-cover shadow ring-gray-500',
                                                'cover': True
                                            }
                                        }
                                    ]
                                },
                                {
                                    'component': 'div',
                                    'content': sub_contents
                                }
                            ]
                        }
                    ]
                }
            )

        return [
            {
                'component': 'div',
                'props': {
                    'class': 'grid gap-3 grid-info-card',
                },
                'content': contents
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
