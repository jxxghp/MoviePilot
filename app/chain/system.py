import json
import re
from pathlib import Path
from typing import Union, Optional

from app.chain import ChainBase
from app.core.config import settings
from app.log import logger
from app.schemas import Notification, MessageChannel
from app.utils.http import RequestUtils
from app.utils.singleton import Singleton
from app.utils.system import SystemUtils
from version import FRONTEND_VERSION, APP_VERSION


class SystemChain(ChainBase, metaclass=Singleton):
    """
    系统级处理链
    """

    _restart_file = "__system_restart__"

    def __init__(self):
        super().__init__()
        # 重启完成检测
        self.restart_finish()

    def remote_clear_cache(self, channel: MessageChannel, userid: Union[int, str], source: Optional[str] = None):
        """
        清理系统缓存
        """
        self.clear_cache()
        self.post_message(Notification(channel=channel, source=source,
                                       title=f"缓存清理完成！", userid=userid))

    def restart(self, channel: MessageChannel, userid: Union[int, str], source: Optional[str] = None):
        """
        重启系统
        """
        if channel and userid:
            self.post_message(Notification(channel=channel, source=source,
                                           title="系统正在重启，请耐心等候！", userid=userid))
            # 保存重启信息
            self.save_cache({
                "channel": channel.value,
                "userid": userid
            }, self._restart_file)
        SystemUtils.restart()

    def __get_version_message(self) -> str:
        """
        获取版本信息文本
        """
        server_release_version = self.__get_server_release_version()
        front_release_version = self.__get_front_release_version()
        server_local_version = self.get_server_local_version()
        front_local_version = self.get_frontend_version()
        if server_release_version == server_local_version:
            title = f"当前后端版本：{server_local_version}，已是最新版本\n"
        else:
            title = f"当前后端版本：{server_local_version}，远程版本：{server_release_version}\n"
        if front_release_version == front_local_version:
            title += f"当前前端版本：{front_local_version}，已是最新版本"
        else:
            title += f"当前前端版本：{front_local_version}，远程版本：{front_release_version}"
        return title

    def version(self, channel: MessageChannel, userid: Union[int, str], source: Optional[str] = None):
        """
        查看当前版本、远程版本
        """
        self.post_message(Notification(channel=channel, source=source,
                                       title=self.__get_version_message(),
                                       userid=userid))

    def restart_finish(self):
        """
        如通过交互命令重启，
        重启完发送msg
        """
        # 重启消息
        restart_channel = self.load_cache(self._restart_file)
        if restart_channel:
            # 发送重启完成msg
            if not isinstance(restart_channel, dict):
                restart_channel = json.loads(restart_channel)
            channel = next(
                (channel for channel in MessageChannel.__members__.values() if
                 channel.value == restart_channel.get('channel')), None)
            userid = restart_channel.get('userid')

            # 版本号
            title = self.__get_version_message()
            self.post_message(Notification(channel=channel,
                                           title=f"系统已重启完成！\n{title}",
                                           userid=userid))
            self.remove_cache(self._restart_file)

    @staticmethod
    def __get_server_release_version():
        """
        获取后端V2最新版本
        """
        try:
            # 获取所有发布的版本列表
            response = RequestUtils(
                proxies=settings.PROXY,
                headers=settings.GITHUB_HEADERS
            ).get_res("https://api.github.com/repos/jxxghp/MoviePilot/releases")
            if response:
                releases = [release['tag_name'] for release in response.json()]
                v2_releases = [tag for tag in releases if re.match(r"^v2\.", tag)]
                if not v2_releases:
                    logger.warn("获取v2后端最新版本版本出错！")
                else:
                    # 找到最新的v2版本
                    latest_v2 = sorted(v2_releases, key=lambda s: list(map(int, re.findall(r'\d+', s))))[-1]
                    logger.info(f"获取到后端最新版本：{latest_v2}")
                    return latest_v2
            else:
                logger.error("无法获取后端版本信息，请检查网络连接或GitHub API请求。")
        except Exception as err:
            logger.error(f"获取后端最新版本失败：{str(err)}")
        return None

    @staticmethod
    def __get_front_release_version():
        """
        获取前端V2最新版本
        """
        try:
            # 获取所有发布的版本列表
            response = RequestUtils(
                proxies=settings.PROXY,
                headers=settings.GITHUB_HEADERS
            ).get_res("https://api.github.com/repos/jxxghp/MoviePilot-Frontend/releases")
            if response:
                releases = [release['tag_name'] for release in response.json()]
                v2_releases = [tag for tag in releases if re.match(r"^v2\.", tag)]
                if not v2_releases:
                    logger.warn("获取v2前端最新版本版本出错！")
                else:
                    # 找到最新的v2版本
                    latest_v2 = sorted(v2_releases, key=lambda s: list(map(int, re.findall(r'\d+', s))))[-1]
                    logger.info(f"获取到前端最新版本：{latest_v2}")
                    return latest_v2
            else:
                logger.error("无法获取前端版本信息，请检查网络连接或GitHub API请求。")
        except Exception as err:
            logger.error(f"获取前端最新版本失败：{str(err)}")
        return None

    @staticmethod
    def get_server_local_version():
        """
        查看当前版本
        """
        return APP_VERSION

    @staticmethod
    def get_frontend_version():
        """
        获取前端版本
        """
        if SystemUtils.is_frozen() and SystemUtils.is_windows():
            version_file = settings.CONFIG_PATH.parent / "nginx" / "html" / "version.txt"
        else:
            version_file = Path(settings.FRONTEND_PATH) / "version.txt"
        if version_file.exists():
            try:
                with open(version_file, 'r') as f:
                    version = str(f.read()).strip()
                return version
            except Exception as err:
                logger.debug(f"加载版本文件 {version_file} 出错：{str(err)}")
        return FRONTEND_VERSION
