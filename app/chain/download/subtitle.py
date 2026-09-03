"""字幕获取、解压和存储 owner。"""

import re
import shutil
import time
from pathlib import Path
from typing import Any, List, Optional, Tuple, Union, cast

from app.application.classification.reference import (
    append_classification_category_path,
    category_path_below_media_type,
)
from app.application.configuration import get_chain_runtime_config_snapshot
from app.application.directory import DirectoryHelper, validate_download_save_path
from app.application.torrent.download import TorrentHelper
from app.chain.download.contract import _DownloadOwnerBase
from app.chain.download.ports import (
    DownloadArchivePort,
    DownloadResponsePort,
    _close_download_response,
    _download_ports_snapshot,
)
from app.chain.media import MediaChain
from app.chain.storage import StorageChain
from app.domain.context import (
    Context,
    MediaInfo,
    SubtitleInfo,
)
from app.domain.metainfo import MetaInfo
from app.runtime.log import logger
from app.schemas.file import FileItem as _SchemaFileItem
from app.schemas.file import FileURI
from app.schemas.system import TransferDirectoryConf as _SchemaTransferDirectoryConf
from app.schemas.types import (
    MediaSource,
)


def _append_download_classification_path(
    root_path: Path,
    dir_info: _SchemaTransferDirectoryConf,
    media_info: MediaInfo,
) -> Path:
    """按目录开关和稳定分类快照拼装下载子目录。"""
    download_dir = root_path
    type_folder_enabled = bool(
        not dir_info.media_type and dir_info.download_type_folder
    )
    if type_folder_enabled:
        download_dir = download_dir / media_info.type.value
    helper = DirectoryHelper()
    if helper.has_fixed_category(dir_info) or not dir_info.download_category_folder:
        return download_dir
    category_path = helper.resolve_media_category(media_info).path
    if not category_path:
        return download_dir
    category_path = category_path_below_media_type(
        category_path,
        media_info.type,
        type_folder_enabled=type_folder_enabled,
    )
    if not category_path:
        return download_dir
    return append_classification_category_path(download_dir, category_path)


class DownloadSubtitleOwner(_DownloadOwnerBase):
    """字幕获取、解压和存储 owner。"""

    _SUBTITLE_ARCHIVE_FORMATS = {
        ".zip": "zip",
        ".rar": "rar",
    }


    @staticmethod
    def _safe_subtitle_file_name(file_name: str, fallback_name: str) -> str:
        """
        生成安全的字幕文件名。
        """
        file_name = Path(file_name or fallback_name).name
        if not Path(file_name).suffix and Path(fallback_name).suffix:
            file_name = f"{file_name}{Path(fallback_name).suffix}"
        return file_name

    @classmethod
    def _is_subtitle_archive(cls, file_name: str) -> bool:
        """
        判断是否为字幕压缩包。
        """
        return Path(file_name).suffix.lower() in cls._SUBTITLE_ARCHIVE_FORMATS

    @classmethod
    def _subtitle_archive_format(cls, file_name: str) -> Optional[str]:
        """
        获取字幕压缩包格式。
        """
        return cls._SUBTITLE_ARCHIVE_FORMATS.get(Path(file_name).suffix.lower())

    @staticmethod
    def _is_subtitle_file(file_name: str) -> bool:
        """
        判断是否为支持的字幕文件。
        """
        return (
            Path(file_name).suffix.lower()
            in get_chain_runtime_config_snapshot().subtitle_extensions
        )

    @classmethod
    def _get_subtitle_working_dir(
            cls,
            storage_chain: StorageChain,
            storage: str,
            target_path: Path,
    ) -> Tuple[Optional[_SchemaFileItem], str]:
        """
        获取字幕保存目录，返回失败原因供前端展示。
        """
        try:
            working_dir_item = storage_chain.get_folder(storage, target_path)
        except Exception as err:
            message = f"下载目录获取失败，无法保存字幕：{target_path} - {str(err)}"
            logger.error(message)
            return None, message

        if not working_dir_item:
            message = f"下载目录不存在，无法保存字幕：{target_path}"
            logger.error(message)
            return None, message
        return working_dir_item, ""

    @staticmethod
    def _detect_subtitle_fallback_name(subtitle: SubtitleInfo, content: bytes) -> str:
        """
        根据响应内容生成兜底字幕文件名。
        """
        suffix = ".zip" if content.startswith(b"PK") else ".srt"
        return f"{subtitle.title or subtitle.subtitle_id or 'subtitle'}{suffix}"

    @classmethod
    def _resolve_media_download_dir(
            cls,
            media_info: MediaInfo,
            save_path: Optional[str] = None,
    ) -> Tuple[Optional[str], Optional[Path], str]:
        """
        根据媒体信息解析下载目录。
        """
        storage = 'local'
        if save_path is not None:
            try:
                validated_save_path = validate_download_save_path(save_path)
            except ValueError as err:
                logger.warn(str(err))
                return None, None, str(err)
            if re.match(r"^[A-Za-z]:/", validated_save_path):
                target_dir = Path(validated_save_path)
            else:
                file_uri = FileURI.from_uri(validated_save_path)
                storage = file_uri.storage or storage
                if not file_uri.path:
                    return None, None, "下载目录路径为空"
                target_dir = Path(file_uri.path)

            dir_info = DirectoryHelper().get_download_dir_by_save_path(
                media=media_info,
                save_path=validated_save_path,
            )
            if dir_info:
                target_dir = cls._append_download_classification(
                    root_path=target_dir,
                    dir_info=dir_info,
                    media_info=media_info,
                )
            return storage, target_dir, ""

        dir_info = DirectoryHelper().get_dir(media_info, include_unsorted=True)
        storage = (dir_info.storage or storage) if dir_info else storage
        if not dir_info:
            logger.error(f"未找到下载目录：{media_info.type.value} {media_info.title_year}")
            return None, None, "未找到下载目录"

        if not dir_info.download_path:
            return None, None, "下载目录路径为空"
        download_dir = cls._append_download_classification(
            root_path=Path(dir_info.download_path),
            dir_info=dir_info,
            media_info=media_info,
        )
        return storage, download_dir, ""

    @staticmethod
    def _append_download_classification(
            root_path: Path,
            dir_info: _SchemaTransferDirectoryConf,
            media_info: MediaInfo,
    ) -> Path:
        """
        按下载目录配置拼装媒体类型和类别子目录。

        :param root_path: 下载根目录
        :param dir_info: 下载目录配置
        :param media_info: 媒体信息
        :return: 应传给存储或下载器的媒体下载目录
        """
        return _append_download_classification_path(root_path, dir_info, media_info)

    @staticmethod
    def _upload_subtitle_file(
            storage_chain: StorageChain,
            storage: str,
            working_dir_item: _SchemaFileItem,
            subtitle_file: Path,
    ) -> Tuple[Optional[str], str]:
        """
        上传单个字幕文件到目标目录。
        """
        if not working_dir_item.path:
            return None, "字幕工作目录路径为空"
        target_sub_file = Path(working_dir_item.path) / subtitle_file.name
        if storage_chain.get_file_item(storage, target_sub_file):
            logger.info(f"字幕文件已存在：{target_sub_file}")
            return target_sub_file.as_posix(), ""
        logger.info(f"转移字幕 {subtitle_file} 到 {target_sub_file} ...")
        uploaded = storage_chain.upload_file(working_dir_item, subtitle_file)
        if uploaded:
            return uploaded.path, ""
        message = f"保存字幕文件失败：{target_sub_file}"
        logger.error(message)
        return None, message

    @staticmethod
    def _build_subtitle_download_error(response: DownloadResponsePort) -> str:
        """
        从字幕下载响应中提取前端可展示的失败原因。
        """
        status_code = getattr(response, "status_code", None)
        reason = getattr(response, "reason", "") or ""
        message = "下载字幕文件失败"
        if status_code:
            message = f"{message}，状态码：{status_code}"
            if reason:
                message = f"{message} {reason}"
        try:
            response_text = (getattr(response, "text", "") or "").strip()
            response_text = re.sub(r"\s+", " ", response_text)
            if response_text:
                message = f"{message}：{response_text[:200]}"
        except Exception as err:
            logger.debug(f"读取字幕下载失败响应内容失败：{str(err)}")
        return message

    def _save_subtitle_response(
            self,
            subtitle: SubtitleInfo,
            response: DownloadResponsePort,
            storage: str,
            target_dir: Path,
    ) -> Tuple[bool, str, List[str]]:
        """
        保存字幕下载响应到目标目录。
        """
        fallback_name = self._detect_subtitle_fallback_name(subtitle, response.content)
        file_name = subtitle.file_name or TorrentHelper.get_url_filename(response, subtitle.enclosure)
        if not Path(file_name).suffix:
            file_name = fallback_name
        file_name = self._safe_subtitle_file_name(
            file_name=file_name,
            fallback_name=fallback_name,
        )
        if not self._is_subtitle_archive(file_name) and not self._is_subtitle_file(file_name):
            message = f"下载链接不是支持的字幕文件：{file_name}"
            logger.warn(f"{message}，链接：{subtitle.enclosure}")
            return False, message, []

        storage_chain = StorageChain()
        working_dir_item, message = self._get_subtitle_working_dir(
            storage_chain=storage_chain,
            storage=storage,
            target_path=target_dir,
        )
        if not working_dir_item:
            return False, message, []

        saved_files = []
        temp_file = self.runtime_config.temporary_path / file_name
        temp_extract_dir = temp_file.with_name(temp_file.stem)
        try:
            self.runtime_config.temporary_path.mkdir(parents=True, exist_ok=True)
            temp_file.write_bytes(response.content)
            if self._is_subtitle_archive(file_name):
                try:
                    _, archive_port = _download_ports_snapshot()
                    archive_port.unpack(
                        temp_file,
                        temp_extract_dir,
                        archive_format=self._subtitle_archive_format(file_name),
                    )
                except Exception as err:
                    message = f"字幕压缩包解压失败：{str(err)}"
                    logger.error(f"{message}，文件：{temp_file}")
                    return False, message, []
                for sub_file in archive_port.list_files(
                    temp_extract_dir,
                    self.runtime_config.subtitle_extensions,
                ):
                    uploaded_path, message = self._upload_subtitle_file(
                        storage_chain=storage_chain,
                        storage=storage,
                        working_dir_item=working_dir_item,
                        subtitle_file=sub_file,
                    )
                    if uploaded_path:
                        saved_files.append(uploaded_path)
                    elif message:
                        logger.error(message)
            else:
                uploaded_path, message = self._upload_subtitle_file(
                    storage_chain=storage_chain,
                    storage=storage,
                    working_dir_item=working_dir_item,
                    subtitle_file=temp_file,
                )
                if uploaded_path:
                    saved_files.append(uploaded_path)
                elif message:
                    return False, message, []
            if not saved_files:
                message = "未保存任何字幕文件"
                logger.error(message)
                return False, message, []
            return True, "字幕文件保存成功", saved_files
        except Exception as err:
            message = f"保存字幕文件失败：{str(err)}"
            logger.error(message)
            return False, message, []
        finally:
            try:
                if temp_extract_dir.exists():
                    shutil.rmtree(temp_extract_dir)
                if temp_file.exists():
                    temp_file.unlink()
            except Exception as err:
                logger.error(f"删除临时字幕文件失败：{str(err)}")

    def download_subtitle(
            self,
            subtitle: SubtitleInfo,
            media_source: MediaSource,
            media_id: str,
            save_path: Optional[str] = None,
            username: Optional[str] = None,
    ) -> Tuple[bool, str, List[str]]:
        """
        下载字幕文件并保存到媒体对应的下载目录。

        :param subtitle: 字幕搜索结果
        :param media_source: 媒体数据源
        :param media_id: 数据源原生ID
        :param save_path: 保存路径
        :param username: 调用下载的用户名
        :return: 成功状态、提示消息、保存文件列表
        """
        if not subtitle or not subtitle.enclosure:
            return False, "字幕下载链接为空", []

        metainfo = MetaInfo(title=subtitle.title, subtitle=subtitle.description)
        mediainfo = MediaChain().recognize_media(
            meta=metainfo,
            media_source=media_source,
            media_id=media_id,
        )
        if not mediainfo:
            return False, "无法识别媒体信息", []
        supplemented_media = MediaChain().supplement_tmdb_info(mediainfo, metainfo)
        if not isinstance(supplemented_media, MediaInfo):
            return False, "字幕下载仅支持影视媒体", []
        mediainfo = supplemented_media

        storage, target_dir, error_msg = self._resolve_media_download_dir(
            media_info=mediainfo,
            save_path=save_path,
        )
        if not target_dir or not storage:
            return False, error_msg or "未找到下载目录", []

        try:
            http, _ = _download_ports_snapshot()
            response = http.get(
                subtitle.enclosure,
                cookies=subtitle.site_cookie,
                ua=subtitle.site_ua or self.runtime_config.user_agent,
                proxies=self.runtime_config.proxy if subtitle.site_proxy else None,
                raise_exception=True,
            )
        except Exception as err:
            message = f"下载字幕文件失败：{str(err)}"
            logger.error(message)
            return False, message, []
        if response is None:
            return False, "下载字幕文件失败：未收到站点响应", []
        try:
            if response.status_code != 200:
                message = self._build_subtitle_download_error(response)
                logger.error(message)
                return False, message, []

            success, message, saved_files = self._save_subtitle_response(
                subtitle=subtitle,
                response=response,
                storage=storage,
                target_dir=target_dir,
            )
            if not success:
                return False, message, []
        finally:
            _close_download_response(response)

        logger.info(
            f"{mediainfo.title_year} 字幕下载完成：{subtitle.site_name} - {subtitle.title}，用户：{username}"
        )
        return True, "字幕下载成功", saved_files

    def _site_subtitle_links(self, context: Context) -> Optional[List[str]]:
        """
        解析站点详情页的字幕下载链接，模块内部自行区分页面解析与API站点
        """
        return cast(
            Optional[List[str]],
            self.run_module("site_subtitle_links", context=context),
        )

    def _save_site_subtitle_response(
        self,
        *,
        response: DownloadResponsePort,
        sublink: str,
        archive_port: DownloadArchivePort,
        storage_chain: StorageChain,
        storage: str,
        working_dir_item: _SchemaFileItem,
    ) -> None:
        """保存站点详情页返回的单个字幕响应，响应所有权仍归调用方。"""
        if response.status_code != 200:
            logger.error(f"下载字幕文件失败：{sublink}")
            return
        file_name = TorrentHelper.get_url_filename(response, sublink)
        if not file_name:
            logger.warn(f"链接不是字幕文件：{sublink}")
            return
        archive_format = self._SUBTITLE_ARCHIVE_FORMATS.get(
            Path(file_name).suffix.lower()
        )
        if not archive_format:
            if Path(file_name).suffix.lower() not in self.runtime_config.subtitle_extensions:
                logger.warn(f"链接不是支持的字幕文件：{sublink} - {file_name}")
                return
            sub_file = self.runtime_config.temporary_path / file_name
            sub_file.write_bytes(response.content)
            if not working_dir_item.path:
                logger.error("字幕工作目录路径为空")
                return
            target_sub_file = Path(working_dir_item.path) / sub_file.name
            if storage_chain.get_file_item(storage, target_sub_file):
                logger.info(f"字幕文件已存在：{target_sub_file}")
                return
            logger.info(f"转移字幕 {sub_file} 到 {target_sub_file} ...")
            storage_chain.upload_file(working_dir_item, sub_file)
            return

        archive_file = self.runtime_config.temporary_path / file_name
        archive_file.write_bytes(response.content)
        archive_path = archive_file.with_name(archive_file.stem)
        try:
            archive_port.unpack(
                archive_file,
                archive_path,
                archive_format=archive_format,
            )
            for sub_file in archive_port.list_files(
                archive_path,
                self.runtime_config.subtitle_extensions,
            ):
                if not working_dir_item.path:
                    logger.error("字幕工作目录路径为空")
                    return
                target_sub_file = Path(working_dir_item.path) / sub_file.name
                if storage_chain.get_file_item(storage, target_sub_file):
                    logger.info(f"字幕文件已存在：{target_sub_file}")
                    continue
                logger.info(f"转移字幕 {sub_file} 到 {target_sub_file} ...")
                storage_chain.upload_file(working_dir_item, sub_file)
        except Exception as err:
            logger.error(f"字幕压缩包解压失败：{archive_file} - {str(err)}")
        finally:
            try:
                if archive_path.exists():
                    shutil.rmtree(archive_path)
                if archive_file.exists():
                    archive_file.unlink()
            except Exception as err:
                logger.error(f"删除临时文件失败：{str(err)}")

    def download_site_subtitles(
            self,
            context: Context,
            download_dir: Path,
            torrent_content: Optional[Union[str, bytes]] = None,
    ) -> None:
        """
        添加下载任务成功后，从站点下载字幕，保存到下载目录
        :param context:  上下文，包括识别信息、媒体信息、种子信息
        :param download_dir:  下载目录
        :param torrent_content: 种子内容，如果是种子文件，则为文件内容，否则为种子字符串
        """
        if not self.runtime_config.download_subtitle:
            return

        # 没有种子文件不处理
        if not torrent_content:
            return

        # 没有详情页不处理
        torrent = context.torrent_info
        if not torrent.page_url:
            return
        # 字幕下载目录
        logger.info("开始从站点下载字幕：%s" % torrent.page_url)
        # 获取种子信息
        folder_name, _ = cast(Any, TorrentHelper)().get_fileinfo_from_torrent_content(
            torrent_content
        )
        # 文件保存目录，如果是单文件种子，则folder_name是空，此时文件保存目录就是下载目录
        storage_chain = StorageChain()
        # 等待目录存在
        working_dir_item = None
        # split download_dir into storage and path
        fileURI = FileURI.from_uri(download_dir.as_posix())
        storage = fileURI.storage or "local"
        if not fileURI.path:
            logger.error("下载目录路径为空，无法保存字幕")
            return
        download_dir = Path(fileURI.path)
        for _ in range(30):
            found = storage_chain.get_file_item(storage, download_dir / folder_name)
            if found:
                working_dir_item = found
                break
            time.sleep(1)
        # 目录仍然不存在，且有文件夹名，则创建目录
        if not working_dir_item and folder_name:
            parent_dir_item = storage_chain.get_folder(storage, download_dir)
            if parent_dir_item:
                working_dir_item = storage_chain.create_folder(
                    parent_dir_item,
                    folder_name
                )
            else:
                logger.error(f"下载根目录不存在，无法创建字幕文件夹：{download_dir}")
                return
        if not working_dir_item:
            logger.error(f"下载目录不存在，无法保存字幕：{download_dir / folder_name}")
            return
        # 解析字幕下载链接
        sublink_list = self._site_subtitle_links(context)
        if not sublink_list:
            logger.warn(f"{torrent.page_url} 页面未找到字幕下载链接")
            return
        # 下载所有字幕文件
        try:
            http, archive_port = _download_ports_snapshot()
        except RuntimeError as err:
            logger.error(str(err))
            return
        self.runtime_config.temporary_path.mkdir(parents=True, exist_ok=True)
        for sublink in sublink_list:
            logger.info(f"找到字幕下载链接：{sublink}，开始下载...")
            ret = http.get(
                sublink,
                cookies=torrent.site_cookie,
                ua=torrent.site_ua,
                proxies=self.runtime_config.proxy if torrent.site_proxy else None,
            )
            if ret is None:
                logger.error(f"下载字幕文件失败：{sublink}")
                continue
            try:
                self._save_site_subtitle_response(
                    response=ret,
                    sublink=sublink,
                    archive_port=archive_port,
                    storage_chain=storage_chain,
                    storage=storage,
                    working_dir_item=working_dir_item,
                )
            finally:
                _close_download_response(ret)
        logger.info(f"{torrent.page_url} 页面字幕下载完成")
