import json
import shutil
import subprocess
import traceback
from pathlib import Path
from typing import Dict, Tuple, Optional, List, Any

from cachetools import TTLCache, cached

from app.core.config import settings
from app.db.systemconfig_oper import SystemConfigOper
from app.log import logger
from app.schemas.types import SystemConfigKey
from app.utils.http import RequestUtils
from app.utils.singleton import Singleton
from app.utils.system import SystemUtils
from app.utils.url import UrlUtils


class PluginHelper(metaclass=Singleton):
    """
    插件市场管理，下载安装插件到本地
    """

    _base_url = "https://raw.githubusercontent.com/{user}/{repo}/main/"
    _install_reg = f"{settings.MP_SERVER_HOST}/plugin/install/{{pid}}"
    _install_report = f"{settings.MP_SERVER_HOST}/plugin/install"
    _install_statistic = f"{settings.MP_SERVER_HOST}/plugin/statistic"

    def __init__(self):
        self.systemconfig = SystemConfigOper()
        if settings.PLUGIN_STATISTIC_SHARE:
            if not self.systemconfig.get(SystemConfigKey.PluginInstallReport):
                if self.install_report():
                    self.systemconfig.set(SystemConfigKey.PluginInstallReport, "1")

    @cached(cache=TTLCache(maxsize=1000, ttl=1800))
    def get_plugins(self, repo_url: str, version: str = None) -> Dict[str, dict]:
        """
        获取Github所有最新插件列表
        :param repo_url: Github仓库地址
        :param version: 版本
        """
        if not repo_url:
            return {}

        user, repo = self.get_repo_info(repo_url)
        if not user or not repo:
            return {}

        raw_url = self._base_url.format(user=user, repo=repo)
        package_url = f"{raw_url}package.{version}.json" if version else f"{raw_url}package.json"

        res = self.__request_with_fallback(package_url, headers=settings.REPO_GITHUB_HEADERS(repo=f"{user}/{repo}"))
        if res:
            try:
                return json.loads(res.text)
            except json.JSONDecodeError:
                logger.error(f"插件包数据解析失败：{res.text}")
        return {}

    @staticmethod
    def get_repo_info(repo_url: str) -> Tuple[Optional[str], Optional[str]]:
        """
        获取GitHub仓库信息
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
            logger.error(f"解析GitHub仓库地址失败：{str(e)} - {traceback.format_exc()}")
            return None, None
        return user, repo

    @cached(cache=TTLCache(maxsize=1, ttl=1800))
    def get_statistic(self) -> Dict:
        """
        获取插件安装统计
        """
        if not settings.PLUGIN_STATISTIC_SHARE:
            return {}
        res = RequestUtils(timeout=10).get_res(self._install_statistic)
        if res and res.status_code == 200:
            return res.json()
        return {}

    def install_reg(self, pid: str) -> bool:
        """
        安装插件统计
        """
        if not settings.PLUGIN_STATISTIC_SHARE:
            return False
        if not pid:
            return False
        install_reg_url = self._install_reg.format(pid=pid)
        res = RequestUtils(timeout=5).get_res(install_reg_url)
        if res and res.status_code == 200:
            return True
        return False

    def install_report(self) -> bool:
        """
        上报存量插件安装统计
        """
        if not settings.PLUGIN_STATISTIC_SHARE:
            return False
        plugins = self.systemconfig.get(SystemConfigKey.UserInstalledPlugins)
        if not plugins:
            return False
        res = RequestUtils(content_type="application/json",
                           timeout=5).post(self._install_report,
                                           json={"plugins": [{"plugin_id": plugin} for plugin in plugins]})
        return True if res else False

    def install(self, pid: str, repo_url: str) -> Tuple[bool, str]:
        """
        安装插件
        """
        if SystemUtils.is_frozen():
            return False, "可执行文件模式下，只能安装本地插件"

        # 验证参数
        if not pid or not repo_url:
            return False, "参数错误"

        # 从GitHub的repo_url获取用户和项目名
        user, repo = self.get_repo_info(repo_url)
        if not user or not repo:
            return False, "不支持的插件仓库地址格式"

        user_repo = f"{user}/{repo}"

        # 获取插件文件列表
        file_list, msg = self.__get_file_list(pid.lower(), user_repo)
        if not file_list:
            return False, msg

        # 删除旧的插件目录
        self.__remove_old_plugin(pid.lower())

        # 下载所有插件文件
        download_success, download_msg = self.__download_files(pid.lower(), file_list, user_repo)
        if not download_success:
            return False, download_msg

        # 插件目录下如有requirements.txt则安装依赖
        success, message = self.__install_dependencies_if_required(pid.lower())
        if not success:
            return False, message

        # 插件安装成功后，统计安装信息
        self.install_reg(pid)
        return True, ""

    def __get_file_list(self, pid: str, user_repo: str) -> Tuple[Optional[list], Optional[str]]:
        """
        获取插件的文件列表
        """
        file_api = f"https://api.github.com/repos/{user_repo}/contents/plugins/{pid}"
        res = self.__request_with_fallback(file_api,
                                           headers=settings.REPO_GITHUB_HEADERS(repo=user_repo),
                                           is_api=True,
                                           timeout=30)
        if res is None:
            return None, "连接仓库失败"
        elif res.status_code != 200:
            return None, f"连接仓库失败：{res.status_code} - " \
                         f"{'超出速率限制，请配置GITHUB_TOKEN环境变量或稍后重试' if res.status_code == 403 else res.reason}"

        try:
            ret = res.json()
            if isinstance(ret, list) and len(ret) > 0 and "message" not in ret[0]:
                return ret, ""
            else:
                return None, "插件在仓库中不存在或返回数据格式不正确"
        except Exception as e:
            logger.error(f"插件数据解析失败：{res.text}，{e}")
            return None, "插件数据解析失败"

    def __download_files(self, pid: str, file_list: List[dict], user_repo: str) -> Tuple[bool, str]:
        """
        下载插件文件
        """
        if not file_list:
            return False, "文件列表为空"

        # 使用栈结构来替代递归调用，避免递归深度过大问题
        stack = [(pid, file_list)]

        while stack:
            current_pid, current_file_list = stack.pop()

            for item in current_file_list:
                if item.get("download_url"):
                    logger.debug(f"正在下载文件：{item.get('path')}")
                    res = self.__request_with_fallback(item.get('download_url'),
                                                       headers=settings.REPO_GITHUB_HEADERS(repo=user_repo))
                    if not res:
                        return False, f"文件 {item.get('path')} 下载失败！"
                    elif res.status_code != 200:
                        return False, f"下载文件 {item.get('path')} 失败：{res.status_code} - " \
                                      f"{'超出速率限制，请配置GITHUB_TOKEN环境变量或稍后重试' if res.status_code == 403 else res.reason}"

                    # 创建插件文件夹并写入文件
                    file_path = Path(settings.ROOT_PATH) / "app" / item.get("path")
                    file_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(file_path, "w", encoding="utf-8") as f:
                        f.write(res.text)
                    logger.debug(f"文件 {item.get('path')} 下载成功，保存路径：{file_path}")
                else:
                    # 将子目录加入栈中以便处理
                    sub_list, msg = self.__get_file_list(f"{current_pid}/{item.get('name')}", user_repo)
                    if not sub_list:
                        return False, msg
                    stack.append((f"{current_pid}/{item.get('name')}", sub_list))

        return True, ""

    def __install_dependencies_if_required(self, pid: str) -> Tuple[bool, str]:
        """
        安装插件依赖（如果有requirements.txt）
        """
        plugin_dir = Path(settings.ROOT_PATH) / "app" / "plugins" / pid
        requirements_file = plugin_dir / "requirements.txt"
        if requirements_file.exists():
            return self.__pip_install_with_fallback(requirements_file)
        return True, ""

    @staticmethod
    def __remove_old_plugin(pid: str):
        """
        删除旧插件
        """
        plugin_dir = Path(settings.ROOT_PATH) / "app" / "plugins" / pid
        if plugin_dir.exists():
            shutil.rmtree(plugin_dir, ignore_errors=True)

    @staticmethod
    def __pip_install_with_fallback(requirements_file: Path) -> Tuple[bool, str]:
        """
        使用自动降级策略 PIP 安装依赖，优先级依次为镜像站、代理、直连
        :param requirements_file: 依赖的 requirements.txt 文件路径
        :return: 依赖安装成功返回 (True, "")，失败返回 (False, 错误信息)
        """
        # 构建三种不同策略下的 PIP 命令
        pip_commands = [
            ["pip", "install", "-r", str(requirements_file), "-i", settings.PIP_PROXY] if settings.PIP_PROXY else None,
            # 使用镜像站
            ["pip", "install", "-r", str(requirements_file), "--proxy",
             settings.PROXY_HOST] if settings.PROXY_HOST else None,  # 使用代理
            ["pip", "install", "-r", str(requirements_file)]  # 直连
        ]

        # 过滤掉 None 的命令
        pip_commands = [cmd for cmd in pip_commands if cmd is not None]

        for pip_command in pip_commands:
            try:
                logger.info(f"尝试使用PIP安装依赖，命令：{' '.join(pip_command)}")
                # 使用 subprocess.run 捕获标准输出和标准错误
                result = subprocess.run(pip_command, check=True, text=True,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                logger.info(f"依赖安装成功，输出：{result.stdout}")
                return True, result.stdout
            except subprocess.CalledProcessError as e:
                error_message = f"命令：{' '.join(pip_command)}，执行失败，错误信息：{e.stderr.strip()}"
                logger.error(error_message)
                return False, error_message
            except Exception as e:
                error_message = f"未知错误，命令：{' '.join(pip_command)}，错误：{str(e)}"
                logger.error(error_message)
                return False, error_message

        return False, "所有依赖安装方式均失败，请检查网络连接或 PIP 配置"

    @staticmethod
    def __request_with_fallback(url: str,
                                headers: Optional[dict] = None,
                                timeout: int = 60,
                                is_api: bool = False) -> Optional[Any]:
        """
        使用自动降级策略请求资源，优先级依次为镜像站、代理、直连
        :param url: 目标URL
        :param headers: 请求头信息
        :param timeout: 请求超时时间
        :param is_api: 是否为GitHub API请求，API请求不走镜像站
        :return: 请求成功则返回Response，失败返回None
        """
        # 镜像站一般不支持API请求，因此API请求直接跳过镜像站
        if not is_api and settings.GITHUB_PROXY:
            proxy_url = f"{UrlUtils.standardize_base_url(settings.GITHUB_PROXY)}{url}"
            try:
                res = RequestUtils(headers=headers, timeout=timeout).get_res(url=proxy_url,
                                                                             raise_exception=True)
                return res
            except Exception as e:
                logger.error(f"使用镜像站 {settings.GITHUB_PROXY} 访问 {url} 失败: {str(e)}")

        # 使用代理
        if settings.PROXY_HOST:
            proxies = {"http": settings.PROXY_HOST, "https": settings.PROXY_HOST}
            try:
                res = RequestUtils(headers=headers, proxies=proxies, timeout=timeout).get_res(url=url,
                                                                                              raise_exception=True)
                return res
            except Exception as e:
                logger.error(f"使用代理 {settings.PROXY_HOST} 访问 {url} 失败: {str(e)}")

        # 最后尝试直连
        try:
            res = RequestUtils(headers=headers, timeout=timeout).get_res(url=url,
                                                                         raise_exception=True)
            return res
        except Exception as e:
            logger.error(f"直连访问 {url} 失败: {str(e)}")

        return None
