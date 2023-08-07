import sqlite3
from datetime import datetime

from app.db.transferhistory_oper import TransferHistoryOper
from app.plugins import _PluginBase
from typing import Any, List, Dict, Tuple
from app.log import logger


class NAStoolSync(_PluginBase):
    # 插件名称
    plugin_name = "历史记录同步"
    # 插件描述
    plugin_desc = "同步NAStool历史记录到MoviePilot。"
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
    auth_level = 2

    # 私有属性
    _transferhistory = None
    _clear = None
    _nt_db_path = None
    _path = None

    def init_plugin(self, config: dict = None):
        self._transferhistory = TransferHistoryOper()
        if config:
            self._clear = config.get("clear")
            self._nt_db_path = config.get("nt_db_path")
            self._path = config.get("path")

            if self._nt_db_path:
                # 导入转移历史
                self.sync_transfer_history()

    def sync_transfer_history(self):
        """
        导入nt转移记录
        """
        # 开始计时
        start_time = datetime.now()

        nt_historys = self.get_nt_transfer_history()

        # 清空MoviePilot转移记录
        if self._clear:
            logger.info("MoviePilot转移记录已清空")
            self._transferhistory.truncate()

        # 处理数据，存入mp数据库
        for history in nt_historys:
            msrc = history[0]
            mdest = history[1]
            mmode = history[2]
            mtype = history[3]
            mcategory = history[4]
            mtitle = history[5]
            myear = history[6]
            mtmdbid = history[7]
            mseasons = history[8]
            mepisodes = history[9]
            mimage = history[10]
            mdownload_hash = history[11]
            mdate = history[12]

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
                download_hash=mdownload_hash,
                date=mdate
            )
            logger.debug(f"{mtitle} {myear} {mtmdbid} {mseasons} {mepisodes} 已同步")

        self.update_config(
            {
                "clear": False,
                "nt_db_path": "",
                "path": self._path
            }
        )

        # 计算耗时
        end_time = datetime.now()

        logger.info(f"转移记录已同步完成。总耗时 {(end_time - start_time).seconds} 秒")

    def get_nt_transfer_history(self):
        """
        获取nt转移记录
        """
        # 读取sqlite数据
        gradedb = sqlite3.connect(self._nt_db_path)
        # 创建游标cursor来执行executeＳＱＬ语句
        cursor = gradedb.cursor()
        sql = '''SELECT
                    t.SOURCE_PATH || '/' || t.SOURCE_FILENAME AS src,
                    t.DEST_PATH || '/' || t.DEST_FILENAME AS dest,
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
                    d.DOWNLOAD_ID AS download_hash,
                    t.DATE AS date 
                FROM
                    TRANSFER_HISTORY t
                    LEFT JOIN ( SELECT * FROM DOWNLOAD_HISTORY GROUP BY TMDBID ) d ON t.TITLE = d.TITLE 
                    AND t.TYPE = d.TYPE;'''
        cursor.execute(sql)
        nt_historys = cursor.fetchall()
        cursor.close()

        if not nt_historys:
            logger.error("未获取到NAStool数据库文件中的转移历史，请检查数据库路径是正确")
            return

        logger.info(f"获取到NAStool转移记录 {len(nt_historys)} 条")
        return nt_historys

    def get_state(self) -> bool:
        return True if self._nt_db_path else False

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
                                            'model': 'clear',
                                            'label': '清空记录',
                                            'placeholder': '开启会清空MoviePilot历史记录'
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
                                            'label': '路径映射',
                                            'placeholder': 'NAStool路径:MoviePilot路径（一行一个）'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                ]
            }
        ], {
            "clear": False,
            "nt_db_path": "",
            "path": "",
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        退出插件
        """
        pass
