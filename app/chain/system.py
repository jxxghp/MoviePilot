import json
import re
from typing import Union

from app.chain import ChainBase
from app.core.config import settings
from app.log import logger
from app.schemas import Notification, MessageChannel
from app.utils.http import RequestUtils
from app.utils.singleton import Singleton
from app.utils.system import SystemUtils


class SystemChain(ChainBase, metaclass=Singleton):
    """
    系统级处理链
    """

    _restart_file = "__system_restart__"

    def remote_clear_cache(self, channel: MessageChannel, userid: Union[int, str]):
        """
        清理系统缓存
        """
        self.clear_cache()
        self.post_message(Notification(channel=channel,
                                       title=f"缓存清理完成！", userid=userid))

    def restart(self, channel: MessageChannel, userid: Union[int, str]):
        """
        重启系统
        """
        if channel and userid:
            self.post_message(Notification(channel=channel,
                                           title="系统正在重启，请耐心等候！", userid=userid))
            # 保存重启信息
            self.save_cache({
                "channel": channel.value,
                "userid": userid
            }, self._restart_file)
        SystemUtils.restart()

    def version(self, channel: MessageChannel, userid: Union[int, str]):
        """
        查看当前版本、远程版本
        """
        release_version = self.__get_release_version()
        local_version = self.get_local_version()
        if release_version == local_version:
            title = f"当前版本：{local_version}，已是最新版本"
        else:
            title = f"当前版本：{local_version}，远程版本：{release_version}"

        self.post_message(Notification(channel=channel,
                                       title=title, userid=userid))

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
            release_version = self.__get_release_version()
            local_version = self.get_local_version()
            if release_version == local_version:
                title = f"当前版本：{local_version}"
            else:
                title = f"当前版本：{local_version}，远程版本：{release_version}"
            self.post_message(Notification(channel=channel,
                                           title=f"系统已重启完成！{title}",
                                           userid=userid))
            self.remove_cache(self._restart_file)

    @staticmethod
    def __get_release_version():
        """
        获取最新版本
        """
        version_res = RequestUtils(proxies=settings.PROXY, headers=settings.GITHUB_HEADERS).get_res(
            "https://api.github.com/repos/jxxghp/MoviePilot/releases/latest")
        if version_res:
            ver_json = version_res.json()
            version = f"{ver_json['tag_name']}"
            return version
        else:
            return None

    @staticmethod
    def get_local_version():
        """
        查看当前版本
        """
        version_file = settings.ROOT_PATH / "version.py"
        if version_file.exists():
            try:
                with open(version_file, 'rb') as f:
                    version = f.read()
                pattern = r"'([^']*)'"
                match = re.search(pattern, str(version))

                if match:
                    version = match.group(1)
                    return version
                else:
                    logger.warn("未找到版本号")
                    return None
            except Exception as err:
                logger.error(f"加载版本文件 {version_file} 出错：{str(err)}")
