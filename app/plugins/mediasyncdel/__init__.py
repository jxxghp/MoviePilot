import os
from typing import List, Tuple, Dict, Any

from app.core.event import eventmanager
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType


class MediaSyncDel(_PluginBase):
    # 插件名称
    plugin_name = "Emby同步删除"
    # 插件描述
    plugin_desc = "Emby删除媒体后同步删除历史记录或源文件。"
    # 插件图标
    plugin_icon = "emby.png"
    # 主题色
    plugin_color = "#C90425"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "mediasyncdel_"
    # 加载顺序
    plugin_order = 15
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    filetransfer = None
    _enable = False
    _del_source = False
    _exclude_path = None
    _send_notify = False

    def init_plugin(self, config: dict = None):
        # 读取配置
        if config:
            self._enable = config.get("enable")
            self._del_source = config.get("del_source")
            self._exclude_path = config.get("exclude_path")
            self._send_notify = config.get("send_notify")

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        pass

    def get_page(self) -> List[dict]:
        pass

    @eventmanager.register(EventType.WebhookMessage)
    def sync_del(self, event):
        """
        emby删除媒体库同步删除历史记录
        """
        if not self._enable:
            return
        event_data = event.event_data
        event_type = event_data.get("event_type")
        if not event_type or str(event_type) != 'media_del':
            return

        # 是否虚拟标识
        item_isvirtual = event_data.get("item_isvirtual")
        if not item_isvirtual:
            logger.error("item_isvirtual参数未配置，为防止误删除，暂停插件运行")
            self.update_config({
                "enable": False,
                "del_source": self._del_source,
                "exclude_path": self._exclude_path,
                "send_notify": self._send_notify
            })
            return

        # 如果是虚拟item，则直接return，不进行删除
        if item_isvirtual == 'True':
            return

        # 媒体类型
        media_type = event_data.get("media_type")
        # 媒体名称
        media_name = event_data.get("media_name")
        # 媒体路径
        media_path = event_data.get("media_path")
        # tmdb_id
        tmdb_id = event_data.get("tmdb_id")
        # 季数
        season_num = event_data.get("season_num")
        if season_num and str(season_num).isdigit() and int(season_num) < 10:
            season_num = f'0{season_num}'
        # 集数
        episode_num = event_data.get("episode_num")
        if episode_num and str(episode_num).isdigit() and int(episode_num) < 10:
            episode_num = f'0{episode_num}'

        if not media_type:
            logger.error(f"{media_name} 同步删除失败，未获取到媒体类型")
            return
        if not tmdb_id or not str(tmdb_id).isdigit():
            logger.error(f"{media_name} 同步删除失败，未获取到TMDB ID")
            return

        if self._exclude_path and media_path and any(
                os.path.abspath(media_path).startswith(os.path.abspath(path)) for path in
                self._exclude_path.split(",")):
            logger.info(f"媒体路径 {media_path} 已被排除，暂不处理")
            return

        # TODO 删除电影
        if media_type == "Movie":
            msg = f'电影 {media_name} {tmdb_id}'
            logger.info(f"正在同步删除{msg}")
        # TODO 删除电视剧
        elif media_type == "Series":
            msg = f'剧集 {media_name} {tmdb_id}'
            logger.info(f"正在同步删除{msg}")
        # TODO 删除季 S02
        elif media_type == "Season":
            if not season_num or not str(season_num).isdigit():
                logger.error(f"{media_name} 季同步删除失败，未获取到具体季")
                return
            msg = f'剧集 {media_name} S{season_num} {tmdb_id}'
            logger.info(f"正在同步删除{msg}")
        # 删除剧集S02E02
        elif media_type == "Episode":
            if not season_num or not str(season_num).isdigit() or not episode_num or not str(episode_num).isdigit():
                logger.error(f"{media_name} 集同步删除失败，未获取到具体集")
                return
            msg = f'剧集 {media_name} S{season_num}E{episode_num} {tmdb_id}'
            logger.info(f"正在同步删除{msg}")
        else:
            return

        # TODO 开始删除
        # TODO 发送消息
        if self._send_notify:
            pass
        logger.info(f"同步删除 {msg} 完成！")

    def get_state(self):
        return self._enable

    def stop_service(self):
        """
        退出插件
        """
        pass
