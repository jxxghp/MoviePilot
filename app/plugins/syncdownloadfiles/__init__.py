import os
import time
from pathlib import Path

from app.db.downloadhistory_oper import DownloadHistoryOper
from app.db.transferhistory_oper import TransferHistoryOper
from app.modules.qbittorrent import Qbittorrent
from app.modules.transmission import Transmission
from app.plugins import _PluginBase
from typing import Any, List, Dict, Tuple
from app.log import logger


class SyncDownloadFiles(_PluginBase):
    # 插件名称
    plugin_name = "SyncDownloadFiles"
    # 插件描述
    plugin_desc = "同步下载器文件记录。"
    # 插件图标
    plugin_icon = "sync_file.png"
    # 主题色
    plugin_color = "bg-blue"
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
    qb = None
    tr = None
    _onlyonce = False
    _history = False
    _downloaders = []
    _dirs = None
    downloadhis = None
    transferhis = None

    def init_plugin(self, config: dict = None):
        if config:
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
            self.update_config({
                "history": self._history,
                "onlyonce": self._onlyonce,
                "downloaders": self._downloaders,
                "dirs": self._dirs
            })

            self.sync()

    def sync(self):
        """
        同步所选下载器种子记录
        """
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

            # 排序种子，根据种子添加时间倒序
            if downloader == "qbittorrent":
                torrents = sorted(torrents, key=lambda x: x.get("added_on"), reverse=True)
            else:
                torrents = sorted(torrents, key=lambda x: x.added_date, reverse=True)

            if torrents:
                logger.info(f"下载器 {downloader} 已完成种子数：{len(torrents)}")
            else:
                logger.info(f"下载器 {downloader} 没有已完成种子")
                continue

            for torrent in torrents:
                # 返回false，标识后续种子已被同步
                sync_flag = self.__compare_time(torrent, downloader, last_sync_time)

                if not sync_flag:
                    logger.info(f"最后同步时间{last_sync_time}, 之前种子已被同步，结束当前下载器 {downloader} 任务")
                    break

                # 获取种子hash
                hash_str = self.__get_hash(torrent, downloader)
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
        return False

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
                                            'model': 'onlyonce',
                                            'label': '运行一次',
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
                                            'model': 'history',
                                            'label': '补充转移记录',
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
                                    'cols': 12
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
                                            'placeholder': '每一行一个目录，下载器地址:mp地址'
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
                                            'text': '如果所选下载器种子很多的话，时间会有点久，请耐心等候，可查看日志。'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "onlyonce": False,
            "history": False,
            "dirs": "",
            "downloaders": []
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        退出插件
        """
        pass
