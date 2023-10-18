import os
from datetime import datetime, timedelta
from pathlib import Path
from threading import Event
from typing import Any, List, Dict, Tuple, Optional

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from bencode import bdecode, bencode

from app.core.config import settings
from app.helper.torrent import TorrentHelper
from app.log import logger
from app.modules.qbittorrent import Qbittorrent
from app.modules.transmission import Transmission
from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.utils.string import StringUtils


class TorrentTransfer(_PluginBase):
    # 插件名称
    plugin_name = "自动转移做种"
    # 插件描述
    plugin_desc = "定期转移下载器中的做种任务到另一个下载器。"
    # 插件图标
    plugin_icon = "torrenttransfer.jpg"
    # 主题色
    plugin_color = "#272636"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "jxxghp"
    # 作者主页
    author_url = "https://github.com/jxxghp"
    # 插件配置项ID前缀
    plugin_config_prefix = "torrenttransfer_"
    # 加载顺序
    plugin_order = 18
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    _scheduler = None
    qb = None
    tr = None
    torrent = None
    # 开关
    _enabled = False
    _cron = None
    _onlyonce = False
    _fromdownloader = None
    _todownloader = None
    _frompath = None
    _topath = None
    _notify = False
    _nolabels = None
    _nopaths = None
    _deletesource = False
    _fromtorrentpath = None
    _autostart = False
    # 退出事件
    _event = Event()
    # 待检查种子清单
    _recheck_torrents = {}
    _is_recheck_running = False
    # 任务标签
    _torrent_tags = ["已整理", "转移做种"]

    def init_plugin(self, config: dict = None):
        self.torrent = TorrentHelper()
        # 读取配置
        if config:
            self._enabled = config.get("enabled")
            self._onlyonce = config.get("onlyonce")
            self._cron = config.get("cron")
            self._notify = config.get("notify")
            self._nolabels = config.get("nolabels")
            self._frompath = config.get("frompath")
            self._topath = config.get("topath")
            self._fromdownloader = config.get("fromdownloader")
            self._todownloader = config.get("todownloader")
            self._deletesource = config.get("deletesource")
            self._fromtorrentpath = config.get("fromtorrentpath")
            self._nopaths = config.get("nopaths")
            self._autostart = config.get("autostart")

        # 停止现有任务
        self.stop_service()

        # 启动定时任务 & 立即运行一次
        if self.get_state() or self._onlyonce:
            self.qb = Qbittorrent()
            self.tr = Transmission()
            # 检查配置
            if self._fromtorrentpath and not Path(self._fromtorrentpath).exists():
                logger.error(f"源下载器种子文件保存路径不存在：{self._fromtorrentpath}")
                self.systemmessage.put(f"源下载器种子文件保存路径不存在：{self._fromtorrentpath}")
                return
            if self._fromdownloader == self._todownloader:
                logger.error(f"源下载器和目的下载器不能相同")
                self.systemmessage.put(f"源下载器和目的下载器不能相同")
                return
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            if self._cron:
                logger.info(f"转移做种服务启动，周期：{self._cron}")
                try:
                    self._scheduler.add_job(self.transfer,
                                            CronTrigger.from_crontab(self._cron))
                except Exception as e:
                    logger.error(f"转移做种服务启动失败：{str(e)}")
                    self.systemmessage.put(f"转移做种服务启动失败：{str(e)}")
                    return
            if self._onlyonce:
                logger.info(f"转移做种服务启动，立即运行一次")
                self._scheduler.add_job(self.transfer, 'date',
                                        run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(
                                            seconds=3))
                # 关闭一次性开关
                self._onlyonce = False
                self.update_config({
                    "enabled": self._enabled,
                    "onlyonce": self._onlyonce,
                    "cron": self._cron,
                    "notify": self._notify,
                    "nolabels": self._nolabels,
                    "frompath": self._frompath,
                    "topath": self._topath,
                    "fromdownloader": self._fromdownloader,
                    "todownloader": self._todownloader,
                    "deletesource": self._deletesource,
                    "fromtorrentpath": self._fromtorrentpath,
                    "nopaths": self._nopaths,
                    "autostart": self._autostart
                })
            if self._scheduler.get_jobs():
                if self._autostart:
                    # 追加种子校验服务
                    self._scheduler.add_job(self.check_recheck, 'interval', minutes=3)
                # 启动服务
                self._scheduler.print_jobs()
                self._scheduler.start()

    def get_state(self):
        return True if self._enabled \
                       and self._cron \
                       and self._fromdownloader \
                       and self._todownloader \
                       and self._fromtorrentpath else False

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
                                            'label': '发送通知',
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
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '执行周期',
                                            'placeholder': '0 0 0 ? *'
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
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'nolabels',
                                            'label': '不转移种子标签',
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
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'fromdownloader',
                                            'label': '源下载器',
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
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'fromtorrentpath',
                                            'label': '源下载器种子文件路径',
                                            'placeholder': 'BT_backup、torrents'
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
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'frompath',
                                            'label': '源数据文件根路径',
                                            'placeholder': '根路径，留空不进行路径转换'
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
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'todownloader',
                                            'label': '目的下载器',
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
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'topath',
                                            'label': '目的数据文件根路径',
                                            'placeholder': '根路径，留空不进行路径转换'
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
                                            'model': 'nopaths',
                                            'label': '不转移数据文件目录',
                                            'rows': 3,
                                            'placeholder': '每一行一个目录'
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
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'autostart',
                                            'label': '校验完成后自动开始',
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
                                            'model': 'deletesource',
                                            'label': '删除源种子',
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
                    }
                ]
            }
        ], {
            "enabled": False,
            "notify": False,
            "onlyonce": False,
            "cron": "",
            "nolabels": "",
            "frompath": "",
            "topath": "",
            "fromdownloader": "",
            "todownloader": "",
            "deletesource": False,
            "fromtorrentpath": "",
            "nopaths": "",
            "autostart": True
        }

    def get_page(self) -> List[dict]:
        pass

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

    def __download(self, downloader: str, content: bytes,
                   save_path: str) -> Optional[str]:
        """
        添加下载任务
        """
        if downloader == "qbittorrent":
            # 生成随机Tag
            tag = StringUtils.generate_random_str(10)
            state = self.qb.add_torrent(content=content,
                                        download_dir=save_path,
                                        is_paused=True,
                                        tag=["已整理", "转移做种", tag])
            if not state:
                return None
            else:
                # 获取种子Hash
                torrent_hash = self.qb.get_torrent_id_by_tag(tags=tag)
                if not torrent_hash:
                    logger.error(f"{downloader} 获取种子Hash失败")
                    return None
            return torrent_hash
        elif downloader == "transmission":
            # 添加任务
            torrent = self.tr.add_torrent(content=content,
                                          download_dir=save_path,
                                          is_paused=True,
                                          labels=["已整理", "转移做种"])
            if not torrent:
                return None
            else:
                return torrent.hashString

        logger.error(f"不支持的下载器：{downloader}")
        return None

    def transfer(self):
        """
        开始转移做种
        """
        logger.info("开始转移做种任务 ...")

        # 源下载器
        downloader = self._fromdownloader
        # 目的下载器
        todownloader = self._todownloader

        # 获取下载器中已完成的种子
        downloader_obj = self.__get_downloader(downloader)
        torrents = downloader_obj.get_completed_torrents()
        if torrents:
            logger.info(f"下载器 {downloader} 已完成种子数：{len(torrents)}")
        else:
            logger.info(f"下载器 {downloader} 没有已完成种子")
            return

        # 过滤种子，记录保存目录
        trans_torrents = []
        for torrent in torrents:
            if self._event.is_set():
                logger.info(f"转移服务停止")
                return

            # 获取种子hash
            hash_str = self.__get_hash(torrent, downloader)
            # 获取保存路径
            save_path = self.__get_save_path(torrent, downloader)

            if self._nopaths and save_path:
                # 过滤不需要转移的路径
                nopath_skip = False
                for nopath in self._nopaths.split('\n'):
                    if os.path.normpath(save_path).startswith(os.path.normpath(nopath)):
                        logger.info(f"种子 {hash_str} 保存路径 {save_path} 不需要转移，跳过 ...")
                        nopath_skip = True
                        break
                if nopath_skip:
                    continue

            # 获取种子标签
            torrent_labels = self.__get_label(torrent, downloader)
            if torrent_labels and self._nolabels:
                is_skip = False
                for label in self._nolabels.split(','):
                    if label in torrent_labels:
                        logger.info(f"种子 {hash_str} 含有不转移标签 {label}，跳过 ...")
                        is_skip = True
                        break
                if is_skip:
                    continue

            # 添加转移数据
            trans_torrents.append({
                "hash": hash_str,
                "save_path": save_path,
                "torrent": torrent
            })

        # 开始转移任务
        if trans_torrents:
            logger.info(f"需要转移的种子数：{len(trans_torrents)}")
            # 记数
            total = len(trans_torrents)
            # 总成功数
            success = 0
            # 总失败数
            fail = 0
            # 跳过数
            skip = 0

            for torrent_item in trans_torrents:
                # 检查种子文件是否存在
                torrent_file = Path(self._fromtorrentpath) / f"{torrent_item.get('hash')}.torrent"
                if not torrent_file.exists():
                    logger.error(f"种子文件不存在：{torrent_file}")
                    # 失败计数
                    fail += 1
                    continue

                # 查询hash值是否已经在目的下载器中
                todownloader_obj = self.__get_downloader(todownloader)
                torrent_info, _ = todownloader_obj.get_torrents(ids=[torrent_item.get('hash')])
                if torrent_info:
                    logger.info(f"{torrent_item.get('hash')} 已在目的下载器中，跳过 ...")
                    # 跳过计数
                    skip += 1
                    continue

                # 转换保存路径
                download_dir = self.__convert_save_path(torrent_item.get('save_path'),
                                                        self._frompath,
                                                        self._topath)
                if not download_dir:
                    logger.error(f"转换保存路径失败：{torrent_item.get('save_path')}")
                    # 失败计数
                    fail += 1
                    continue

                # 如果源下载器是QB检查是否有Tracker，没有的话额外获取
                if downloader == "qbittorrent":
                    # 读取种子内容、解析种子文件
                    content = torrent_file.read_bytes()
                    if not content:
                        logger.warn(f"读取种子文件失败：{torrent_file}")
                        fail += 1
                        continue
                    # 读取trackers
                    try:
                        torrent_main = bdecode(content)
                        main_announce = torrent_main.get('announce')
                    except Exception as err:
                        logger.warn(f"解析种子文件 {torrent_file} 失败：{str(err)}")
                        fail += 1
                        continue

                    if not main_announce:
                        logger.info(f"{torrent_item.get('hash')} 未发现tracker信息，尝试补充tracker信息...")
                        # 读取fastresume文件
                        fastresume_file = Path(self._fromtorrentpath) / f"{torrent_item.get('hash')}.fastresume"
                        if not fastresume_file.exists():
                            logger.warn(f"fastresume文件不存在：{fastresume_file}")
                            fail += 1
                            continue
                        # 尝试补充trackers
                        try:
                            # 解析fastresume文件
                            fastresume = fastresume_file.read_bytes()
                            torrent_fastresume = bdecode(fastresume)
                            # 读取trackers
                            fastresume_trackers = torrent_fastresume.get('trackers')
                            if isinstance(fastresume_trackers, list) \
                                    and len(fastresume_trackers) > 0 \
                                    and fastresume_trackers[0]:
                                # 重新赋值
                                torrent_main['announce'] = fastresume_trackers[0][0]
                                # 替换种子文件路径
                                torrent_file = settings.TEMP_PATH / f"{torrent_item.get('hash')}.torrent"
                                # 编码并保存到临时文件
                                torrent_file.write_bytes(bencode(torrent_main))
                        except Exception as err:
                            logger.error(f"解析fastresume文件 {fastresume_file} 出错：{str(err)}")
                            fail += 1
                            continue

                # 发送到另一个下载器中下载：默认暂停、传输下载路径、关闭自动管理模式
                logger.info(f"添加转移做种任务到下载器 {todownloader}：{torrent_file}")
                download_id = self.__download(downloader=todownloader,
                                              content=torrent_file.read_bytes(),
                                              save_path=download_dir)
                if not download_id:
                    # 下载失败
                    fail += 1
                    logger.error(f"添加下载任务失败：{torrent_file}")
                    continue
                else:
                    # 下载成功
                    logger.info(f"成功添加转移做种任务，种子文件：{torrent_file}")

                    # TR会自动校验，QB需要手动校验
                    if todownloader == "qbittorrent":
                        logger.info(f"qbittorrent 开始校验 {download_id} ...")
                        todownloader_obj.recheck_torrents(ids=[download_id])

                    # 追加校验任务
                    logger.info(f"添加校验检查任务：{download_id} ...")
                    if not self._recheck_torrents.get(todownloader):
                        self._recheck_torrents[todownloader] = []
                    self._recheck_torrents[todownloader].append(download_id)

                    # 删除源种子，不能删除文件！
                    if self._deletesource:
                        logger.info(f"删除源下载器任务（不含文件）：{torrent_item.get('hash')} ...")
                        downloader_obj.delete_torrents(delete_file=False, ids=[torrent_item.get('hash')])

                    # 成功计数
                    success += 1
                    # 插入转种记录
                    history_key = "%s-%s" % (self._fromdownloader, torrent_item.get('hash'))
                    self.save_data(key=history_key,
                                   value={
                                       "to_download": self._todownloader,
                                       "to_download_id": download_id,
                                       "delete_source": self._deletesource,
                                   })
            # 触发校验任务
            if success > 0 and self._autostart:
                self.check_recheck()

            # 发送通知
            if self._notify:
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title="【转移做种任务执行完成】",
                    text=f"总数：{total}，成功：{success}，失败：{fail}，跳过：{skip}"
                )
        else:
            logger.info(f"没有需要转移的种子")
        logger.info("转移做种任务执行完成")

    def check_recheck(self):
        """
        定时检查下载器中种子是否校验完成，校验完成且完整的自动开始辅种
        """
        if not self._recheck_torrents:
            return
        if not self._todownloader:
            return
        if self._is_recheck_running:
            return

        # 校验下载器
        downloader = self._todownloader

        # 需要检查的种子
        recheck_torrents = self._recheck_torrents.get(downloader, [])
        if not recheck_torrents:
            return

        logger.info(f"开始检查下载器 {downloader} 的校验任务 ...")

        # 运行状态
        self._is_recheck_running = True

        # 获取任务
        downloader_obj = self.__get_downloader(downloader)
        torrents, _ = downloader_obj.get_torrents(ids=recheck_torrents)
        if torrents:
            # 可做种的种子
            can_seeding_torrents = []
            for torrent in torrents:
                # 获取种子hash
                hash_str = self.__get_hash(torrent, downloader)
                # 判断是否可做种
                if self.__can_seeding(torrent, downloader):
                    can_seeding_torrents.append(hash_str)

            if can_seeding_torrents:
                logger.info(f"共 {len(can_seeding_torrents)} 个任务校验完成，开始做种")
                # 开始做种
                downloader_obj.start_torrents(ids=can_seeding_torrents)
                # 去除已经处理过的种子
                self._recheck_torrents[downloader] = list(
                    set(recheck_torrents).difference(set(can_seeding_torrents)))
            else:
                logger.info(f"没有新的任务校验完成，将在下次个周期继续检查 ...")

        elif torrents is None:
            logger.info(f"下载器 {downloader} 查询校验任务失败，将在下次继续查询 ...")
        else:
            logger.info(f"下载器 {downloader} 中没有需要检查的校验任务，清空待处理列表")
            self._recheck_torrents[downloader] = []

        self._is_recheck_running = False

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

    @staticmethod
    def __get_label(torrent: Any, dl_type: str):
        """
        获取种子标签
        """
        try:
            return [str(tag).strip() for tag in torrent.get("tags").split(',')] \
                if dl_type == "qbittorrent" else torrent.labels or []
        except Exception as e:
            print(str(e))
            return []

    @staticmethod
    def __get_save_path(torrent: Any, dl_type: str):
        """
        获取种子保存路径
        """
        try:
            return torrent.get("save_path") if dl_type == "qbittorrent" else torrent.download_dir
        except Exception as e:
            print(str(e))
            return ""

    @staticmethod
    def __can_seeding(torrent: Any, dl_type: str):
        """
        判断种子是否可以做种并处于暂停状态
        """
        try:
            return (torrent.get("state") == "pausedUP") if dl_type == "qbittorrent" \
                else (torrent.status.stopped and torrent.percent_done == 1)
        except Exception as e:
            print(str(e))
            return False

    @staticmethod
    def __convert_save_path(save_path: str, from_root: str, to_root: str):
        """
        转换保存路径
        """
        try:
            # 没有保存目录，以目的根目录为准
            if not save_path:
                return to_root
            # 没有设置根目录时返回save_path
            if not to_root or not from_root:
                return save_path
            # 统一目录格式
            save_path = os.path.normpath(save_path).replace("\\", "/")
            from_root = os.path.normpath(from_root).replace("\\", "/")
            to_root = os.path.normpath(to_root).replace("\\", "/")
            # 替换根目录
            if save_path.startswith(from_root):
                return save_path.replace(from_root, to_root, 1)
        except Exception as e:
            print(str(e))
        return None

    def stop_service(self):
        """
        退出插件
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._event.set()
                    self._scheduler.shutdown()
                    self._event.clear()
                self._scheduler = None
        except Exception as e:
            print(str(e))
