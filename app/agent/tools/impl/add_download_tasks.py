"""添加下载任务工具"""

import re
from pathlib import Path
from typing import List, Optional, Type, Union

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.chain.download import DownloadChain
from app.chain.media import MediaChain
from app.chain.search import SearchChain
from app.core.config import settings
from app.core.context import Context
from app.core.metainfo import MetaInfo
from app.db.site_oper import SiteOper
from app.helper.directory import DirectoryHelper, validate_download_save_path
from app.log import logger
from app.schemas import FileURI, TorrentInfo
from app.utils.crypto import HashUtils


class AddDownloadTasksInput(BaseModel):
    """添加下载任务工具的输入参数模型"""

    torrent_url: List[str] = Field(
        ...,
        description="One or more torrent_url values. Supports refs from get_search_results (`hash:id`) and magnet links."
    )
    downloader: Optional[str] = Field(None,
                                      description="Name of the downloader to use (optional, uses default if not specified)")
    save_path: Optional[str] = Field(None,
                                     description="Directory path where the downloaded files should be saved. Using `<storage>:<path>` for remote storage. e.g. rclone:/MP, smb:/server/share/Movies. (optional, uses default path if not specified)")
    labels: Optional[str] = Field(None,
                                  description="Comma-separated list of labels/tags to assign to the download (optional, e.g., 'movie,hd,bluray')")


class AddDownloadTasksTool(MoviePilotTool):
    """添加下载任务工具"""

    name: str = "add_download_tasks"
    tags: list[str] = [
        ToolTag.Write,
        ToolTag.Download,
        ToolTag.Resource,
    ]
    description: str = "Add torrent download tasks using refs from get_search_results or magnet links."
    args_schema: Type[BaseModel] = AddDownloadTasksInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据下载参数生成友好的提示消息"""
        torrent_urls = self._normalize_torrent_urls(kwargs.get("torrent_url"))
        downloader = kwargs.get("downloader")

        if torrent_urls:
            if len(torrent_urls) == 1:
                if self._is_torrent_ref(torrent_urls[0]):
                    message = f"添加下载任务: 资源 {torrent_urls[0]}"
                else:
                    message = "添加下载任务: 磁力链接"
            else:
                message = f"批量添加下载任务: 共 {len(torrent_urls)} 个资源"
        else:
            message = "添加下载任务"
        if downloader:
            message += f" [下载器: {downloader}]"

        return message

    @staticmethod
    def _build_torrent_ref(context: Context) -> str:
        """生成用于校验缓存项的短引用"""
        if not context or not context.torrent_info:
            return ""
        return HashUtils.sha1(context.torrent_info.enclosure or "")[:7]

    @staticmethod
    def _is_torrent_ref(torrent_ref: Optional[str]) -> bool:
        """判断是否为内部搜索结果引用"""
        if not torrent_ref:
            return False
        return bool(re.fullmatch(r"[0-9a-f]{7}:\d+", str(torrent_ref).strip()))

    @staticmethod
    def _is_magnet_link_input(torrent_url: Optional[str]) -> bool:
        """判断输入是否为允许直接添加的磁力链接"""
        if not torrent_url:
            return False
        value = str(torrent_url).strip()
        return value.startswith("magnet:")

    @classmethod
    def _resolve_cached_context(cls, torrent_ref: str) -> Optional[Context]:
        """从最近一次搜索缓存中解析种子上下文，仅支持 hash:id 格式"""
        ref = str(torrent_ref).strip()
        if ":" not in ref:
            return None
        try:
            ref_hash, ref_index = ref.split(":", 1)
            index = int(ref_index)
        except (TypeError, ValueError):
            return None

        if index < 1:
            return None

        results = SearchChain().last_search_results() or []
        if index > len(results):
            return None
        context = results[index - 1]
        if not ref_hash or cls._build_torrent_ref(context) != ref_hash:
            return None
        return context

    @classmethod
    async def _async_resolve_cached_context(cls, torrent_ref: str) -> Optional[Context]:
        """异步读取最近搜索缓存，避免在协程里直接访问同步文件缓存。"""
        ref = str(torrent_ref).strip()
        if ":" not in ref:
            return None
        try:
            ref_hash, ref_index = ref.split(":", 1)
            index = int(ref_index)
        except (TypeError, ValueError):
            return None

        if index < 1:
            return None

        results = await SearchChain().async_last_search_results() or []
        if index > len(results):
            return None
        context = results[index - 1]
        if not ref_hash or cls._build_torrent_ref(context) != ref_hash:
            return None
        return context

    @staticmethod
    def _merge_labels_with_system_tag(labels: Optional[str]) -> Optional[str]:
        """合并用户标签与系统默认标签，确保任务可被系统管理"""
        system_tag = (settings.TORRENT_TAG or "").strip()
        user_labels = [item.strip() for item in (labels or "").split(",") if item.strip()]

        if system_tag and system_tag not in user_labels:
            user_labels.append(system_tag)

        return ",".join(user_labels) if user_labels else None

    @staticmethod
    def _format_failed_result(failed_messages: List[str]) -> str:
        """统一格式化失败结果"""
        return ", ".join([message for message in failed_messages if message])

    @staticmethod
    def _build_failure_message(torrent_ref: str, error_msg: Optional[str] = None) -> str:
        """构造失败提示"""
        normalized_error = (error_msg or "").strip()
        prefix = "添加种子任务失败："
        if normalized_error.startswith(prefix):
            normalized_error = normalized_error[len(prefix):].lstrip()
        if AddDownloadTasksTool._is_magnet_link_input(normalized_error):
            normalized_error = ""
        if normalized_error:
            return f"{torrent_ref} {normalized_error}"
        if AddDownloadTasksTool._is_torrent_ref(torrent_ref):
            return torrent_ref
        return ""

    @classmethod
    def _normalize_torrent_urls(cls, torrent_url: Optional[Union[List[str], str]]) -> List[str]:
        """统一规范 torrent_url 输入，保留所有非空值"""
        if torrent_url is None:
            return []

        if isinstance(torrent_url, str):
            candidates = torrent_url.split(",")
        else:
            candidates = torrent_url

        return [str(item).strip() for item in candidates if item and str(item).strip()]

    @staticmethod
    def _resolve_direct_download_dir(save_path: Optional[str]) -> Optional[Path]:
        """解析直接下载使用的目录，优先使用 save_path，其次使用默认下载目录"""
        if save_path is not None:
            return Path(validate_download_save_path(save_path))

        download_dirs = DirectoryHelper().get_download_dirs()
        if not download_dirs:
            return None

        dir_conf = download_dirs[0]
        if not dir_conf.download_path:
            return None

        return Path(FileURI(storage=dir_conf.storage or "local", path=dir_conf.download_path).uri)

    @staticmethod
    def _download_direct_sync(
        torrent_input: str,
        download_dir: Path,
        merged_labels: Optional[str],
        downloader: Optional[str],
    ) -> tuple[Optional[str], Optional[str]]:
        """同步添加磁力下载任务，避免下载器调用阻塞事件循环。"""
        result = DownloadChain().download(
            content=torrent_input,
            download_dir=download_dir,
            cookie=None,
            label=merged_labels,
            downloader=downloader,
        )
        if result:
            _, did, _, error_msg = result
        else:
            did, error_msg = None, "未找到下载器"
        return did, error_msg

    @staticmethod
    def _download_single_sync(
        context: Context,
        downloader: Optional[str],
        save_path: Optional[str],
        merged_labels: Optional[str],
    ) -> tuple[Optional[str], Optional[str]]:
        """同步提交带上下文的下载任务，避免站点下载与下载器调用阻塞事件循环。"""
        if save_path is not None:
            save_path = validate_download_save_path(save_path)
        return DownloadChain().download_single(
            context=context,
            downloader=downloader,
            save_path=save_path,
            label=merged_labels,
            return_detail=True,
        )

    async def run(self, torrent_url: Optional[List[str]] = None,
                  downloader: Optional[str] = None, save_path: Optional[str] = None,
                  labels: Optional[str] = None, **kwargs) -> str:
        """执行添加下载任务。"""
        logger.info(
            f"执行工具: {self.name}, 参数: torrent_url={torrent_url}, downloader={downloader}, save_path={save_path}, labels={labels}")

        try:
            torrent_inputs = self._normalize_torrent_urls(torrent_url)
            if not torrent_inputs:
                return "错误：torrent_url 不能为空。"

            if save_path is not None:
                try:
                    save_path = validate_download_save_path(save_path)
                except ValueError as err:
                    return f"参数错误：save_path {str(err)}"

            merged_labels = self._merge_labels_with_system_tag(labels)
            success_count = 0
            failed_messages = []

            for torrent_input in torrent_inputs:
                if self._is_torrent_ref(torrent_input):
                    cached_context = await self._async_resolve_cached_context(torrent_input)
                    if not cached_context or not cached_context.torrent_info:
                        failed_messages.append(f"{torrent_input} 引用无效，请重新使用 get_search_results 查看搜索结果")
                        continue

                    cached_torrent = cached_context.torrent_info
                    site_name = cached_torrent.site_name
                    torrent_title = cached_torrent.title or torrent_input
                    torrent_description = cached_torrent.description
                    enclosure = cached_torrent.enclosure

                    if not site_name:
                        failed_messages.append(f"{torrent_input} 缺少站点名称")
                        continue

                    siteinfo = await SiteOper().async_get_by_name(site_name)
                    if not siteinfo:
                        failed_messages.append(f"{torrent_input} 未找到站点信息 {site_name}")
                        continue

                    torrent_info = TorrentInfo(
                        title=torrent_title,
                        description=torrent_description,
                        enclosure=enclosure,
                        site_name=site_name,
                        site_ua=siteinfo.ua,
                        site_cookie=siteinfo.cookie,
                        site_proxy=siteinfo.proxy,
                        site_order=siteinfo.pri,
                        site_downloader=siteinfo.downloader
                    )
                    meta_info = MetaInfo(title=torrent_title, subtitle=torrent_description)
                    media_info = cached_context.media_info if cached_context.media_info else None
                    if not media_info:
                        media_info = await MediaChain().async_recognize_by_meta(
                            meta_info,
                            obtain_images=False,
                        )
                    if not media_info:
                        failed_messages.append(f"{torrent_input} 无法识别媒体信息")
                        continue

                    context = Context(
                        torrent_info=torrent_info,
                        meta_info=meta_info,
                        media_info=media_info
                    )
                else:
                    if not self._is_magnet_link_input(torrent_input):
                        failed_messages.append(
                            f"{torrent_input} 不是有效的下载内容，非 hash:id 时仅支持 magnet: 开头"
                        )
                        continue
                    download_dir = await self.run_blocking(
                        "storage", self._resolve_direct_download_dir, save_path
                    )
                    if not download_dir:
                        failed_messages.append(f"{torrent_input} 缺少保存路径，且系统未配置可用下载目录")
                        continue
                    did, error_msg = await self.run_blocking(
                        "downloader",
                        self._download_direct_sync,
                        torrent_input,
                        download_dir,
                        merged_labels,
                        downloader,
                    )
                    if did:
                        success_count += 1
                    else:
                        failed_messages.append(self._build_failure_message(torrent_input, error_msg))
                    continue

                did, error_msg = await self.run_blocking(
                    "downloader",
                    self._download_single_sync,
                    context,
                    downloader,
                    save_path,
                    merged_labels,
                )
                if did:
                    success_count += 1
                else:
                    failed_messages.append(self._build_failure_message(torrent_input, error_msg))

            if success_count and not failed_messages:
                return "任务添加成功"

            if success_count:
                return f"部分任务添加失败：{self._format_failed_result(failed_messages)}"

            return f"任务添加失败：{self._format_failed_result(failed_messages)}"
        except Exception as e:
            logger.error(f"添加下载任务失败: {e}", exc_info=True)
            return f"添加下载任务时发生错误: {str(e)}"
