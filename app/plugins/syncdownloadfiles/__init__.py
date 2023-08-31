import os
import time
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.db.downloadhistory_oper import DownloadHistoryOper
from app.db.transferhistory_oper import TransferHistoryOper
from app.modules.qbittorrent import Qbittorrent
from app.modules.transmission import Transmission
from app.plugins import _PluginBase
from typing import Any, List, Dict, Tuple, Optional
from app.log import logger


class SyncDownloadFiles(_PluginBase):
    # 插件名称
    plugin_name = "下载器文件同步"
    # 插件描述
    plugin_desc = "同步下载器的文件信息到数据库，删除文件时联动删除下载任务。"
    # 插件图标
    plugin_icon = "sync_file.png"
    # 主题色
    plugin_color = "#4686E3"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "syncdownloadfiles_"
    # 加载顺序
    plugin_order = 20
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    _enabled = False
    # 任务执行间隔
    _time = None
    qb = None
    tr = None
    _onlyonce = False
    _history = False
    _downloaders = []
    _dirs = None
    downloadhis = None
    transferhis = None

    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        # 停止现有任务
        self.stop_service()

        if config:
            self._enabled = config.get('enabled')
            self._time = config.get('time') or 6
            self._history = config.get('history')
            self._onlyonce = config.get("onlyonce")
            self._downloaders = config.get('downloaders') or []
            self._dirs = config.get("dirs") or ""

        if self._onlyonce:
            # 执行一次
            self.qb = Qbittorrent()
            self.tr = Transmission()
            self.downloadhis = DownloadHistoryOper(self.db)
            self.transferhis = TransferHistoryOper(self.db)

            # 关闭onlyonce
            self._onlyonce = False
            self.__update_config()

            self.sync()

        if self._enabled:
            # 定时服务
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            if self._time:
                try:
                    self._scheduler.add_job(func=self.sync,
                                            trigger="interval",
                                            hours=float(self._time.strip()),
                                            name="自动同步下载器文件记录")
                    logger.info(f"自动同步下载器文件记录服务启动，时间间隔 {self._time} 小时")
                except Exception as err:
                    logger.error(f"定时任务配置错误：{err}")

                # 启动任务
                if self._scheduler.get_jobs():
                    self._scheduler.print_jobs()
                    self._scheduler.start()
            else:
                self._enabled = False
                self.__update_config()

    def sync(self):
        """
        同步所选下载器种子记录
        """
        start_time = datetime.now()
        logger.info("开始同步下载器任务文件记录")

        if not self._downloaders:
            logger.error("未选择同步下载器，停止运行")
            return

        # 遍历下载器同步记录
        for downloader in self._downloaders:
            # 获取最后同步时间
            last_sync_time = self.get_data(f"last_sync_time_{downloader}")

            logger.info(f"开始扫描下载器 {downloader} ...")
            downloader_obj = self.__get_downloader(downloader)
            # 获取下载器中已完成的种子
            torrents = downloader_obj.get_completed_torrents()
            if torrents:
                logger.info(f"下载器 {downloader} 已完成种子数：{len(torrents)}")
            else:
                logger.info(f"下载器 {downloader} 没有已完成种子")
                continue

            # 把种子按照名称和种子大小分组，获取添加时间最早的一个，认定为是源种子，其余为辅种
            torrents = self.__get_origin_torrents(torrents, downloader)
            logger.info(f"下载器 {downloader} 去除辅种，获取到源种子数：{len(torrents)}")

            for torrent in torrents:
                # 返回false，标识后续种子已被同步
                sync_flag = self.__compare_time(torrent, downloader, last_sync_time)

                if not sync_flag:
                    logger.info(f"最后同步时间{last_sync_time}, 之前种子已被同步，结束当前下载器 {downloader} 任务")
                    break

                # 获取种子hash
                hash_str = self.__get_hash(torrent, downloader)

                # 判断是否是mp下载，判断download_hash是否在downloadhistory表中，是则不处理
                downloadhis = self.downloadhis.get_by_hash(hash_str)
                if downloadhis:
                    downlod_files = self.downloadhis.get_files_by_hash(hash_str)
                    if downlod_files:
                        logger.info(f"种子 {hash_str} 通过MoviePilot下载，跳过处理")
                        continue

                # 获取种子download_dir
                download_dir = self.__get_download_dir(torrent, downloader)
                # 获取种子name
                torrent_name = self.__get_torrent_name(torrent, downloader)
                # 获取种子文件
                torrent_files = self.__get_torrent_files(torrent, downloader, downloader_obj)
                logger.info(f"开始同步种子 {hash_str}, 文件数 {len(torrent_files)}")

                # 处理路径映射
                if self._dirs:
                    paths = self._dirs.split("\n")
                    for path in paths:
                        sub_paths = path.split(":")
                        download_dir = download_dir.replace(sub_paths[0], sub_paths[1]).replace('\\', '/')

                download_files = []
                for file in torrent_files:
                    file_name = self.__get_file_name(file, downloader)
                    full_path = Path(download_dir).joinpath(torrent_name, file_name)
                    if self._history:
                        transferhis = self.transferhis.get_by_src(str(full_path))
                        if transferhis and not transferhis.download_hash:
                            logger.info(f"开始补充转移记录 {transferhis.id} download_hash {hash_str}")
                            self.transferhis.update_download_hash(historyid=transferhis.id,
                                                                  download_hash=hash_str)

                    # 种子文件记录
                    download_files.append(
                        {
                            "download_hash": hash_str,
                            "downloader": downloader,
                            "fullpath": str(full_path),
                            "savepath": str(Path(download_dir).joinpath(torrent_name)),
                            "filepath": file_name,
                            "torrentname": torrent_name,
                        }
                    )

                if download_files:
                    # 登记下载文件
                    self.downloadhis.add_files(download_files)
                logger.info(f"种子 {hash_str} 同步完成")

            logger.info(f"下载器种子文件同步完成！")
            self.save_data(f"last_sync_time_{downloader}",
                           time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time())))

            # 计算耗时
            end_time = datetime.now()

            logger.info(f"下载器任务文件记录已同步完成。总耗时 {(end_time - start_time).seconds} 秒")

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "time": self._time,
            "history": self._history,
            "onlyonce": self._onlyonce,
            "downloaders": self._downloaders,
            "dirs": self._dirs
        })

    @staticmethod
    def __get_origin_torrents(torrents: Any, dl_tpe: str):
        # 把种子按照名称和种子大小分组，获取添加时间最早的一个，认定为是源种子，其余为辅种
        grouped_data = {}

        # 排序种子，根据种子添加时间倒序
        if dl_tpe == "qbittorrent":
            torrents = sorted(torrents, key=lambda x: x.get("added_on"), reverse=True)
            # 遍历原始数组，按照size和name进行分组
            for torrent in torrents:
                size = torrent.get('size')
                name = torrent.get('name')
                key = (size, name)  # 使用元组作为字典的键

                # 如果分组键不存在，则将当前元素作为最小元素添加到字典中
                if key not in grouped_data:
                    grouped_data[key] = torrent
                else:
                    # 如果分组键已存在，则比较当前元素的time是否更小，如果更小则更新字典中的元素
                    if torrent.get('added_on') < grouped_data[key].get('added_on'):
                        grouped_data[key] = torrent
        else:
            torrents = sorted(torrents, key=lambda x: x.added_date, reverse=True)
            # 遍历原始数组，按照size和name进行分组
            for torrent in torrents:
                size = torrent.total_size
                name = torrent.name
                key = (size, name)  # 使用元组作为字典的键

                # 如果分组键不存在，则将当前元素作为最小元素添加到字典中
                if key not in grouped_data:
                    grouped_data[key] = torrent
                else:
                    # 如果分组键已存在，则比较当前元素的time是否更小，如果更小则更新字典中的元素
                    if torrent.added_date < grouped_data[key].added_date:
                        grouped_data[key] = torrent

        # 新的数组
        return list(grouped_data.values())

    @staticmethod
    def __compare_time(torrent: Any, dl_tpe: str, last_sync_time: str = None):
        if last_sync_time:
            # 获取种子时间
            if dl_tpe == "qbittorrent":
                torrent_date = time.gmtime(torrent.get("added_on"))  # 将时间戳转换为时间元组
                torrent_date = time.strftime("%Y-%m-%d %H:%M:%S", torrent_date)  # 格式化时间
            else:
                torrent_date = torrent.added_date

            # 之后的种子已经同步了
            if last_sync_time > str(torrent_date):
                return False

        return True

    @staticmethod
    def __get_file_name(file: Any, dl_type: str):
        """
        获取文件名
        """
        try:
            return os.path.basename(file.get("name")) if dl_type == "qbittorrent" else os.path.basename(file.name)
        except Exception as e:
            print(str(e))
            return ""

    @staticmethod
    def __get_torrent_files(torrent: Any, dl_type: str, downloader_obj):
        """
        获取种子文件
        """
        try:
            return torrent.files if dl_type == "qbittorrent" else downloader_obj.get_files(tid=torrent.id)
        except Exception as e:
            print(str(e))
            return ""

    @staticmethod
    def __get_torrent_name(torrent: Any, dl_type: str):
        """
        获取种子name
        """
        try:
            return torrent.get("name") if dl_type == "qbittorrent" else torrent.name
        except Exception as e:
            print(str(e))
            return ""

    @staticmethod
    def __get_download_dir(torrent: Any, dl_type: str):
        """
        获取种子download_dir
        """
        try:
            return torrent.get("save_path") if dl_type == "qbittorrent" else torrent.download_dir
        except Exception as e:
            print(str(e))
            return ""

    @staticmethod
    def __get_hash(torrent: Any, dl_type: str):
        """
        获取种子hash
        """
        try:
            return torrent.get("hash") if dl_type == "qbittorrent" else torrent.hashString
        except Exception as e:
            print(str(e))
            return ""

    def __get_downloader(self, dtype: str):
        """
        根据类型返回下载器实例
        """
        if dtype == "qbittorrent":
            return self.qb
        elif dtype == "transmission":
            return self.tr
        else:
            return None

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
                                            'label': '开启插件',
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
                                            'model': 'history',
                                            'label': '补充整理历史记录',
                                        }
                                    }
                                ]
                            },
                        ]
                    },
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
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'time',
                                            'label': '同步时间间隔'
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
                                        'component': 'VSelect',
                                        'props': {
                                            'chips': True,
                                            'multiple': True,
                                            'model': 'downloaders',
                                            'label': '同步下载器',
                                            'items': [
                                                {'title': 'Qbittorrent', 'value': 'qbittorrent'},
                                                {'title': 'Transmission', 'value': 'transmission'}
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
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'dirs',
                                            'label': '目录映射',
                                            'rows': 5,
                                            'placeholder': '每一行一个目录，下载器保存目录:MoviePilot映射目录'
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
                                            'text': '适用于非MoviePilot下载的任务；下载器种子数据较多时，同步时间将会较长，请耐心等候，可查看实时日志了解同步进度；时间间隔建议最少每6小时执行一次，防止上次任务没处理完。'
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
            "history": False,
            "time": 6,
            "dirs": "",
            "downloaders": []
        }

    def get_page(self) -> List[dict]:
        pass

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
