import json
import os
import sqlite3
from datetime import datetime

from app.core.config import settings
from app.db.downloadhistory_oper import DownloadHistoryOper
from app.db.plugindata_oper import PluginDataOper
from app.db.transferhistory_oper import TransferHistoryOper
from app.plugins import _PluginBase
from typing import Any, List, Dict, Tuple
from app.log import logger


class NAStoolSync(_PluginBase):
    # 插件名称
    plugin_name = "历史记录同步"
    # 插件描述
    plugin_desc = "同步NAStool历史记录、下载记录、插件记录到MoviePilot。"
    # 插件图标
    plugin_icon = "sync.png"
    # 主题色
    plugin_color = "#53BA47"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "nastoolsync_"
    # 加载顺序
    plugin_order = 15
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _transferhistory = None
    _plugindata = None
    _downloadhistory = None
    _clear = None
    _nt_db_path = None
    _path = None
    _site = None
    _downloader = None
    _transfer = False

    def init_plugin(self, config: dict = None):
        self._transferhistory = TransferHistoryOper()
        self._plugindata = PluginDataOper()
        self._downloadhistory = DownloadHistoryOper()

        if config:
            self._clear = config.get("clear")
            self._nt_db_path = config.get("nt_db_path")
            self._path = config.get("path")
            self._site = config.get("site")
            self._downloader = config.get("downloader")
            self._transfer = config.get("transfer")

            if self._nt_db_path and self._transfer:
                # 读取sqlite数据
                try:
                    gradedb = sqlite3.connect(self._nt_db_path)
                except Exception as e:
                    self.update_config(
                        {
                            "transfer": False,
                            "clear": False,
                            "nt_db_path": None,
                            "path": self._path,
                            "downloader": self._downloader,
                            "site": self._site,
                        }
                    )
                    logger.error(f"无法打开数据库文件 {self._nt_db_path}，请检查路径是否正确：{str(e)}")
                    return

                # 创建游标cursor来执行executeＳＱＬ语句
                cursor = gradedb.cursor()

                download_history = self.get_nt_download_history(cursor)
                plugin_history = self.get_nt_plugin_history(cursor)
                transfer_history = self.get_nt_transfer_history(cursor)

                # 关闭游标
                cursor.close()

                # 导入下载记录
                if download_history:
                    self.sync_download_history(download_history)

                # 导入插件记录
                if plugin_history:
                    self.sync_plugin_history(plugin_history)

                # 导入历史记录
                if transfer_history:
                    self.sync_transfer_history(transfer_history)

                self.update_config(
                    {
                        "transfer": False,
                        "clear": False,
                        "nt_db_path": self._nt_db_path,
                        "path": self._path,
                        "downloader": self._downloader,
                        "site": self._site,
                    }
                )

    def sync_plugin_history(self, plugin_history):
        """
        导入插件记录

        NAStool
        {
            "id": "TorrentTransfer",
            "key: "1-4bdc22bc1e062803c8686beb2796369c59ee141f",
            "value": "{"to_download": 2, "to_download_id": "4bdc22bc1e062803c8686beb2796369c59ee141f", "delete_source": true}"
        },
        {
            "id": "IYUUAutoSeed",
            "key: "f161efaf008d2e56e7939272e8d95eca58fa71dd",
            "value": "[{"downloader": "2", "torrents": ["bd64a8edc5afe6b4beb8813bdbf6faedfb1d4cc4"]}]"
        }
        """
        # 开始计时
        start_time = datetime.now()
        logger.info("开始同步NAStool插件历史记录到MoviePilot")
        # 清空MoviePilot插件记录
        if self._clear:
            logger.info("MoviePilot插件记录已清空")
            self._plugindata.truncate()

        cnt = 0
        for history in plugin_history:
            plugin_id = history[1]
            plugin_key = history[2]
            plugin_value = history[3]

            # 替换转种记录
            if str(plugin_id) == "TorrentTransfer":
                keys = str(plugin_key).split("-")

                # 1-2cd5d6fe32dca4e39a3e9f10961bfbdb00437e91
                if len(keys) == 2 and keys[0].isdigit():
                    mp_downloader = self.__get_target_downloader(int(keys[0]))
                    # 替换key
                    plugin_key = mp_downloader + "-" + keys[1]

                    # 替换value
                    """
                    {
                        "to_download":2,
                        "to_download_id":"2cd5d6fe32dca4e39a3e9f10961bfbdb00437e91",
                        "delete_source":true
                    }
                    """
                    if isinstance(plugin_value, str):
                        plugin_value: dict = json.loads(plugin_value)
                    if isinstance(plugin_value, dict):
                        if str(plugin_value.get("to_download")).isdigit():
                            to_downloader = self.__get_target_downloader(int(plugin_value.get("to_download")))
                            plugin_value["to_download"] = to_downloader

            # 替换辅种记录
            elif str(plugin_id) == "IYUUAutoSeed":
                """
                [
                    {
                        "downloader":"2",
                        "torrents":[
                            "a18aa62abab42613edba15e7dbad0d729d8500da",
                            "e494f372316bbfd8572da80138a6ef4c491d5991",
                            "cc2bbc1e654d8fc0f83297f6cd36a38805aa2864",
                            "68aec0db3aa7fe28a887e5e41a0d0d5bc284910f",
                            "f02962474287e11441e34e40b8326ddf28d034f6"
                        ]
                    },
                    {
                        "downloader":"2",
                        "torrents":[
                            "4f042003ce90519e1aadd02b76f51c0c0711adb3"
                        ]
                    }
                ]
                """
                if isinstance(plugin_value, str):
                    plugin_value: list = json.loads(plugin_value)
                if not isinstance(plugin_value, list):
                    plugin_value = [plugin_value]
                for value in plugin_value:
                    if str(value.get("downloader")).isdigit():
                        downloader = self.__get_target_downloader(int(value.get("downloader")))
                        value["downloader"] = downloader

            self._plugindata.save(plugin_id=plugin_id,
                                  key=plugin_key,
                                  value=plugin_value)
            cnt += 1
            if cnt % 100 == 0:
                logger.info(f"插件记录同步进度 {cnt} / {len(plugin_history)}")

        # 计算耗时
        end_time = datetime.now()

        logger.info(f"插件记录已同步完成。总耗时 {(end_time - start_time).seconds} 秒")

    def __get_target_downloader(self, download_id: int):
        """
        获取NAStool下载器id对应的Moviepilot下载器
        """
        # 处理下载器映射
        if self._downloader:
            downloaders = self._downloader.split("\n")
            for downloader in downloaders:
                if not downloader:
                    continue
                sub_downloaders = downloader.split(":")
                if not str(sub_downloaders[0]).isdigit():
                    logger.error(f"下载器映射配置错误：NAStool下载器id 应为数字！")
                    continue
                if int(sub_downloaders[0]) == download_id:
                    return str(sub_downloaders[1])
        return download_id

    def sync_download_history(self, download_history):
        """
        导入下载记录
        """
        # 开始计时
        start_time = datetime.now()
        logger.info("开始同步NAStool下载历史记录到MoviePilot")
        # 清空MoviePilot下载记录
        if self._clear:
            logger.info("MoviePilot下载记录已清空")
            self._downloadhistory.truncate()

        cnt = 0
        for history in download_history:
            mpath = history[0]
            mtype = history[1]
            mtitle = history[2]
            myear = history[3]
            mtmdbid = history[4]
            mseasons = history[5]
            mepisodes = history[6]
            mimages = history[7]
            mdownload_hash = history[8]
            mtorrent = history[9]
            mdesc = history[10]
            msite = history[11]
            mdate = history[12]

            # 处理站点映射
            if self._site:
                sites = self._site.split("\n")
                for site in sites:
                    sub_sites = site.split(":")
                    if str(msite) == str(sub_sites[0]):
                        msite = str(sub_sites[1])

            self._downloadhistory.add(
                path=os.path.basename(mpath),
                type=mtype,
                title=mtitle,
                year=myear,
                tmdbid=mtmdbid,
                seasons=mseasons,
                episodes=mepisodes,
                image=mimages,
                download_hash=mdownload_hash,
                torrent_name=mtorrent,
                torrent_description=mdesc,
                torrent_site=msite,
                userid=settings.SUPERUSER,
                date=mdate
            )
            cnt += 1
            if cnt % 100 == 0:
                logger.info(f"下载记录同步进度 {cnt} / {len(download_history)}")

        # 计算耗时
        end_time = datetime.now()

        logger.info(f"下载记录已同步完成。总耗时 {(end_time - start_time).seconds} 秒")

    def sync_transfer_history(self, transfer_history):
        """
        导入nt转移记录
        """
        # 开始计时
        start_time = datetime.now()
        logger.info("开始同步NAStool转移历史记录到MoviePilot")

        # 清空MoviePilot转移记录
        if self._clear:
            logger.info("MoviePilot转移记录已清空")
            self._transferhistory.truncate()

        # 处理数据，存入mp数据库
        cnt = 0
        for history in transfer_history:
            msrc_path = history[0]
            msrc_filename = history[1]
            mdest_path = history[2]
            mdest_filename = history[3]
            mmode = history[4]
            mtype = history[5]
            mcategory = history[6]
            mtitle = history[7]
            myear = history[8]
            mtmdbid = history[9]
            mseasons = history[10]
            mepisodes = history[11]
            mimage = history[12]
            mdate = history[13]

            if not msrc_path or not mdest_path:
                continue

            msrc = msrc_path + "/" + msrc_filename
            mdest = mdest_path + "/" + mdest_filename

            # 处理路径映射
            if self._path:
                paths = self._path.split("\n")
                for path in paths:
                    sub_paths = path.split(":")
                    msrc = msrc.replace(sub_paths[0], sub_paths[1]).replace('\\', '/')
                    mdest = mdest.replace(sub_paths[0], sub_paths[1]).replace('\\', '/')

            # 存库
            self._transferhistory.add(
                src=msrc,
                dest=mdest,
                mode=mmode,
                type=mtype,
                category=mcategory,
                title=mtitle,
                year=myear,
                tmdbid=mtmdbid,
                seasons=mseasons,
                episodes=mepisodes,
                image=mimage,
                date=mdate
            )
            logger.debug(f"{mtitle} {myear} {mtmdbid} {mseasons} {mepisodes} 已同步")

            cnt += 1
            if cnt % 100 == 0:
                logger.info(f"转移记录同步进度 {cnt} / {len(transfer_history)}")

        # 计算耗时
        end_time = datetime.now()

        logger.info(f"转移记录已同步完成。总耗时 {(end_time - start_time).seconds} 秒")

    @staticmethod
    def get_nt_plugin_history(cursor):
        """
        获取插件历史记录
        """
        sql = 'select * from PLUGIN_HISTORY;'
        cursor.execute(sql)
        plugin_history = cursor.fetchall()

        if not plugin_history:
            logger.error("未获取到NAStool数据库文件中的插件历史，请检查数据库路径是正确")
            return

        logger.info(f"获取到NAStool插件记录 {len(plugin_history)} 条")
        return plugin_history

    @staticmethod
    def get_nt_download_history(cursor):
        """
        获取下载历史记录
        """
        sql = '''
        SELECT
            SAVE_PATH,
            TYPE,
            TITLE,
            YEAR,
            TMDBID,
        CASE
                SE 
            WHEN NULL THEN
                NULL ELSE substr( SE, 1, instr ( SE, ' ' ) - 1 ) 
            END AS seasons,
        CASE
                SE 
            WHEN NULL THEN
                NULL ELSE substr( SE, instr ( SE, ' ' ) + 1 ) 
            END AS episodes,
            POSTER,
            DOWNLOAD_ID,
            TORRENT,
            DESC,
            SITE,
            DATE
        FROM
            DOWNLOAD_HISTORY 
        WHERE
            SAVE_PATH IS NOT NULL;
            '''
        cursor.execute(sql)
        download_history = cursor.fetchall()

        if not download_history:
            logger.error("未获取到NAStool数据库文件中的下载历史，请检查数据库路径是正确")
            return

        logger.info(f"获取到NAStool下载记录 {len(download_history)} 条")
        return download_history

    @staticmethod
    def get_nt_transfer_history(cursor):
        """
        获取nt转移记录
        """
        sql = '''
        SELECT
            t.SOURCE_PATH AS src_path,
            t.SOURCE_FILENAME AS src_filename,
            t.DEST_PATH AS dest_path,
            t.DEST_FILENAME AS dest_filename,
        CASE
                t.MODE 
                WHEN '硬链接' THEN
                'link' 
                WHEN '移动' THEN
                'move' 
                WHEN '复制' THEN
                'copy' 
            END AS mode,
        CASE
                t.TYPE 
                WHEN '动漫' THEN
                '电视剧' ELSE t.TYPE 
            END AS type,
            t.CATEGORY AS category,
            t.TITLE AS title,
            t.YEAR AS year,
            t.TMDBID AS tmdbid,
        CASE
                t.SEASON_EPISODE 
            WHEN NULL THEN
                NULL ELSE substr( t.SEASON_EPISODE, 1, instr ( t.SEASON_EPISODE, ' ' ) - 1 ) 
            END AS seasons,
        CASE
                t.SEASON_EPISODE 
            WHEN NULL THEN
                NULL ELSE substr( t.SEASON_EPISODE, instr ( t.SEASON_EPISODE, ' ' ) + 1 ) 
            END AS episodes,
            d.POSTER AS image,
            t.DATE AS date 
        FROM
            TRANSFER_HISTORY t
            LEFT JOIN ( SELECT * FROM DOWNLOAD_HISTORY GROUP BY TMDBID ) d ON t.TMDBID = d.TMDBID
            AND t.TYPE = d.TYPE;
            '''
        cursor.execute(sql)
        nt_historys = cursor.fetchall()

        if not nt_historys:
            logger.error("未获取到NAStool数据库文件中的转移历史，请检查数据库路径是正确")
            return

        logger.info(f"获取到NAStool转移记录 {len(nt_historys)} 条")
        return nt_historys

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
                                            'model': 'transfer',
                                            'label': '同步记录'
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
                                            'model': 'clear',
                                            'label': '清空记录'
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
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'nt_db_path',
                                            'label': 'NAStool数据库user.db路径',
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
                                            'model': 'path',
                                            'rows': '2',
                                            'label': '历史记录路径映射',
                                            'placeholder': 'NAStool路径:MoviePilot路径（一行一个）'
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
                                            'model': 'downloader',
                                            'rows': '2',
                                            'label': '插件数据下载器映射',
                                            'placeholder': 'NAStool下载器id:qbittorrent|transmission（一行一个）'
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
                                            'model': 'site',
                                            'label': '下载历史站点映射',
                                            'placeholder': 'NAStool站点名:MoviePilot站点名（一行一个）'
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
                                            'text': '开启清空记录时，会在导入历史数据之前删除MoviePilot之前的记录。'
                                                    '如果转移记录很多，同步时间可能会长（3-10分钟），'
                                                    '所以点击确定后页面没反应是正常现象，后台正在处理。'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "transfer": False,
            "clear": False,
            "supp": False,
            "nt_db_path": "",
            "path": "",
            "downloader": "",
            "site": "",
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        退出插件
        """
        pass
