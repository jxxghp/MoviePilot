import os
from pathlib import Path
from typing import Any, List, Dict, Tuple

from requests import RequestException

from app.chain.tmdb import TmdbChain
from app.core.config import settings
from app.core.event import eventmanager, Event
from app.helper.nfo import NfoReader
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import TransferInfo, MediaInfo
from app.schemas.types import EventType, MediaType
from app.utils.common import retry
from app.utils.http import RequestUtils


class PersonMeta(_PluginBase):
    # 插件名称
    plugin_name = "演职人员刮削"
    # 插件描述
    plugin_desc = "刮削演职人员图片以及中文名称。"
    # 插件图标
    plugin_icon = "actor.png"
    # 主题色
    plugin_color = "#E66E72"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "jxxghp"
    # 作者主页
    author_url = "https://github.com/jxxghp"
    # 插件配置项ID前缀
    plugin_config_prefix = "personmeta_"
    # 加载顺序
    plugin_order = 24
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    tmdbchain = None
    _enabled = False
    _metadir = ""

    def init_plugin(self, config: dict = None):
        self.tmdbchain = TmdbChain(self.db)
        if config:
            self._enabled = config.get("enabled")
            self._metadir = config.get("metadir")

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
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'metadir',
                                            'label': '人物元数据目录',
                                            'placeholder': '/metadata/people'
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
            "metadir": ""
        }

    def get_page(self) -> List[dict]:
        pass

    @eventmanager.register(EventType.TransferComplete)
    def scrap_rt(self, event: Event):
        """
        根据事件实时刮削演员信息
        """
        if not self._enabled:
            return
        # 下载人物头像
        if not self._metadir:
            logger.warning("人物元数据目录未配置，无法下载人物头像")
            return
        # 事件数据
        mediainfo: MediaInfo = event.event_data.get("mediainfo")
        transferinfo: TransferInfo = event.event_data.get("transferinfo")
        if not mediainfo or not transferinfo:
            return
        # 文件路径
        if not transferinfo.file_list_new:
            return
        filepath = Path(transferinfo.file_list_new[0])
        # 电影
        if mediainfo.type == MediaType.MOVIE:
            # nfo文件
            nfofile = filepath.with_name("movie.nfo")
            if not nfofile.exists():
                nfofile = filepath.with_name(f"{filepath.stem}.nfo")
                if not nfofile.exists():
                    logger.warning(f"演职人员刮削 电影nfo文件不存在：{nfofile}")
                    return
        else:
            # nfo文件
            nfofile = filepath.parent.with_name("tvshow.nfo")
            if not nfofile.exists():
                logger.warning(f"演职人员刮削 剧集nfo文件不存在：{nfofile}")
                return
        logger.info(f"演职人员刮削 开始刮削：{filepath}")
        # 主要媒体服务器
        mediaserver = str(settings.MEDIASERVER).split(",")[0]
        # 读取nfo文件
        nfo = NfoReader(nfofile)
        # 读取演员信息
        actors = nfo.get_elements("actor") or []
        for actor in actors:
            # 演员ID
            actor_id = actor.find("tmdbid").text
            if not actor_id:
                continue
            # 演员名称
            actor_name = actor.find("name").text
            if not actor_name:
                continue
            # 查询演员详情
            actor_info = self.tmdbchain.person_detail(int(actor_id))
            if not actor_info:
                continue
            # 演员头像
            actor_image = actor_info.get("profile_path")
            if not actor_image:
                continue
            # 计算保存目录
            if mediaserver == 'jellyfin':
                pers_path = Path(self._metadir) / f"{actor_name[0]}" / f"{actor_name}"
            else:
                pers_path = Path(self._metadir) / f"{actor_name}-tmdb-{actor_id}"
            # 创建目录
            if not pers_path.exists():
                os.makedirs(pers_path, exist_ok=True)
            # 文件路径
            image_path = pers_path / f"folder{Path(actor_image).suffix}"
            if image_path.exists():
                continue
            # 下载图片
            self.download_image(
                image_url=f"https://image.tmdb.org/t/p/original{actor_image}",
                path=image_path
            )
            # 刷新媒体库
            self.chain.refresh_mediaserver(
                mediainfo=mediainfo,
                file_path=filepath
            )
        logger.info(f"演职人员刮削 刮削完成：{filepath}")

    @staticmethod
    @retry(RequestException, logger=logger)
    def download_image(image_url: str, path: Path):
        """
        下载图片，保存到指定路径
        """
        try:
            logger.info(f"正在下载演职人员图片：{image_url} ...")
            r = RequestUtils().get_res(url=image_url, raise_exception=True)
            if r:
                path.write_bytes(r.content)
                logger.info(f"图片已保存：{path}")
            else:
                logger.info(f"图片下载失败，请检查网络连通性：{image_url}")
        except RequestException as err:
            raise err
        except Exception as err:
            logger.error(f"图片下载失败：{err}")

    def stop_service(self):
        """
        退出插件
        """
        pass
