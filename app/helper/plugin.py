import json
import shutil
from pathlib import Path
from typing import Dict, Tuple

from cachetools import TTLCache, cached

from app.core.config import settings
from app.utils.http import RequestUtils
from app.utils.singleton import Singleton
from app.utils.system import SystemUtils


class PluginHelper(metaclass=Singleton):
    """
    插件市场管理，下载安装插件到本地
    """

    @cached(cache=TTLCache(maxsize=1, ttl=1800))
    def get_plugins(self, repo_url: str) -> Dict[str, dict]:
        """
        获取Github所有最新插件列表
        :param repo_url: Github仓库地址
        """
        if not repo_url:
            return {}
        res = RequestUtils(proxies=settings.PROXY).get_res(f"{repo_url}package.json")
        if res:
            return json.loads(res.text)
        return {}

    @staticmethod
    def install(pid: str, repo_url: str) -> Tuple[bool, str]:
        """
        安装插件
        """
        if not pid or not repo_url:
            return False, "参数错误"
        # 从Github的repo_url获取用户和项目名
        try:
            user, repo = repo_url.split("/")[-4:-2]
        except Exception as e:
            return False, f"不支持的插件仓库地址格式：{str(e)}"
        if not user or not repo:
            return False, "不支持的插件仓库地址格式"
        if SystemUtils.is_frozen():
            return False, "可执行文件模式下，只能安装本地插件"
        # 获取插件的文件列表
        """
        [
            {
                "name": "__init__.py",
                "path": "plugins/autobackup/__init__.py",
                "sha": "cd10eba3f0355d61adeb35561cb26a0a36c15a6c",
                "size": 12385,
                "url": "https://api.github.com/repos/jxxghp/MoviePilot-Plugins/contents/plugins/autobackup/__init__.py?ref=main",
                "html_url": "https://github.com/jxxghp/MoviePilot-Plugins/blob/main/plugins/autobackup/__init__.py",
                "git_url": "https://api.github.com/repos/jxxghp/MoviePilot-Plugins/git/blobs/cd10eba3f0355d61adeb35561cb26a0a36c15a6c",
                "download_url": "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/plugins/autobackup/__init__.py",
                "type": "file",
                "_links": {
                    "self": "https://api.github.com/repos/jxxghp/MoviePilot-Plugins/contents/plugins/autobackup/__init__.py?ref=main",
                    "git": "https://api.github.com/repos/jxxghp/MoviePilot-Plugins/git/blobs/cd10eba3f0355d61adeb35561cb26a0a36c15a6c",
                    "html": "https://github.com/jxxghp/MoviePilot-Plugins/blob/main/plugins/autobackup/__init__.py"
                }
            }
        ]
        """
        file_api = f"https://api.github.com/repos/{user}/{repo}/contents/plugins/{pid.lower()}"
        res = RequestUtils(proxies=settings.PROXY).get_res(file_api)
        if not res or res.status_code != 200:
            return False, f"连接仓库失败：{res.status_code} - {res.reason}"
        ret_json = res.json()
        if ret_json and ret_json[0].get("message") == "Not Found":
            return False, "插件在仓库中不存在"
        # 本地存在时先删除
        plugin_dir = Path(settings.ROOT_PATH) / "app" / "plugins" / pid.lower()
        if plugin_dir.exists():
            shutil.rmtree(plugin_dir, ignore_errors=True)
        # 下载所有文件
        for item in ret_json:
            if item.get("download_url"):
                # 下载插件文件
                res = RequestUtils(proxies=settings.PROXY).get_res(item["download_url"])
                if not res or res.status_code != 200:
                    return False, f"下载文件 {item.get('name')} 失败：{res.status_code} - {res.reason}"
                # 创建插件文件夹
                file_path = Path(settings.ROOT_PATH) / "app" / item.get("path")
                if not file_path.parent.exists():
                    file_path.parent.mkdir(parents=True, exist_ok=True)
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(res.text)
        return True, ""
