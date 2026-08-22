import errno
import json
import re
import shutil
import uuid
from pathlib import Path
from typing import Union, Optional

from app.application.orchestration import ChainBase
from app.application.configuration import get_chain_runtime_config_snapshot
from app.runtime.state import SystemHelper
from app.runtime.log import logger
from app.schemas.message import Message
from app.schemas.notification import ChannelRef, channel_identity, resolve_channel
from app.adapters.network.http import RequestUtils
from app.adapters.system.host import SystemUtils
from version import FRONTEND_VERSION, APP_VERSION


class SystemChain(ChainBase):
    """
    系统级处理链
    """

    _restart_file = "__system_restart__"
    _plugin_restore_pending_file = "__plugin_restore_pending__"

    def remote_clear_cache(self, channel: ChannelRef, userid: Union[int, str], source: Optional[str] = None):
        """
        清理系统缓存
        """
        self.clear_cache()
        self.post_message(Message(
            channel=channel,
            source=source,
            title=f"缓存清理完成！",
            userid=userid,
            save_history=False))

    def restart(self, channel: ChannelRef, userid: Union[int, str], source: Optional[str] = None):
        """
        重启系统
        """
        if channel and userid:
            self.post_message(Message(
                channel=channel,
                source=source,
                title="系统正在重启，请耐心等候！",
                userid=userid,
                save_history=False))
            # 保存重启信息
            self.save_cache({
                "channel": channel_identity(channel),
                "userid": userid
            }, self._restart_file)
        # 主动备份一次插件
        self.backup_plugins()
        # 重启
        SystemHelper.restart()

    @staticmethod
    def backup_plugins():
        """
        备份插件到用户配置目录（仅docker环境）
        """

        # 非docker环境不处理
        if not SystemUtils.is_docker():
            return

        try:
            # 使用绝对路径确保准确性
            config = get_chain_runtime_config_snapshot()
            plugins_dir = config.root_path / "app" / "plugins"
            backup_dir = config.config_path / "plugins_backup"

            if not plugins_dir.exists():
                logger.info("插件目录不存在，跳过备份")
                return

            # 确保备份目录存在
            backup_dir.mkdir(parents=True, exist_ok=True)
            pending_file = backup_dir / SystemChain._plugin_restore_pending_file
            pending_items = (
                SystemChain.__read_plugin_restore_pending(pending_file)
                if pending_file.exists()
                else None
            )
            # 需要排除的文件和目录
            exclude_items = {"__init__.py", "__pycache__", ".DS_Store"}

            backup_failed = False

            # 遍历插件目录，备份除排除项外的所有内容
            for item in plugins_dir.iterdir():
                if item.name in exclude_items:
                    continue
                # 失败项目的原快照是下一次恢复的唯一材料，关停备份不能覆盖它。
                if pending_file.exists() and (
                    pending_items is None or item.name in pending_items
                ):
                    logger.debug(f"插件 {item.name} 有待重试恢复标记，保留原快照")
                    continue
                target_path = backup_dir / item.name

                try:
                    SystemChain.__replace_snapshot(
                        item,
                        target_path,
                        ignore=shutil.ignore_patterns(
                            "__pycache__", "*.pyc", ".DS_Store"
                        ) if item.is_dir() else None,
                    )
                    logger.debug(f"已备份插件项目: {item.name}")
                except Exception as e:
                    backup_failed = True
                    logger.error(f"备份插件 {item.name} 失败: {e}")

            if backup_failed:
                logger.warning(f"插件备份部分失败，保留可用旧快照: {backup_dir}")
            else:
                logger.info(f"插件备份完成，备份位置: {backup_dir}")

        except Exception as e:
            logger.error(f"插件备份失败: {str(e)}")

    @staticmethod
    def restore_plugins():
        """
        从备份恢复插件到app/plugins目录，恢复完成后删除备份（仅docker环境）
        """

        # 非docker环境不处理
        if not SystemUtils.is_docker():
            return

        # 使用绝对路径确保准确性
        config = get_chain_runtime_config_snapshot()
        plugins_dir = config.root_path / "app" / "plugins"
        backup_dir = config.config_path / "plugins_backup"

        if not backup_dir.exists():
            logger.info("插件备份目录不存在，跳过恢复")
            return

        pending_file = backup_dir / SystemChain._plugin_restore_pending_file

        # 系统重置或上次恢复未完成时才消费备份。
        system_reset = SystemHelper().is_system_reset()
        should_restore = system_reset or pending_file.exists()
        if not should_restore:
            logger.info("当前不是系统重置，保留插件备份供后续重置使用")
            return

        # 确保插件目录存在
        plugins_dir.mkdir(parents=True, exist_ok=True)

        # 遍历备份目录，恢复所有内容
        restored_count = 0
        restore_failed = False
        failed_items: dict[str, bool] = {}
        pending_items = (
            SystemChain.__read_plugin_restore_pending(pending_file)
            if pending_file.exists() and not system_reset
            else None
        )
        for item in backup_dir.iterdir():
            if (
                item.name == SystemChain._plugin_restore_pending_file
                or SystemChain.__is_snapshot_artifact(item.name)
            ):
                continue
            target_path = plugins_dir / item.name
            if pending_items is not None:
                if item.name not in pending_items:
                    continue
                if not pending_items[item.name] and target_path.exists():
                    logger.info(f"插件 {item.name} 已在恢复失败后重新安装，跳过备份覆盖")
                    continue
            target_existed = target_path.exists()
            try:
                if item.is_dir() or item.is_file():
                    SystemChain.__replace_snapshot(item, target_path)
                    logger.debug(f"已恢复插件文件: {item.name}")
                    restored_count += 1
            except Exception as e:
                restore_failed = True
                failed_items[item.name] = target_existed
                logger.error(f"恢复插件 {item.name} 时发生错误: {str(e)}")
                continue

        logger.info(f"插件恢复完成，共恢复 {restored_count} 个项目")

        if restore_failed:
            if SystemChain.__write_plugin_restore_pending(pending_file, failed_items):
                logger.warning("插件恢复未完成，保留备份并标记为下次启动重试")
            else:
                logger.warning("插件恢复未完成，已保留备份，但无法写入下次启动重试标记")
            return

        # 源码恢复完成后即可消费备份；依赖由启动后的统一后台任务处理。
        try:
            shutil.rmtree(backup_dir)
            logger.info(f"已删除插件备份目录: {backup_dir}")
        except Exception as e:
            logger.warning(f"删除备份目录失败: {str(e)}")
            if backup_dir.exists():
                SystemChain.__write_plugin_restore_pending(pending_file, {})

    @staticmethod
    def __is_snapshot_artifact(name: str) -> bool:
        """识别快照替换过程中生成的临时或旧快照条目。"""
        return bool(re.fullmatch(r"\..+\.(?:tmp|old)-[0-9a-f]{32}", name))

    @staticmethod
    def __read_plugin_restore_pending(pending_file: Path) -> Optional[dict[str, bool]]:
        """读取仍需恢复的插件项目；无效内容按全部项目重试。"""
        try:
            payload = json.loads(pending_file.read_text(encoding="utf-8"))
            failed_items = payload.get("failed_items")
            if not isinstance(failed_items, dict):
                return None
            return {
                str(name): target_existed
                for name, target_existed in failed_items.items()
                if isinstance(name, str) and isinstance(target_existed, bool)
            }
        except (OSError, ValueError, TypeError):
            return None

    @staticmethod
    def __write_plugin_restore_pending(
        pending_file: Path,
        failed_items: dict[str, bool],
    ) -> bool:
        """记录失败项目及其原目标状态，供普通重启继续未完成恢复。"""
        try:
            pending_file.write_text(
                json.dumps({"failed_items": failed_items}, ensure_ascii=False),
                encoding="utf-8",
            )
            return True
        except Exception as e:
            logger.error(f"写入插件恢复重试标记失败: {e}")
            return False

    @staticmethod
    def __replace_snapshot(source: Path, target: Path, *, ignore=None) -> None:
        """复制到同级临时路径后替换目标，避免失败时丢失旧快照。"""
        target.parent.mkdir(parents=True, exist_ok=True)
        suffix = uuid.uuid4().hex
        staging = target.with_name(f".{target.name}.tmp-{suffix}")
        previous = target.with_name(f".{target.name}.old-{suffix}")
        previous_available = False
        published = False
        try:
            if source.is_dir():
                shutil.copytree(source, staging, ignore=ignore)
            else:
                shutil.copy2(source, staging)
            if target.exists():
                try:
                    target.replace(previous)
                except OSError as error:
                    if error.errno != errno.EXDEV:
                        raise
                    # overlayfs 可能拒绝把镜像层目录直接 rename 到可写层，
                    # 先复制旧目标保留恢复材料，再删除旧目录继续发布快照。
                    if target.is_dir():
                        shutil.copytree(target, previous, symlinks=True)
                    else:
                        shutil.copy2(target, previous, follow_symlinks=False)
                    previous_available = True
                    SystemChain.__remove_snapshot_path(target)
                else:
                    previous_available = True
            staging.replace(target)
            published = True
        except Exception:
            if previous_available and not published:
                try:
                    SystemChain.__remove_snapshot_path(target)
                    previous.replace(target)
                    previous_available = False
                except Exception as rollback_error:
                    logger.error(
                        f"恢复旧快照失败，已保留恢复材料 {previous}: "
                        f"{rollback_error}"
                    )
            raise
        finally:
            if staging.is_dir():
                shutil.rmtree(staging, ignore_errors=True)
            elif staging.exists():
                staging.unlink(missing_ok=True)
            if published and previous.exists():
                if previous.is_dir():
                    shutil.rmtree(previous, ignore_errors=True)
                else:
                    previous.unlink(missing_ok=True)

    @staticmethod
    def __remove_snapshot_path(path: Path) -> None:
        """删除待替换目标，保留失败回滚所需的旧快照副本。"""
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        elif path.exists() or path.is_symlink():
            path.unlink()

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

    def version(self, channel: ChannelRef, userid: Union[int, str], source: Optional[str] = None):
        """
        查看当前版本、远程版本
        """
        self.post_message(Message(
            channel=channel,
            source=source,
            title=self.__get_version_message(),
            userid=userid,
            save_history=False))

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
            channel = resolve_channel(restart_channel.get('channel'))
            userid = restart_channel.get('userid')

            # 版本号
            title = self.__get_version_message()
            self.post_message(Message(
                channel=channel,
                title=f"系统已重启完成！\n{title}",
                userid=userid,
                save_history=False))
            self.remove_cache(self._restart_file)

    @staticmethod
    def __get_server_release_version():
        """
        获取后端V2最新版本
        """
        try:
            # 获取所有发布的版本列表
            response = RequestUtils(
                proxies=get_chain_runtime_config_snapshot().proxy,
                headers=get_chain_runtime_config_snapshot().github_headers,
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
                proxies=get_chain_runtime_config_snapshot().proxy,
                headers=get_chain_runtime_config_snapshot().github_headers,
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
            config = get_chain_runtime_config_snapshot()
            version_file = config.config_path.parent / "nginx" / "html" / "version.txt"
        else:
            version_file = get_chain_runtime_config_snapshot().frontend_path / "version.txt"
        if version_file.exists():
            try:
                with open(version_file, 'r', encoding='utf-8', errors='replace') as f:
                    version = str(f.read()).strip()
                return version
            except Exception as err:
                logger.debug(f"加载版本文件 {version_file} 出错：{str(err)}")
        return FRONTEND_VERSION
