from functools import lru_cache
from pathlib import Path
from typing import List, Tuple, Dict, Any

from app.core.config import settings
from app.core.event import eventmanager
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, MediaType
from app.utils.http import RequestUtils


class ChineseSubFinder(_PluginBase):
    # 插件名称
    plugin_name = "ChineseSubFinder"
    # 插件描述
    plugin_desc = "通知ChineseSubFinder下载字幕。"
    # 插件图标
    plugin_icon = "chinesesubfinder.png"
    # 主题色
    plugin_color = "#83BE39"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "jxxghp"
    # 作者主页
    author_url = "https://github.com/jxxghp"
    # 插件配置项ID前缀
    plugin_config_prefix = "chinesesubfinder_"
    # 加载顺序
    plugin_order = 5
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _save_tmp_path = None
    _enable = False
    _host = None
    _api_key = None
    _remote_path = None
    _local_path = None
    _remote_path2 = None
    _local_path2 = None
    _remote_path3 = None
    _local_path3 = None

    def init_plugin(self, config: dict = None):
        self._save_tmp_path = settings.TEMP_PATH
        if config:
            self._enable = config.get("enable")
            self._api_key = config.get("api_key")
            self._host = config.get('host')
            if self._host:
                if not self._host.startswith('http'):
                    self._host = "http://" + self._host
                if not self._host.endswith('/'):
                    self._host = self._host + "/"
            self._local_path = config.get("local_path")
            self._remote_path = config.get("remote_path")
            self._local_path2 = config.get("local_path2")
            self._remote_path2 = config.get("remote_path2")
            self._local_path3 = config.get("local_path3")
            self._remote_path3 = config.get("remote_path3")

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        pass

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        pass

    @eventmanager.register(EventType.TransferComplete)
    def download(self, event):
        """
        调用ChineseSubFinder下载字幕
        """
        if not self._host or not self._api_key:
            return
        item = event.event_data
        if not item:
            return
        # FIXME
        req_url = "%sapi/v1/add-job" % self._host

        item_media = item.get("media_info")
        item_type = item_media.get("type")
        item_bluray = item.get("bluray")
        item_file = item.get("file")
        item_file_ext = item.get("file_ext")

        if item_bluray:
            file_path = "%s.mp4" % item_file
        else:
            if Path(item_file).suffix != item_file_ext:
                file_path = "%s%s" % (item_file, item_file_ext)
            else:
                file_path = item_file

        # 路径替换
        if self._local_path and self._remote_path and file_path.startswith(self._local_path):
            file_path = file_path.replace(self._local_path, self._remote_path).replace('\\', '/')

        if self._local_path2 and self._remote_path2 and file_path.startswith(self._local_path2):
            file_path = file_path.replace(self._local_path2, self._remote_path2).replace('\\', '/')

        if self._local_path3 and self._remote_path3 and file_path.startswith(self._local_path3):
            file_path = file_path.replace(self._local_path3, self._remote_path3).replace('\\', '/')

        # 调用CSF下载字幕
        self.__request_csf(req_url=req_url,
                           file_path=file_path,
                           item_type=0 if item_type == MediaType.MOVIE.value else 1,
                           item_bluray=item_bluray)

    @lru_cache(maxsize=128)
    def __request_csf(self, req_url, file_path, item_type, item_bluray):
        # 一个名称只建一个任务
        logger.info("通知ChineseSubFinder下载字幕: %s" % file_path)
        params = {
            "video_type": item_type,
            "physical_video_file_full_path": file_path,
            "task_priority_level": 3,
            "media_server_inside_video_id": "",
            "is_bluray": item_bluray
        }
        try:
            res = RequestUtils(headers={
                "Authorization": "Bearer %s" % self._api_key
            }).post(req_url, json=params)
            if not res or res.status_code != 200:
                logger.error("调用ChineseSubFinder API失败！")
            else:
                # 如果文件目录没有识别的nfo元数据， 此接口会返回控制符，推测是ChineseSubFinder的原因
                # emby refresh元数据时异步的
                if res.text:
                    job_id = res.json().get("job_id")
                    message = res.json().get("message")
                    if not job_id:
                        logger.warn("ChineseSubFinder下载字幕出错：%s" % message)
                    else:
                        logger.info("ChineseSubFinder任务添加成功：%s" % job_id)
                else:
                    logger.error("%s 目录缺失nfo元数据" % file_path)
        except Exception as e:
            logger.error("连接ChineseSubFinder出错：" + str(e))
