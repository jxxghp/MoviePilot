import json
import shutil
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Set

from packaging.specifiers import SpecifierSet, InvalidSpecifier
from packaging.version import Version, InvalidVersion
from pkg_resources import Requirement, working_set

from app.core.cache import cached
from app.core.config import settings
from app.db.systemconfig_oper import SystemConfigOper
from app.log import logger
from app.schemas.types import SystemConfigKey
from app.utils.http import RequestUtils
from app.utils.singleton import Singleton
from app.utils.system import SystemUtils
from app.utils.url import UrlUtils

PLUGIN_DIR = Path(settings.ROOT_PATH) / "app" / "plugins"


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

    @cached(maxsize=1000, ttl=1800)
    def get_plugins(self, repo_url: str, package_version: Optional[str] = None) -> Optional[Dict[str, dict]]:
        """
        获取Github所有最新插件列表
        :param repo_url: Github仓库地址
        :param package_version: 首选插件版本 (如 "v2", "v3")，如果不指定则获取 v1 版本
        """
        if not repo_url:
            return None

        user, repo = self.get_repo_info(repo_url)
        if not user or not repo:
            return None

        raw_url = self._base_url.format(user=user, repo=repo)
        package_url = f"{raw_url}package.{package_version}.json" if package_version else f"{raw_url}package.json"

        res = self.__request_with_fallback(package_url, headers=settings.REPO_GITHUB_HEADERS(repo=f"{user}/{repo}"))
        if res is None:
            return None
        if res:
            try:
                return json.loads(res.text)
            except json.JSONDecodeError:
                logger.error(f"插件包数据解析失败：{res.text}")
                return None
        return {}

    def get_plugin_package_version(self, pid: str, repo_url: str, package_version: Optional[str] = None) -> Optional[str]:
        """
        检查并获取指定插件的可用版本，支持多版本优先级加载和版本兼容性检测
        1. 如果未指定版本，则使用系统配置的默认版本（通过 settings.VERSION_FLAG 设置）
        2. 优先检查指定版本的插件（如 `package.v2.json`）
        3. 如果插件不存在于指定版本，检查 `package.json` 文件，查看该插件是否兼容指定版本
        4. 如果插件不存在或不兼容指定版本，返回 `None`
        :param pid: 插件 ID，用于在插件列表中查找
        :param repo_url: 插件仓库的 URL，指定用于获取插件信息的 GitHub 仓库地址
        :param package_version: 首选插件版本 (如 "v2", "v3")，如不指定则默认使用系统配置的版本
        :return: 返回可用的插件版本号 (如 "v2"，如果指定版本不可用则返回空字符串表示 v1)，如果插件不可用则返回 None
        """
        # 如果没有指定版本，则使用当前系统配置的版本（如 "v2"）
        if not package_version:
            package_version = settings.VERSION_FLAG

        # 优先检查指定版本的插件，即 package.v(x).json 文件中是否存在该插件，如果存在，返回该版本号
        plugins = self.get_plugins(repo_url, package_version)
        if pid in plugins:
            return package_version

        # 如果指定版本的插件不存在，检查全局 package.json 文件，查看插件是否兼容指定的版本
        global_plugins = self.get_plugins(repo_url)
        plugin = global_plugins.get(pid, None)

        # 检查插件是否明确支持当前指定的版本（如 v2 或 v3），如果支持，返回空字符串表示使用 package.json（v1）
        if plugin and plugin.get(package_version) is True:
            return ""

        # 如果所有版本都不存在或插件不兼容，返回 None，表示插件不可用
        return None

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

    @cached(maxsize=1, ttl=1800)
    def get_statistic(self) -> Dict:
        """
        获取插件安装统计
        """
        if not settings.PLUGIN_STATISTIC_SHARE:
            return {}
        res = RequestUtils(proxies=settings.PROXY, timeout=10).get_res(self._install_statistic)
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
        res = RequestUtils(proxies=settings.PROXY, timeout=5).get_res(install_reg_url)
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
        res = RequestUtils(proxies=settings.PROXY,
                           content_type="application/json",
                           timeout=5).post(self._install_report,
                                           json={"plugins": [{"plugin_id": plugin} for plugin in plugins]})
        return True if res else False

    def install(self, pid: str, repo_url: str, package_version: Optional[str] = None, force_install: bool = False) \
            -> Tuple[bool, str]:
        """
        安装插件，包括依赖安装和文件下载，相关资源支持自动降级策略
        1. 检查并获取插件的指定版本，确认版本兼容性
        2. 从 GitHub 获取文件列表（包括 requirements.txt）
        3. 删除旧的插件目录（如非强制安装则进行备份）
        4. 下载并预安装 requirements.txt 中的依赖（如果存在）
        5. 下载并安装插件的其他文件
        6. 再次尝试安装依赖（确保安装完整）
        :param pid: 插件 ID
        :param repo_url: 插件仓库地址
        :param package_version: 首选插件版本 (如 "v2", "v3")，如不指定则默认使用系统配置的版本
        :param force_install: 是否强制安装插件，默认不启用，启用时不进行备份和恢复操作
        :return: (是否成功, 错误信息)
        """
        if SystemUtils.is_frozen():
            return False, "可执行文件模式下，只能安装本地插件"

        # 验证参数
        if not pid or not repo_url:
            return False, "参数错误"

        # 从 GitHub 的 repo_url 获取用户和项目名
        user, repo = self.get_repo_info(repo_url)
        if not user or not repo:
            return False, "不支持的插件仓库地址格式"

        user_repo = f"{user}/{repo}"

        if not package_version:
            package_version = settings.VERSION_FLAG

        # 1. 优先检查指定版本的插件
        package_version = self.get_plugin_package_version(pid, repo_url, package_version)
        # 如果 package_version 为None，说明没有找到匹配的插件
        if package_version is None:
            msg = f"{pid} 没有找到适用于当前版本的插件"
            logger.debug(msg)
            return False, msg
        # package_version 为空，表示从 package.json 中找到插件
        elif package_version == "":
            logger.debug(f"{pid} 从 package.json 中找到适用于当前版本的插件")
        else:
            logger.debug(f"{pid} 从 package.{package_version}.json 中找到适用于当前版本的插件")

        # 2. 获取插件文件列表（包括 requirements.txt）
        file_list, msg = self.__get_file_list(pid.lower(), user_repo, package_version)
        if not file_list:
            return False, msg

        # 3. 删除旧的插件目录，如果不强制安装则备份
        backup_dir = None
        if not force_install:
            backup_dir = self.__backup_plugin(pid.lower())

        self.__remove_old_plugin(pid.lower())

        # 4. 查找并安装 requirements.txt 中的依赖，确保插件环境的依赖尽可能完整。依赖安装可能失败且不影响插件安装，目前只记录日志
        requirements_file_info = next((f for f in file_list if f.get("name") == "requirements.txt"), None)
        if requirements_file_info:
            logger.debug(f"{pid} 发现 requirements.txt，提前下载并预安装依赖")
            success, message = self.__download_and_install_requirements(requirements_file_info,
                                                                        pid, user_repo)
            if not success:
                logger.debug(f"{pid} 依赖预安装失败：{message}")
            else:
                logger.debug(f"{pid} 依赖预安装成功")

        # 5. 下载插件的其他文件
        logger.info(f"{pid} 准备开始下载插件文件")
        success, message = self.__download_files(pid.lower(), file_list, user_repo, package_version, True)
        if not success:
            logger.error(f"{pid} 下载插件文件失败：{message}")
            if backup_dir:
                self.__restore_plugin(pid.lower(), backup_dir)
                logger.warning(f"{pid} 插件安装失败，已还原备份插件")
            else:
                self.__remove_old_plugin(pid.lower())
                logger.warning(f"{pid} 已清理对应插件目录，请尝试重新安装")

            return False, message
        else:
            logger.info(f"{pid} 下载插件文件成功")

        # 6. 插件文件安装成功后，再次尝试安装依赖，避免因为遗漏依赖导致的插件运行问题，目前依旧只记录日志
        dependencies_exist, success, message = self.__install_dependencies_if_required(pid)
        if dependencies_exist:
            if not success:
                logger.error(f"{pid} 依赖安装失败：{message}")
                if backup_dir:
                    self.__restore_plugin(pid.lower(), backup_dir)
                    logger.warning(f"{pid} 插件安装失败，已还原备份插件")
                else:
                    self.__remove_old_plugin(pid.lower())
                    logger.warning(f"{pid} 已清理对应插件目录，请尝试重新安装")
            else:
                logger.info(f"{pid} 依赖安装成功")

        # 插件安装成功后，统计安装信息
        self.install_reg(pid)
        return True, ""

    def __get_file_list(self, pid: str, user_repo: str, package_version: Optional[str] = None) -> \
            Tuple[Optional[list], Optional[str]]:
        """
        获取插件的文件列表
        :param pid: 插件 ID
        :param user_repo: GitHub 仓库的 user/repo 路径
        :return: (文件列表, 错误信息)
        """
        file_api = f"https://api.github.com/repos/{user_repo}/contents/plugins"
        # 如果 package_version 存在（如 "v2"），则加上版本号
        if package_version:
            file_api += f".{package_version}"
        file_api += f"/{pid}"

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

    def __download_files(self, pid: str, file_list: List[dict], user_repo: str,
                         package_version: Optional[str] = None, skip_requirements: bool = False) -> Tuple[bool, str]:
        """
        下载插件文件
        :param pid: 插件 ID
        :param file_list: 要下载的文件列表，包含文件的元数据（包括下载链接）
        :param user_repo: GitHub 仓库的 user/repo 路径
        :param skip_requirements: 是否跳过 requirements.txt 文件的下载
        :return: (是否成功, 错误信息)
        """
        if not file_list:
            return False, "文件列表为空"

        # 使用栈结构来替代递归调用，避免递归深度过大问题
        stack = [(pid, file_list)]

        while stack:
            current_pid, current_file_list = stack.pop()

            for item in current_file_list:
                # 跳过 requirements.txt 的下载
                if skip_requirements and item.get("name") == "requirements.txt":
                    continue

                if item.get("download_url"):
                    logger.debug(f"正在下载文件：{item.get('path')}")
                    res = self.__request_with_fallback(item.get('download_url'),
                                                       headers=settings.REPO_GITHUB_HEADERS(repo=user_repo))
                    if not res:
                        return False, f"文件 {item.get('path')} 下载失败！"
                    elif res.status_code != 200:
                        return False, f"下载文件 {item.get('path')} 失败：{res.status_code}"

                    # 确保文件路径不包含版本号（如 v2、v3），如果有 package_version，移除路径中的版本号
                    relative_path = item.get("path")
                    if package_version:
                        relative_path = relative_path.replace(f"plugins.{package_version}", "plugins", 1)

                    # 创建插件文件夹并写入文件
                    file_path = Path(settings.ROOT_PATH) / "app" / relative_path
                    file_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(file_path, "w", encoding="utf-8") as f:
                        f.write(res.text)
                    logger.debug(f"文件 {item.get('path')} 下载成功，保存路径：{file_path}")
                else:
                    # 如果是子目录，则将子目录内容加入栈中继续处理
                    sub_list, msg = self.__get_file_list(f"{current_pid}/{item.get('name')}", user_repo,
                                                         package_version)
                    if not sub_list:
                        return False, msg
                    stack.append((f"{current_pid}/{item.get('name')}", sub_list))

        return True, ""

    def __download_and_install_requirements(self, requirements_file_info: dict, pid: str, user_repo: str) \
            -> Tuple[bool, str]:
        """
        下载并安装 requirements.txt 文件中的依赖
        :param requirements_file_info: requirements.txt 文件的元数据信息
        :param pid: 插件 ID
        :param user_repo: GitHub 仓库的 user/repo 路径
        :return: (是否成功, 错误信息)
        """
        # 下载 requirements.txt
        res = self.__request_with_fallback(requirements_file_info.get("download_url"),
                                           headers=settings.REPO_GITHUB_HEADERS(repo=user_repo))
        if not res:
            return False, "requirements.txt 文件下载失败"
        elif res.status_code != 200:
            return False, f"下载 requirements.txt 文件失败：{res.status_code}"

        requirements_txt = res.text
        if requirements_txt.strip():
            # 保存并安装依赖
            requirements_file_path = PLUGIN_DIR / pid.lower() / "requirements.txt"
            requirements_file_path.parent.mkdir(parents=True, exist_ok=True)
            with open(requirements_file_path, "w", encoding="utf-8") as f:
                f.write(requirements_txt)

            success, message = self.__pip_install_with_fallback(requirements_file_path)
            return success, message

        return True, ""  # 如果 requirements.txt 为空，视作成功

    def __install_dependencies_if_required(self, pid: str) -> Tuple[bool, bool, str]:
        """
        安装插件依赖。
        :param pid: 插件 ID
        :return: (是否存在依赖，安装是否成功, 错误信息)
        """
        # 定位插件目录和依赖文件
        plugin_dir = PLUGIN_DIR / pid.lower()
        requirements_file = plugin_dir / "requirements.txt"

        # 检查是否存在 requirements.txt 文件
        if requirements_file.exists():
            logger.info(f"{pid} 存在依赖，开始尝试安装依赖")
            success, error_message = self.__pip_install_with_fallback(requirements_file)
            if success:
                return True, True, ""
            else:
                return True, False, error_message

        return False, False, "不存在依赖"

    @staticmethod
    def __backup_plugin(pid: str) -> str:
        """
        备份旧插件目录
        :param pid: 插件 ID
        :return: 备份目录路径
        """
        plugin_dir = PLUGIN_DIR / pid
        backup_dir = Path(settings.TEMP_PATH) / "plugin_backup" / pid

        if plugin_dir.exists():
            # 备份时清理已有的备份目录，防止残留文件影响
            if backup_dir.exists():
                shutil.rmtree(backup_dir, ignore_errors=True)
                logger.debug(f"{pid} 旧的备份目录已清理 {backup_dir}")

            shutil.copytree(plugin_dir, backup_dir, dirs_exist_ok=True)
            logger.debug(f"{pid} 插件已备份到 {backup_dir}")

        return str(backup_dir) if backup_dir.exists() else None

    @staticmethod
    def __restore_plugin(pid: str, backup_dir: str):
        """
        还原旧插件目录
        :param pid: 插件 ID
        :param backup_dir: 备份目录路径
        """
        plugin_dir = PLUGIN_DIR / pid
        if plugin_dir.exists():
            shutil.rmtree(plugin_dir, ignore_errors=True)
            logger.debug(f"{pid} 已清理插件目录 {plugin_dir}")

        if Path(backup_dir).exists():
            shutil.copytree(backup_dir, plugin_dir, dirs_exist_ok=True)
            logger.debug(f"{pid} 已还原插件目录 {plugin_dir}")
            shutil.rmtree(backup_dir, ignore_errors=True)
            logger.debug(f"{pid} 已删除备份目录 {backup_dir}")

    @staticmethod
    def __remove_old_plugin(pid: str):
        """
        删除旧插件
        :param pid: 插件 ID
        """
        plugin_dir = PLUGIN_DIR / pid
        if plugin_dir.exists():
            shutil.rmtree(plugin_dir, ignore_errors=True)

    @staticmethod
    def __pip_install_with_fallback(requirements_file: Path) -> Tuple[bool, str]:
        """
        使用自动降级策略，PIP 安装依赖，优先级依次为镜像站、代理、直连
        :param requirements_file: 依赖的 requirements.txt 文件路径
        :return: (是否成功, 错误信息)
        """
        strategies = []

        # 添加策略到列表中
        if settings.PIP_PROXY:
            strategies.append(("镜像站", ["pip", "install", "-r", str(requirements_file), "-i", settings.PIP_PROXY]))
        if settings.PROXY_HOST:
            strategies.append(
                ("代理", ["pip", "install", "-r", str(requirements_file), "--proxy", settings.PROXY_HOST]))
        strategies.append(("直连", ["pip", "install", "-r", str(requirements_file)]))

        # 遍历策略进行安装
        for strategy_name, pip_command in strategies:
            logger.debug(f"[PIP] 尝试使用策略：{strategy_name} 安装依赖，命令：{' '.join(pip_command)}")
            success, message = SystemUtils.execute_with_subprocess(pip_command)
            if success:
                logger.debug(f"[PIP] 策略：{strategy_name} 安装依赖成功，输出：{message}")
                return True, message
            else:
                logger.error(f"[PIP] 策略：{strategy_name} 安装依赖失败，错误信息：{message}")

        return False, "[PIP] 所有策略均安装依赖失败，请检查网络连接或 PIP 配置"

    @staticmethod
    def __request_with_fallback(url: str,
                                headers: Optional[dict] = None,
                                timeout: Optional[int] = 60,
                                is_api: bool = False) -> Optional[Any]:
        """
        使用自动降级策略，请求资源，优先级依次为镜像站、代理、直连
        :param url: 目标URL
        :param headers: 请求头信息
        :param timeout: 请求超时时间
        :param is_api: 是否为GitHub API请求，API请求不走镜像站
        :return: 请求成功则返回 Response，失败返回 None
        """
        strategies = []

        # 1. 尝试使用镜像站，镜像站一般不支持API请求，因此API请求直接跳过镜像站
        if not is_api and settings.GITHUB_PROXY:
            proxy_url = f"{UrlUtils.standardize_base_url(settings.GITHUB_PROXY)}{url}"
            strategies.append(("镜像站", proxy_url, {"headers": headers, "timeout": timeout}))

        # 2. 尝试使用代理
        if settings.PROXY_HOST:
            strategies.append(("代理", url, {"headers": headers, "proxies": settings.PROXY, "timeout": timeout}))

        # 3. 最后尝试直连
        strategies.append(("直连", url, {"headers": headers, "timeout": timeout}))

        # 遍历策略并尝试请求
        for strategy_name, target_url, request_params in strategies:
            logger.debug(f"[GitHub] 尝试使用策略：{strategy_name} 请求 URL：{target_url}")

            try:
                res = RequestUtils(**request_params).get_res(url=target_url, raise_exception=True)
                logger.debug(f"[GitHub] 请求成功，策略：{strategy_name}, URL: {target_url}")
                return res
            except Exception as e:
                logger.error(f"[GitHub] 请求失败，策略：{strategy_name}, URL: {target_url}，错误：{str(e)}")

        logger.error(f"[GitHub] 所有策略均请求失败，URL: {url}，请检查网络连接或 GitHub 配置")
        return None

    def find_missing_dependencies(self) -> List[str]:
        """
        收集所有需要安装或更新的依赖项
        1. 收集所有插件的依赖项，合并版本约束
        2. 获取已安装的包及其版本
        3. 比较已安装的包与所需的依赖项，找出需要安装或升级的包
        :return: 需要安装或更新的依赖项列表，例如 ["package1>=1.0.0", "package2"]
        """
        try:
            # 收集所有插件的依赖项
            plugin_dependencies = self.__find_plugin_dependencies()  # 返回格式为 {package_name: version_specifier}
            # 获取已安装的包及其版本
            installed_packages = self.__get_installed_packages()  # 返回格式为 {package_name: Version}
            # 需要安装或更新的依赖项列表
            dependencies_to_install = []
            for pkg_name, version_specifier in plugin_dependencies.items():
                spec_set = SpecifierSet(version_specifier)
                installed_version = installed_packages.get(pkg_name)
                if installed_version is None:
                    # 包未安装，需要安装
                    if version_specifier:
                        dependencies_to_install.append(f"{pkg_name}{version_specifier}")
                    else:
                        dependencies_to_install.append(pkg_name)
                elif not spec_set.contains(installed_version, prereleases=True):
                    # 已安装的版本不满足版本约束，需要升级或降级
                    if version_specifier:
                        dependencies_to_install.append(f"{pkg_name}{version_specifier}")
                    else:
                        dependencies_to_install.append(pkg_name)
                # 已安装的版本满足要求，无需操作
            return dependencies_to_install
        except Exception as e:
            logger.error(f"收集所有需要安装或更新的依赖项时发生错误：{e}")
            return []

    def install_dependencies(self, dependencies: List[str]) -> Tuple[bool, str]:
        """
        安装指定的依赖项列表

        :param dependencies: 需要安装或更新的依赖项列表
        :return: (success, message)
        """
        if not dependencies:
            return False, "没有传入需要安装的依赖项"

        try:
            logger.debug(f"需要安装或更新的依赖项：{dependencies}")
            # 创建临时的 requirements.txt 文件用于批量安装
            requirements_temp_file = Path(settings.TEMP_PATH) / "plugin_dependencies" / "requirements.txt"
            requirements_temp_file.parent.mkdir(parents=True, exist_ok=True)
            with open(requirements_temp_file, "w", encoding="utf-8") as f:
                for dep in dependencies:
                    f.write(dep + "\n")

            # 使用自动降级策略安装依赖
            success, message = self.__pip_install_with_fallback(requirements_temp_file)
            # 删除临时文件
            requirements_temp_file.unlink()
            return success, message
        except Exception as e:
            logger.error(f"安装依赖项时发生错误：{e}")
            return False, f"安装依赖项时发生错误：{e}"

    def __get_installed_packages(self) -> Dict[str, Version]:
        """
        获取已安装的包及其版本
        使用 pkg_resources 获取当前环境中已安装的包，标准化包名并转换版本信息
        对于无法解析的版本，记录警告日志并跳过
        :return: 已安装包的字典，格式为 {package_name: Version}
        """
        installed_packages = {}
        try:
            for dist in working_set:
                pkg_name = self.__standardize_pkg_name(dist.project_name)
                try:
                    installed_packages[pkg_name] = Version(dist.version)
                except InvalidVersion:
                    logger.debug(f"无法解析已安装包 '{pkg_name}' 的版本：{dist.version}")
                    continue
            return installed_packages
        except Exception as e:
            logger.error(f"获取已安装的包时发生错误：{e}")
            return {}

    def __find_plugin_dependencies(self) -> Dict[str, str]:
        """
        收集所有插件的依赖项
        遍历 plugins 目录下的所有插件，查找存在 requirements.txt 的插件目录
        ，并解析其中的依赖项，同时将所有插件的依赖项合并到字典中，方便后续统一处理
        :return: 依赖项字典，格式为 {package_name: set(version_specifiers)}
        """
        dependencies = {}
        try:
            for plugin_dir in PLUGIN_DIR.iterdir():
                if plugin_dir.is_dir():
                    requirements_file = plugin_dir / "requirements.txt"
                    if requirements_file.exists():
                        # 解析当前插件的 requirements.txt，获取依赖项
                        plugin_deps = self.__parse_requirements(requirements_file)
                        for pkg_name, version_specifiers in plugin_deps.items():
                            if pkg_name in dependencies:
                                # 更新已存在的包的版本约束集合
                                dependencies[pkg_name].update(version_specifiers)
                            else:
                                # 添加新的包及其版本约束
                                dependencies[pkg_name] = set(version_specifiers)
            return self.__merge_dependencies(dependencies)
        except Exception as e:
            logger.error(f"收集插件依赖项时发生错误：{e}")
            return {}

    def __parse_requirements(self, requirements_file: Path) -> Dict[str, List[str]]:
        """
        解析 requirements.txt 文件，返回依赖项字典
        使用 packaging 库解析每一行依赖项，提取包名和版本约束
        对于无法解析的行，记录警告日志，便于后续检查
        :param requirements_file: requirements.txt 文件的路径
        :return: 依赖项字典，格式为 {package_name: [version_specifier]}
        """
        dependencies = {}
        try:
            with open(requirements_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#'):
                        # 使用 packaging 库解析依赖项
                        try:
                            req = Requirement(line)
                            pkg_name = self.__standardize_pkg_name(req.name)
                            version_specifier = str(req.specifier)
                            if pkg_name in dependencies:
                                dependencies[pkg_name].append(version_specifier)
                            else:
                                dependencies[pkg_name] = [version_specifier]
                        except Exception as e:
                            logger.debug(f"无法解析依赖项 '{line}'：{e}")
            return dependencies
        except Exception as e:
            logger.error(f"解析 requirements.txt 时发生错误：{e}")
            return {}

    @staticmethod
    def __merge_dependencies(dependencies: Dict[str, Set[str]]) -> Dict[str, str]:
        """
        合并依赖项，选择每个包的最高版本要求
        对于多个插件依赖同一包的情况，合并其版本约束，取交集以满足所有插件的要求
        如果交集为空，表示存在版本冲突，需要根据策略进行处理
        :param dependencies: 依赖项字典，格式为 {package_name: set(version_specifiers)}
        :return: 合并后的依赖项字典，格式为 {package_name: version_specifiers}
        """
        try:
            merged_dependencies = {}
            for pkg_name, version_specifiers in dependencies.items():
                # 合并版本约束
                spec_set = SpecifierSet()
                for specifier in version_specifiers:
                    try:
                        if specifier:
                            spec_set &= SpecifierSet(specifier)
                    except InvalidSpecifier as e:
                        logger.error(f"发生版本约束冲突：{e}")
                # 将合并后的版本约束添加到结果字典
                merged_dependencies[pkg_name] = str(spec_set) if spec_set else ''
            return merged_dependencies
        except Exception as e:
            logger.error(f"合并依赖项时发生错误：{e}")
            return {}

    @staticmethod
    def __standardize_pkg_name(name: str) -> str:
        """
        标准化包名，将包名转换为小写并将连字符替换为下划线

        :param name: 原始包名
        :return: 标准化后的包名
        """
        return name.lower().replace("-", "_") if name else name
