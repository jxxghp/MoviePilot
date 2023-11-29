import json
import shutil
from pathlib import Path
from typing import Dict, Tuple, Optional, List

from cachetools import TTLCache, cached

from app.core.config import settings
from app.utils.http import RequestUtils
from app.utils.singleton import Singleton
from app.utils.system import SystemUtils


class PluginHelper(metaclass=Singleton):
    """
    插件市场管理，下载安装插件到本地
    """

    _base_url = "https://raw.githubusercontent.com/%s/%s/main/"

    @cached(cache=TTLCache(maxsize=10, ttl=1800))
    def get_plugins(self, repo_url: str) -> Dict[str, dict]:
        """
        获取Github所有最新插件列表
        :param repo_url: Github仓库地址
        """
        if not repo_url:
            return {}
        user, repo = self.get_repo_info(repo_url)
        if not user or not repo:
            return {}
        raw_url = self._base_url % (user, repo)
        res = RequestUtils(proxies=settings.PROXY, headers=settings.GITHUB_HEADERS,
                           timeout=10).get_res(f"{raw_url}package.json")
        if res:
            return json.loads(res.text)
        return {}

    @staticmethod
    def get_repo_info(repo_url: str) -> Tuple[Optional[str], Optional[str]]:
        """
        获取Github仓库信息
        :param repo_url: Github仓库地址
        """
        if not repo_url:
            return None, None
        if not repo_url.endswith("/"):
            repo_url += "/"
        if repo_url.count("/") < 6:
            repo_url = f"{repo_url}main/"
        try:
            user, repo = repo_url.split("/")[-4:-2]
        except Exception as e:
            print(str(e))
            return None, None
        return user, repo

    def install(self, pid: str, repo_url: str) -> Tuple[bool, str]:
        """
        安装插件
        """
        if SystemUtils.is_frozen():
            return False, "可执行文件模式下，只能安装本地插件"

        # 从Github的repo_url获取用户和项目名
        user, repo = self.get_repo_info(repo_url)
        if not user or not repo:
            return False, "不支持的插件仓库地址格式"

        def __get_filelist(_p: str) -> Tuple[Optional[list], Optional[str]]:
            """
            获取插件的文件列表
            """
            file_api = f"https://api.github.com/repos/{user}/{repo}/contents/plugins/{_p.lower()}"
            r = RequestUtils(proxies=settings.PROXY, headers=settings.GITHUB_HEADERS, timeout=30).get_res(file_api)
            if not r or r.status_code != 200:
                return None, f"连接仓库失败：{r.status_code} - {r.reason}"
            ret = r.json()
            if ret and ret[0].get("message") == "Not Found":
                return None, "插件在仓库中不存在"
            return ret, ""

        def __download_files(_p: str, _l: List[dict]) -> Tuple[bool, str]:
            """
            下载插件文件
            """
            if not _l:
                return False, "文件列表为空"
            for item in _l:
                if item.get("download_url"):
                    # 下载插件文件
                    res = RequestUtils(proxies=settings.PROXY,
                                       headers=settings.GITHUB_HEADERS, timeout=60).get_res(item["download_url"])
                    if not res:
                        return False, f"文件 {item.get('name')} 下载失败！"
                    elif res.status_code != 200:
                        return False, f"下载文件 {item.get('name')} 失败：{res.status_code} - {res.reason}"
                    # 创建插件文件夹
                    file_path = Path(settings.ROOT_PATH) / "app" / item.get("path")
                    if not file_path.parent.exists():
                        file_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(file_path, "w", encoding="utf-8") as f:
                        f.write(res.text)
                else:
                    # 递归下载子目录
                    p = f"{_p}/{item.get('name')}"
                    l, m = __get_filelist(p)
                    if not l:
                        return False, m
                    return __download_files(p, l)
            return True, ""

        if not pid or not repo_url:
            return False, "参数错误"

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
        # 获取第一级文件列表
        file_list, msg = __get_filelist(pid.lower())
        if not file_list:
            return False, msg
        # 本地存在时先删除
        plugin_dir = Path(settings.ROOT_PATH) / "app" / "plugins" / pid.lower()
        if plugin_dir.exists():
            shutil.rmtree(plugin_dir, ignore_errors=True)
        # 下载所有文件
        __download_files(pid.lower(), file_list)
        # 插件目录下如有requirements.txt则安装依赖
        requirements_file = plugin_dir / "requirements.txt"
        if requirements_file.exists():
            SystemUtils.execute(f"pip install -r {requirements_file}")
        return True, ""
