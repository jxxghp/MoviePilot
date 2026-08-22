import base64
import copy
import hashlib
import json
import re
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional, Tuple, Set, Dict, Union
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from app.schemas.transfer import DownloaderTorrent as _SchemaDownloaderTorrent
from app.schemas.system import TransferDirectoryConf as _SchemaTransferDirectoryConf
from app.schemas.workflow import FileItem as _SchemaFileItem
from app.application.orchestration import ChainBase
from app.application.orchestration.media import MediaChain
from app.application.orchestration.storage import StorageChain
from app.runtime.cache import FileCache
from app.runtime.config import global_vars
from app.application.configuration import get_chain_runtime_config_snapshot
from app.domain.context import (
    Context,
    MediaInfo,
    MusicInfo,
    SubtitleInfo,
    TorrentInfo,
)
from app.runtime.events import eventmanager, Event
from app.domain.meta.metabase import MetaBase
from app.domain.meta.metamusic import MetaMusic
from app.domain.metainfo import MetaInfo
from app.application.orchestration.data import (
    DownloadFailurePortProxy as DownloadFailureOper,
    DownloadHistoryPortProxy as DownloadHistoryOper,
    MediaServerPortProxy as MediaServerOper,
)
from app.application.directory import DirectoryHelper, validate_download_save_path
from app.application.download.tasks import DownloadTaskService
from app.runtime.thread import ThreadHelper
from app.application.torrent import TorrentHelper
from app.runtime.log import logger
from app.schemas.mediaserver import ExistMediaInfo
from app.schemas.file import FileURI
from app.schemas.mediaserver import NotExistMediaInfo
from app.schemas.transfer import DownloaderTorrent
from app.schemas.message import Message
from app.schemas.event import ResourceSelectionEventData
from app.schemas.event import ResourceDownloadEventData
from app.schemas.notification import channel_identity
from app.schemas.types import MUSIC_ENTITY_ALBUM, MediaSource, MediaType, TorrentStatus, EventType, NotificationChannel, MessageType, ContentType, \
    ChainEventType
from app.adapters.network.http import RequestUtils
from app.schemas.media import build_media_key, resolve_media_identity
from app.domain import episode as episode_rules
from app.foundation import size as size_tools
from app.foundation import text as text_tools
from app.adapters.system.host import SystemUtils

if TYPE_CHECKING:
    from typing import Any

    DownloadFailure = Any


DOWNLOAD_FAILURE_RESOURCE_TTL_SECONDS = 24 * 60 * 60
DOWNLOAD_FAILURE_TRANSIENT_TTL_SECONDS = 60 * 60
DOWNLOAD_FAILURE_RESOURCE_ERROR_KEYWORDS = (
    "无法读取种子文件",
    "下载种子内容为空",
    "无法获取下载地址",
    "种子下载失败",
    "torrent not found",
    "not found",
    "404",
    "deleted",
    "invalid torrent",
    "专辑资源",
)


class DownloadChain(ChainBase):
    """
    下载处理链
    """

    _SUBTITLE_ARCHIVE_FORMATS = {
        ".zip": "zip",
        ".rar": "rar",
    }

    @staticmethod
    def _build_download_note(
            source: Optional[str],
            media: MediaInfo | MusicInfo,
            meta: MetaBase,
    ) -> dict:
        """构造下载历史备注，并为音乐保存可恢复的版本化上下文。"""
        note = {"source": source}
        if getattr(media, "type", None) != MediaType.MUSIC:
            return note
        media_payload = media.to_dict()
        media_payload.pop("raw_data", None)
        note["music"] = {
            "version": 1,
            "meta": meta.to_dict(),
            "media": media_payload,
        }
        return note

    def _after_download_history_commit(
        self,
        *,
        context: Context,
        media: MediaInfo | MusicInfo,
        meta: MetaBase,
        torrent: TorrentInfo,
        channel: NotificationChannel | None,
        source: str | None,
        userid: str | None,
        username: str | None,
        download_episodes: list[int] | None,
        download_dir: Path,
        torrent_content: bytes,
    ) -> None:
        """保持下载历史提交后的通知和后处理顺序。"""
        self.post_message(
            Message(
                channel=channel,
                source=source if channel else None,
                mtype=MessageType.Download,
                ctype=ContentType.DownloadAdded,
                image=media.get_message_image(),
                link=self.runtime_config.downloading_url,
                userid=userid,
                username=username,
            ),
            meta=meta,
            mediainfo=media,
            torrentinfo=torrent,
            download_episodes=download_episodes,
            username=username,
        )
        self._submit_download_added_task(
            context=context,
            download_dir=download_dir,
            torrent_content=torrent_content,
        )

    @staticmethod
    def _validate_music_album_resource(
            context: Context,
            file_list: Optional[List[str]],
    ) -> Optional[str]:
        """校验专辑种子是否包含预期数量的独立音轨，并标记已确认的整专覆盖。"""
        media = context.media_info
        if (
                not media
                or media.type != MediaType.MUSIC
                or getattr(media, "music_type", None) != MUSIC_ENTITY_ALBUM
        ):
            return None

        context.confirmed_full_coverage = False
        try:
            expected_tracks = int(getattr(media, "total_tracks", None) or 0)
        except (TypeError, ValueError):
            expected_tracks = 0
        if expected_tracks <= 0:
            return "专辑资源无法校验：专辑总曲目数未知"
        if not file_list:
            return "专辑资源无法校验：种子未提供文件清单，不能确认整专曲目"

        track_identities = {
            identity
            for file in file_list
            if Path(str(file)).suffix.lower()
            in get_chain_runtime_config_snapshot().audio_extensions
            and (identity := DownloadChain._music_resource_track_identity(file))
        }
        actual_tracks = len(track_identities)
        if actual_tracks < expected_tracks:
            return (
                f"专辑资源不完整：专辑共 {expected_tracks} 首，"
                f"种子仅包含 {actual_tracks} 个独立音频文件"
            )

        context.confirmed_full_coverage = True
        return None

    @staticmethod
    def _music_resource_track_identity(file: str) -> Optional[Tuple[int, Union[int, str]]]:
        """从资源文件路径提取盘号和曲序，缺少曲序时按归一化曲名去重。"""
        file_path = Path(str(file))
        file_meta = MetaMusic(
            org_string=file_path.name,
            title=file_path.stem,
        ).apply_path_context(file_path)
        track_identity: Union[int, str, None] = file_meta.track_number
        if track_identity is None:
            track_identity = text_tools.normalize_upper(file_meta.title or file_path.stem)
        if track_identity in (None, ""):
            return None
        return file_meta.disc_number or 1, track_identity

    @staticmethod
    def _normalize_indirect_download_url(url: str, base_url: Optional[str] = None) -> str:
        """
        将两段式下载结果约束到索引器配置的可信 API 地址。

        :param url: 换票接口返回的临时下载地址
        :param base_url: 索引器配置的可信 API Base URL
        :return: 使用可信 API 来源的临时下载地址
        """
        if not url or not base_url:
            return url
        base_parts = urlparse(base_url)
        if not base_parts.scheme or not base_parts.netloc:
            return url
        url_parts = urlparse(url)
        if not url_parts.netloc:
            return urljoin(f"{base_url.rstrip('/')}/", url)
        return url_parts._replace(
            scheme=base_parts.scheme,
            netloc=base_parts.netloc,
        ).geturl()

    @staticmethod
    def _media_identity_keys(media: Optional[MediaInfo]) -> Set[str]:
        """返回媒体的统一身份键，用于临时缺失集映射匹配。"""
        if not media:
            return set()
        source, media_id = resolve_media_identity(media=media)
        media_key = build_media_key(source, media_id)
        return {media_key} if media_key else set()

    @classmethod
    def _matches_media_identity(cls, media: Optional[MediaInfo], media_key: object) -> bool:
        """判断媒体是否命中来源与原生 ID 组成的统一身份键。"""
        return media_key is not None and str(media_key) in cls._media_identity_keys(media)

    @staticmethod
    def _safe_subtitle_file_name(file_name: str, fallback_name: str) -> str:
        """
        生成安全的字幕文件名。
        """
        file_name = Path(file_name or fallback_name).name
        if not Path(file_name).suffix and Path(fallback_name).suffix:
            file_name = f"{file_name}{Path(fallback_name).suffix}"
        return file_name

    @staticmethod
    def _is_subtitle_archive(file_name: str) -> bool:
        """
        判断是否为字幕压缩包。
        """
        return Path(file_name).suffix.lower() in DownloadChain._SUBTITLE_ARCHIVE_FORMATS

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

    @staticmethod
    def _resolve_media_download_dir(
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
                target_dir = Path(file_uri.path)

            dir_info = DirectoryHelper().get_download_dir_by_save_path(
                media=media_info,
                save_path=validated_save_path,
            )
            if dir_info:
                target_dir = DownloadChain._append_download_classification(
                    root_path=target_dir,
                    dir_info=dir_info,
                    media_info=media_info,
                )
            return storage, target_dir, ""

        dir_info = DirectoryHelper().get_dir(media_info, include_unsorted=True)
        storage = dir_info.storage if dir_info else storage
        if not dir_info:
            logger.error(f"未找到下载目录：{media_info.type.value} {media_info.title_year}")
            return None, None, "未找到下载目录"

        download_dir = DownloadChain._append_download_classification(
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
        download_dir = root_path
        if not dir_info.media_type and dir_info.download_type_folder:
            download_dir = download_dir / media_info.type.value
        if not dir_info.media_category and dir_info.download_category_folder and media_info.category:
            download_dir = download_dir / media_info.category
        return download_dir

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
    def _build_subtitle_download_error(response) -> str:
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
            response,
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
                    SystemUtils.unpack_archive(
                        temp_file,
                        temp_extract_dir,
                        archive_format=self._subtitle_archive_format(file_name),
                    )
                except Exception as err:
                    message = f"字幕压缩包解压失败：{str(err)}"
                    logger.error(f"{message}，文件：{temp_file}")
                    return False, message, []
                for sub_file in SystemUtils.list_files(
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
        mediainfo = MediaChain().supplement_tmdb_info(mediainfo, metainfo)

        storage, target_dir, error_msg = self._resolve_media_download_dir(
            media_info=mediainfo,
            save_path=save_path,
        )
        if not target_dir:
            return False, error_msg or "未找到下载目录", []

        request = RequestUtils(
            cookies=subtitle.site_cookie,
            ua=subtitle.site_ua or self.runtime_config.user_agent,
            proxies=self.runtime_config.proxy if subtitle.site_proxy else None,
        )
        try:
            response = request.get_res(subtitle.enclosure, raise_exception=True)
        except Exception as err:
            message = f"下载字幕文件失败：{str(err)}"
            logger.error(message)
            return False, message, []
        if response is None:
            return False, "下载字幕文件失败：未收到站点响应", []
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

        logger.info(
            f"{mediainfo.title_year} 字幕下载完成：{subtitle.site_name} - {subtitle.title}，用户：{username}"
        )
        return True, "字幕下载成功", saved_files

    def _submit_download_added_task(
            self,
            context: Context,
            download_dir: Path,
            torrent_content: Union[str, bytes],
    ) -> None:
        """
        后台执行下载成功后的附加处理，避免站点字幕下载阻塞添加下载响应。
        """

        def _run_download_added() -> None:
            try:
                self.download_added(
                    context=context,
                    download_dir=download_dir,
                    torrent_content=torrent_content,
                )
                self.download_site_subtitles(
                    context=context,
                    download_dir=download_dir,
                    torrent_content=torrent_content,
                )
            except Exception as e:
                logger.error(f"执行下载成功后处理失败：{str(e)}")

        try:
            ThreadHelper().submit(_run_download_added)
        except Exception as err:
            logger.error(f"提交下载成功后处理后台任务失败：{str(err)}")

    # 字幕压缩包扩展名与解压格式映射
    _SUBTITLE_ARCHIVE_FORMATS = {
        ".zip": "zip",
        ".rar": "rar",
    }

    def _site_subtitle_links(self, context: Context) -> Optional[List[str]]:
        """
        解析站点详情页的字幕下载链接，模块内部自行区分页面解析与API站点
        """
        return self.unicast("site_subtitle_links", context=context)

    def download_site_subtitles(
            self,
            context: Context,
            download_dir: Path,
            torrent_content: Union[str, bytes] = None,
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
        folder_name, _ = TorrentHelper().get_fileinfo_from_torrent_content(torrent_content)
        # 文件保存目录，如果是单文件种子，则folder_name是空，此时文件保存目录就是下载目录
        storage_chain = StorageChain()
        # 等待目录存在
        working_dir_item = None
        # split download_dir into storage and path
        fileURI = FileURI.from_uri(download_dir.as_posix())
        storage = fileURI.storage
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
        request = RequestUtils(
            cookies=torrent.site_cookie,
            ua=torrent.site_ua,
            proxies=self.runtime_config.proxy if torrent.site_proxy else None,
        )
        self.runtime_config.temporary_path.mkdir(parents=True, exist_ok=True)
        for sublink in sublink_list:
            logger.info(f"找到字幕下载链接：{sublink}，开始下载...")
            # 下载
            ret = request.get_res(sublink)
            if ret and ret.status_code == 200:
                file_name = TorrentHelper.get_url_filename(ret, sublink)
                if not file_name:
                    logger.warn(f"链接不是字幕文件：{sublink}")
                    continue
                archive_format = self._SUBTITLE_ARCHIVE_FORMATS.get(Path(file_name).suffix.lower())
                if archive_format:
                    archive_file = self.runtime_config.temporary_path / file_name
                    # 保存
                    archive_file.write_bytes(ret.content)
                    # 解压路径
                    archive_path = archive_file.with_name(archive_file.stem)
                    try:
                        # 解压文件
                        SystemUtils.unpack_archive(
                            archive_file,
                            archive_path,
                            archive_format=archive_format,
                        )
                        # 遍历转移文件
                        for sub_file in SystemUtils.list_files(
                            archive_path,
                            self.runtime_config.subtitle_extensions,
                        ):
                            target_sub_file = Path(working_dir_item.path) / Path(sub_file.name)
                            if storage_chain.get_file_item(storage, target_sub_file):
                                logger.info(f"字幕文件已存在：{target_sub_file}")
                                continue
                            logger.info(f"转移字幕 {sub_file} 到 {target_sub_file} ...")
                            storage_chain.upload_file(working_dir_item, sub_file)
                    except Exception as err:
                        logger.error(f"字幕压缩包解压失败：{archive_file} - {str(err)}")
                    # 删除临时文件
                    try:
                        if archive_path.exists():
                            shutil.rmtree(archive_path)
                        if archive_file.exists():
                            archive_file.unlink()
                    except Exception as err:
                        logger.error(f"删除临时文件失败：{str(err)}")
                else:
                    if (
                        Path(file_name).suffix.lower()
                        not in self.runtime_config.subtitle_extensions
                    ):
                        logger.warn(f"链接不是支持的字幕文件：{sublink} - {file_name}")
                        continue
                    sub_file = self.runtime_config.temporary_path / file_name
                    # 保存
                    sub_file.write_bytes(ret.content)
                    target_sub_file = Path(working_dir_item.path) / Path(sub_file.name)
                    if storage_chain.get_file_item(storage, target_sub_file):
                        logger.info(f"字幕文件已存在：{target_sub_file}")
                        continue
                    logger.info(f"转移字幕 {sub_file} 到 {target_sub_file} ...")
                    storage_chain.upload_file(working_dir_item, sub_file)
            else:
                logger.error(f"下载字幕文件失败：{sublink}")
                continue
        logger.info(f"{torrent.page_url} 页面字幕下载完成")

    @staticmethod
    def _is_subscribe_source(source: Optional[str]) -> bool:
        """
        判断下载来源是否为订阅任务。
        """
        return bool(source and str(source).startswith("Subscribe|"))

    @staticmethod
    def _format_failure_episodes(meta: Optional[MetaBase]) -> Optional[str]:
        """
        从识别元数据中格式化用于失败记录的集数。
        """
        if not meta:
            return None
        if getattr(meta, "episode", None):
            return meta.episode
        episode_list = getattr(meta, "episode_list", None)
        if episode_list:
            return episode_rules.format_ranges(list(episode_list))
        return None

    @staticmethod
    def _torrent_resource_key(torrent: Optional[TorrentInfo]) -> str:
        """
        生成不保存敏感下载链接的种子资源键。
        """
        if not torrent:
            return ""
        for attr_name in ("torrent_id", "info_hash"):
            value = getattr(torrent, attr_name, None)
            if value:
                return str(value)

        for attr_name in ("page_url", "enclosure"):
            url = getattr(torrent, attr_name, None)
            if not url:
                continue
            match = re.search(r"\[(.*?)](.*)", str(url))
            if match:
                url = match.group(2)
            parsed = urlparse(str(url))
            params = parse_qs(parsed.query)
            for param_name in ("id", "torrentid", "torrent_id", "tid", "hash"):
                values = params.get(param_name)
                if values:
                    return f"{parsed.netloc}:{param_name}={values[0]}"
            if parsed.netloc and parsed.path:
                return f"{parsed.netloc}{parsed.path}"

        title = getattr(torrent, "title", "") or ""
        size = getattr(torrent, "size", "") or ""
        return f"title={title}|size={size}"

    @classmethod
    def _build_download_failure_fingerprint(cls, context: Context) -> Optional[str]:
        """
        根据媒体和种子资源信息生成失败冷却指纹。
        """
        media = getattr(context, "media_info", None)
        torrent = getattr(context, "torrent_info", None)
        if not media or not torrent:
            return None

        media_type = getattr(getattr(media, "type", None), "value", getattr(media, "type", None))
        media_source, media_id = resolve_media_identity(media=media)
        media_key = build_media_key(media_source, media_id) or (
            f"{getattr(media, 'title', '')}:{getattr(media, 'year', '')}"
        )
        meta = getattr(context, "meta_info", None)
        site = getattr(torrent, "site", None) or getattr(torrent, "site_name", None)
        meta_season = getattr(meta, "season", None)
        media_season = getattr(media, "season", None)
        season = meta_season if meta_season is not None else media_season
        payload = {
            "media_type": str(media_type or ""),
            "media_key": str(media_key or ""),
            "season": str(season) if season is not None else "",
            "episodes": cls._format_failure_episodes(meta) or "",
            "site": str(site or ""),
            "resource": cls._torrent_resource_key(torrent),
        }
        if not payload["media_type"] or not payload["media_key"] or not payload["resource"]:
            return None
        raw_text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw_text.encode("utf-8")).hexdigest()

    @staticmethod
    def _download_failure_ttl(error_msg: Optional[str]) -> int:
        """
        按失败原因确定资源冷却时间。
        """
        error_text = str(error_msg or "").lower()
        if any(keyword in error_text for keyword in DOWNLOAD_FAILURE_RESOURCE_ERROR_KEYWORDS):
            return DOWNLOAD_FAILURE_RESOURCE_TTL_SECONDS
        return DOWNLOAD_FAILURE_TRANSIENT_TTL_SECONDS

    def _record_download_failure(
            self,
            context: Context,
            error_msg: Optional[str],
            downloader: Optional[str] = None,
            source: Optional[str] = None,
            episodes: Optional[Set[int]] = None,
    ) -> Optional[str]:
        """
        记录资源级下载失败，并返回本次失败指纹。
        """
        fingerprint = self._build_download_failure_fingerprint(context)
        if not fingerprint:
            return None

        now_timestamp = time.time()
        now_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now_timestamp))
        next_retry_at = time.strftime(
            "%Y-%m-%d %H:%M:%S",
            time.localtime(now_timestamp + self._download_failure_ttl(error_msg)),
        )
        media = context.media_info
        media_source, media_id = resolve_media_identity(media=media)
        meta = context.meta_info
        torrent = context.torrent_info
        site = getattr(torrent, "site", None)
        try:
            DownloadFailureOper().record_failure(
                fingerprint=fingerprint,
                now_time=now_time,
                next_retry_at=next_retry_at,
                type=getattr(getattr(media, "type", None), "value", getattr(media, "type", None)),
                title=getattr(media, "title", None),
                year=getattr(media, "year", None),
                media_source=media_source,
                media_id=media_id,
                seasons=getattr(meta, "season", None),
                episodes=episode_rules.format_ranges(list(episodes)) if episodes else self._format_failure_episodes(meta),
                site=site if isinstance(site, int) else None,
                site_name=getattr(torrent, "site_name", None),
                torrent_id=self._torrent_resource_key(torrent),
                torrent_name=getattr(torrent, "title", None),
                torrent_size=getattr(torrent, "size", None),
                downloader=downloader,
                source=str(source)[:1000] if source else None,
                error_message=str(error_msg or "")[:1000],
            )
        except Exception as err:
            logger.error(f"记录下载失败冷却失败：{str(err)}")
        return fingerprint

    def _active_download_failure_fingerprints(
            self,
            contexts: List[Context],
            source: Optional[str],
    ) -> Dict[str, "DownloadFailure"]:
        """
        查询当前订阅候选中仍处于冷却期的失败记录，返回指纹到失败记录的映射。
        """
        if not self._is_subscribe_source(source):
            return {}
        fingerprints = [
            fingerprint
            for fingerprint in [
                self._build_download_failure_fingerprint(context)
                for context in contexts or []
            ]
            if fingerprint
        ]
        if not fingerprints:
            return {}
        now_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        try:
            return DownloadFailureOper().get_active_by_fingerprints(
                fingerprints=fingerprints, now_time=now_time,
            )
        except Exception as err:
            logger.error(f"查询下载失败冷却失败：{str(err)}")
            return {}

    @staticmethod
    def _log_download_failure_cooldown(
            context: Context,
            failure: Optional["DownloadFailure"],
    ) -> None:
        """记录候选资源处于失败冷却期时的跳过原因和下次重试时间。"""
        reason = getattr(failure, "error_message", None) or "未知原因"
        retry_at = getattr(failure, "next_retry_at", None)
        if retry_at:
            logger.info(
                f"{context.torrent_info.title} 近期添加下载失败（失败原因：{reason}），"
                f"暂时跳过该资源，将于 {retry_at} 后重试"
            )
        else:
            logger.info(
                f"{context.torrent_info.title} 近期添加下载失败（失败原因：{reason}），"
                "暂时跳过该资源"
            )

    def _prepare_batch_download_contexts(
        self,
        contexts: List[Context],
        downloader: Optional[str],
        source: Optional[str],
    ) -> Tuple[List[Context], Dict[str, "DownloadFailure"]]:
        """
        执行批量下载前的资源选择、排序和失败冷却准备。

        :return: 排序后的上下文和本轮失败冷却记录；资源选择事件仍可替换上下文列表
        """
        logger.debug(f"Initial contexts: {len(contexts)} items, Downloader: {downloader}")
        event_data = ResourceSelectionEventData(
            contexts=contexts,
            downloader=downloader,
            origin=source,
        )
        event = eventmanager.send_event(ChainEventType.ResourceSelection, event_data)
        if event and event.event_data:
            event_data = event.event_data
            if event_data.updated and event_data.updated_contexts is not None:
                logger.debug(
                    f"Contexts updated by event: {len(event_data.updated_contexts)} "
                    f"items (source: {event_data.source})"
                )
                contexts = event_data.updated_contexts
        contexts = TorrentHelper().sort_torrents(contexts)
        return contexts, self._active_download_failure_fingerprints(
            contexts=contexts,
            source=source,
        )

    def _download_movie_music_candidates(
        self,
        contexts: List[Context],
        downloaded_list: List[Context],
        active_failure_records: Dict[str, "DownloadFailure"],
        save_path: Optional[str],
        channel: Optional[NotificationChannel],
        source: Optional[str],
        userid: Optional[str],
        username: Optional[str],
        downloader: Optional[str],
        custom_words: Optional[str],
    ) -> None:
        """
        处理电影与音乐的直接候选下载。

        两类媒体都遵循成功后按身份去重、失败后继续尝试后续候选的规则，差异只在去重键。
        """
        downloaded_keys: Dict[MediaType, Set[str]] = {
            MediaType.MOVIE: set(),
            MediaType.MUSIC: set(),
        }
        for context in contexts:
            if global_vars.is_system_stopped:
                break
            media_type = context.media_info.type
            if media_type not in downloaded_keys:
                continue
            fingerprint = self._build_download_failure_fingerprint(context)
            if fingerprint and fingerprint in active_failure_records:
                self._log_download_failure_cooldown(
                    context,
                    active_failure_records[fingerprint],
                )
                continue
            if media_type == MediaType.MOVIE:
                download_key = context.media_info.title_year
                label = "电影"
            else:
                media_source, media_id = resolve_media_identity(media=context.media_info)
                download_key = build_media_key(media_source, media_id) or context.media_info.title_year
                label = "音乐"
            if download_key in downloaded_keys[media_type]:
                continue
            logger.info(f"开始下载{label} {context.torrent_info.title} ...")
            if self.download_single(
                context,
                save_path=save_path,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                downloader=downloader,
                custom_words=custom_words,
            ):
                logger.info(f"{context.torrent_info.title} 添加下载成功")
                downloaded_list.append(context)
                downloaded_keys[media_type].add(download_key)
            elif fingerprint:
                active_failure_records[fingerprint] = None

    def download_torrent(self, torrent: TorrentInfo,
                         channel: NotificationChannel = None,
                         source: Optional[str] = None,
                         userid: Union[str, int] = None
                         ) -> Tuple[Optional[Union[str, bytes]], str, list]:
        """
        下载种子文件，如果是磁力链，会返回磁力链接本身
        :return: 种子内容，种子目录名，种子文件清单
        """

        def __get_redict_url(url: str, ua: Optional[str] = None, cookie: Optional[str] = None) -> Optional[str]:
            """
            获取下载链接， url格式：[base64]url
            """
            # 获取[]中的内容
            m = re.search(r"\[(.*)](.*)", url)
            if m:
                # 参数
                base64_str = m.group(1)
                # URL
                url = m.group(2)
                if not base64_str:
                    return url
                # 解码参数
                req_str = base64.b64decode(base64_str.encode('utf-8')).decode('utf-8')
                req_params: Dict[str, dict] = json.loads(req_str)
                # 是否使用cookie
                if not req_params.get('cookie'):
                    cookie = None
                # 代理
                proxy = req_params.get('proxy')
                # 请求头
                if req_params.get('header'):
                    headers = req_params.get('header')
                else:
                    headers = None
                if req_params.get('method') == 'get':
                    # GET请求
                    res = RequestUtils(
                        ua=ua,
                        cookies=cookie,
                        headers=headers,
                        proxies=get_chain_runtime_config_snapshot().proxy if proxy else None
                    ).get_res(url, params=req_params.get('params'))
                else:
                    # POST请求
                    res = RequestUtils(
                        ua=ua,
                        cookies=cookie,
                        headers=headers,
                        proxies=get_chain_runtime_config_snapshot().proxy if proxy else None
                    ).post_res(url, params=req_params.get('params'))
                if not res:
                    return None
                if not req_params.get('result'):
                    return res.text
                else:
                    data = res.json()
                    success_key = req_params.get('success')
                    if success_key and not data.get(success_key):
                        return None
                    for key in str(req_params.get('result')).split("."):
                        data = data.get(key)
                        if not data:
                            return None
                    result_path = req_params.get('result_path')
                    result_query_param = req_params.get('result_query_param')
                    if result_path and result_query_param:
                        result_url = urljoin(
                            f"{str(req_params.get('result_base_url')).rstrip('/')}/",
                            str(result_path).lstrip('/'),
                        )
                        return f"{result_url}?{urlencode({result_query_param: data})}"
                    data = self._normalize_indirect_download_url(
                        url=data,
                        base_url=req_params.get('result_base_url'),
                    )
                    logger.info("已获取到站点临时下载地址")
                    return data
            return None

        # 获取下载链接
        if not torrent.enclosure:
            return None, "", []
        if torrent.enclosure.startswith("magnet:"):
            return torrent.enclosure, "", []
        # Cookie
        site_cookie = torrent.site_cookie
        indirect_download = torrent.enclosure.startswith("[")
        if indirect_download:
            # 需要解码获取下载地址
            torrent_url = __get_redict_url(url=torrent.enclosure,
                                           ua=torrent.site_ua,
                                           cookie=site_cookie)
            # 涉及解析地址的不使用Cookie下载种子，否则MT会出错
            site_cookie = None
        else:
            torrent_url = torrent.enclosure
        if not torrent_url:
            logger.error(f"{torrent.title} 无法获取下载地址！")
            return None, "", []
        # 下载种子文件
        _, content, download_folder, files, error_msg = TorrentHelper().download_torrent(
            url=torrent_url,
            cookie=site_cookie,
            ua=torrent.site_ua or self.runtime_config.user_agent,
            proxy=torrent.site_proxy,
            cache_invalid=not indirect_download)

        if isinstance(content, str):
            # 磁力链
            return content, "", []

        if not content:
            logger.error(f"下载种子文件失败：{torrent.title}")
            self.post_message(Message(
                channel=channel,
                source=source if channel else None,
                mtype=MessageType.Manual,
                title=f"{torrent.title} 种子下载失败！",
                text=f"错误信息：{error_msg}\n站点：{torrent.site_name}",
                userid=userid))
            return None, "", []

        # 返回 种子文件路径，种子目录名，种子文件清单
        return content, download_folder, files

    @staticmethod
    def _apply_resource_download_event(
            context: Context,
            episodes: Optional[Set[int]],
            channel: Optional[NotificationChannel],
            source: Optional[str],
            downloader: Optional[str],
            save_path: Optional[str],
            userid: Union[str, int, None],
            username: Optional[str],
    ) -> tuple[Optional[str], Optional[str]]:
        """应用资源下载事件覆盖，并校验事件返回的下载目录。"""
        event_data = ResourceDownloadEventData(
            context=context,
            episodes=episodes or context.meta_info.episode_list,
            channel=channel,
            origin=source,
            downloader=downloader,
            options={
                "save_path": save_path,
                "userid": userid,
                "username": username,
                "media_category": context.media_info.category,
            },
        )
        event = eventmanager.send_event(ChainEventType.ResourceDownload, event_data)
        if event and event.event_data:
            event_data = event.event_data
            if event_data.cancel:
                logger.debug(
                    "Resource download canceled by event: %s,Reason: %s",
                    event_data.source,
                    event_data.reason,
                )
                return save_path, "下载被事件取消"
            if event_data.options and "save_path" in event_data.options:
                save_path = event_data.options.get("save_path")
        if save_path is None:
            return None, None
        try:
            return validate_download_save_path(save_path), None
        except ValueError as err:
            logger.warn(str(err))
            return save_path, str(err)

    def _settle_download_success(
            self,
            *,
            context: Context,
            media: MediaInfo,
            meta: MetaInfo,
            torrent: TorrentInfo,
            folder_name: str,
            file_list: list,
            download_dir: Path,
            layout: Optional[str],
            downloader: Optional[str],
            download_hash: str,
            download_episodes: Optional[str],
            episodes: Optional[Set[int]],
            channel: Optional[NotificationChannel],
            source: Optional[str],
            userid: Union[str, int, None],
            username: Optional[str],
            torrent_content: Union[str, bytes],
            custom_words: Optional[str],
    ) -> None:
        """提交下载历史、文件明细和 durable 下载事件。"""
        if layout == "NoSubfolder" or not folder_name:
            download_path = download_dir / file_list[0] if file_list else download_dir
        elif folder_name:
            download_path = download_dir / folder_name
        else:
            download_path = download_dir / Path(file_list[0]).stem if file_list else download_dir
        save_path = download_dir if layout == "NoSubfolder" or not folder_name else download_path
        media_source, media_id = resolve_media_identity(media=media)
        history_payload = {
            "path": download_path.as_posix(), "type": media.type.value,
            "title": media.title, "year": media.year,
            "media_source": media_source, "media_id": media_id,
            "music_type": getattr(media, "music_type", None), "seasons": meta.season,
            "episodes": download_episodes or meta.episode,
            "image": media.get_backdrop_image(), "poster": media.get_poster_image(),
            "downloader": downloader, "download_hash": download_hash,
            "torrent_name": torrent.title, "torrent_description": torrent.description,
            "torrent_site": torrent.site_name, "userid": userid, "username": username,
            "channel": channel_identity(channel),
            "date": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            "media_category": media.category, "episode_group": media.episode_group,
            "note": self._build_download_note(source, media, meta),
            "custom_words": custom_words,
        }
        files_to_add = []
        for file in file_list:
            if episodes:
                file_meta = MetaInfo(Path(file).stem)
                if not file_meta.begin_episode or file_meta.begin_episode not in episodes:
                    continue
            media_exts = (
                self.runtime_config.media_extensions
                + self.runtime_config.subtitle_extensions
                + self.runtime_config.audio_extensions
            )
            if not Path(file).suffix or Path(file).suffix.lower() not in media_exts:
                continue
            files_to_add.append({
                "download_hash": download_hash, "downloader": downloader,
                "fullpath": (save_path / file).as_posix(), "savepath": save_path.as_posix(),
                "filepath": file, "torrentname": meta.org_string,
            })
        event_payload = {
            "hash": download_hash, "context": context, "username": username,
            "downloader": downloader, "episodes": episodes or meta.episode_list, "source": source,
        }

        def after_commit() -> None:
            """在历史与 intent 提交后保持原有通知和任务编排。"""
            self._after_download_history_commit(
                context=context, media=media, meta=meta, torrent=torrent,
                channel=channel, source=source, userid=userid, username=username,
                download_episodes=download_episodes, download_dir=download_dir,
                torrent_content=torrent_content,
            )

        durable_event_writer = getattr(self, "durable_event_writer", None)
        if durable_event_writer:
            durable_event_writer.download_added(
                history_payload=history_payload, file_payloads=files_to_add,
                event_payload=event_payload, after_commit=after_commit,
                publish=lambda payload: self.eventmanager.send_event(EventType.DownloadAdded, payload),
            )
            return
        downloadhis = DownloadHistoryOper()
        downloadhis.add(**history_payload)
        if files_to_add:
            downloadhis.add_files(files_to_add)
        after_commit()
        self.eventmanager.send_event(EventType.DownloadAdded, event_payload)

    def download_single(self, context: Context,
                        torrent_file: Path = None,
                        torrent_content: Optional[Union[str, bytes]] = None,
                        episodes: Set[int] = None,
                        channel: NotificationChannel = None,
                        source: Optional[str] = None,
                        downloader: Optional[str] = None,
                        save_path: Optional[str] = None,
                        userid: Union[str, int] = None,
                        username: Optional[str] = None,
                        label: Optional[str] = None,
                        return_detail: bool = False,
                        custom_words: Optional[str] = None) -> Union[Optional[str], Tuple[Optional[str], Optional[str]]]:
        """
        下载单个资源并发送结果通知。

        保持下载链、消息入口和插件使用的公开签名，实际流程委托给内部执行阶段。
        """
        return self._execute_download_single(
            context=context,
            torrent_file=torrent_file,
            torrent_content=torrent_content,
            episodes=episodes,
            channel=channel,
            source=source,
            downloader=downloader,
            save_path=save_path,
            userid=userid,
            username=username,
            label=label,
            return_detail=return_detail,
            custom_words=custom_words,
        )

    def _execute_download_single(self, context: Context,
                        torrent_file: Path = None,
                        torrent_content: Optional[Union[str, bytes]] = None,
                        episodes: Set[int] = None,
                        channel: NotificationChannel = None,
                        source: Optional[str] = None,
                        downloader: Optional[str] = None,
                        save_path: Optional[str] = None,
                        userid: Union[str, int] = None,
                        username: Optional[str] = None,
                        label: Optional[str] = None,
                        return_detail: bool = False,
                        custom_words: Optional[str] = None) -> Union[Optional[str], Tuple[Optional[str], Optional[str]]]:
        """
        下载及发送通知
        :param context: 资源上下文
        :param torrent_file: 种子文件路径
        :param torrent_content: 种子内容（磁力链或种子文件内容）
        :param episodes: 需要下载的集数
        :param channel: 通知渠道
        :param source: 来源（消息通知、Subscribe、Manual等）
        :param downloader: 下载器
        :param save_path: 保存路径, 支持<storage>:<path>, 如rclone:/MP, smb:/server/share/Movies等
        :param userid: 用户ID
        :param username: 调用下载的用户名/插件名
        :param label: 自定义标签
        :param return_detail: 是否返回详细结果；False 时返回下载任务 hash 或 None，True 时返回 (hash, error_msg)
        :param custom_words: 下载来源（如订阅）的完整自定义识别词文本，随下载记录存档，供整理时原样复现识别
        :return: return_detail=False 时返回下载任务 hash 或 None；return_detail=True 时返回 (hash, error_msg)
        """
        _torrent = context.torrent_info
        _media = context.media_info
        _meta = context.meta_info
        _site_downloader = _torrent.site_downloader

        # 下载目录和下载器分类依赖 TMDB 辅助分类，但媒体主身份保持不变。
        _media = MediaChain().supplement_tmdb_info(_media, _meta)
        context.media_info = _media

        save_path, event_error = self._apply_resource_download_event(
            context, episodes, channel, source, downloader, save_path,
            userid, username,
        )
        if event_error:
            return (None, event_error) if return_detail else None

        # 实际下载的集数
        download_episodes = episode_rules.format_ranges(list(episodes)) if episodes else None
        if episodes is not None:
            context.selected_episodes = sorted(set(episodes))
        elif _meta and _meta.episode_list:
            context.selected_episodes = sorted(set(_meta.episode_list))
        else:
            context.selected_episodes = []
        _folder_name = ""
        if not torrent_file and not torrent_content:
            # 下载种子文件，得到的可能是文件也可能是磁力链
            torrent_content, _folder_name, _file_list = self.download_torrent(_torrent,
                                                                              channel=channel,
                                                                              source=source,
                                                                              userid=userid)
        elif torrent_file:
            if torrent_file.exists():
                torrent_content = torrent_file.read_bytes()
            else:
                # 缓存处理器
                cache_backend = FileCache()
                # 读取缓存的种子文件
                torrent_content = cache_backend.get(torrent_file.as_posix(), region="torrents")

        if not torrent_content:
            self._record_download_failure(
                context=context,
                error_msg="下载种子内容为空",
                downloader=downloader or _site_downloader,
                source=source,
                episodes=episodes,
            )
            return (None, "下载种子内容为空") if return_detail else None

        # 获取种子文件的文件夹名和文件清单
        _folder_name, _file_list = TorrentHelper().get_fileinfo_from_torrent_content(torrent_content)

        album_validation_error = self._validate_music_album_resource(context, _file_list)
        if album_validation_error:
            logger.info(f"{_torrent.title} {album_validation_error}，跳过该资源")
            self._record_download_failure(
                context=context,
                error_msg=album_validation_error,
                downloader=downloader or _site_downloader,
                source=source,
                episodes=episodes,
            )
            return (None, album_validation_error) if return_detail else None

        storage, download_dir, error_msg = self._resolve_media_download_dir(
            media_info=_media,
            save_path=save_path,
        )
        if not download_dir:
            if error_msg == "未找到下载目录":
                self.messagehelper.put(f"{_media.type.value} {_media.title_year} 未找到下载目录！",
                                       title="下载失败", role="system")
            return (None, error_msg or "未找到下载目录") if return_detail else None
        file_uri = FileURI(storage=storage, path=download_dir.as_posix())
        download_dir = Path(file_uri.uri)

        # 添加下载
        result: Optional[tuple] = self.download(content=torrent_content,
                                                cookie=_torrent.site_cookie,
                                                episodes=episodes,
                                                download_dir=download_dir,
                                                category=_media.category,
                                                label=label,
                                                downloader=downloader or _site_downloader)
        if result:
            _downloader, _hash, _layout, error_msg = result
        else:
            _downloader, _hash, _layout, error_msg = None, None, None, "未找到下载器"

        if _hash:
            self._settle_download_success(
                context=context,
                media=_media,
                meta=_meta,
                torrent=_torrent,
                folder_name=_folder_name,
                file_list=_file_list,
                download_dir=download_dir,
                layout=_layout,
                downloader=_downloader,
                download_hash=_hash,
                download_episodes=download_episodes,
                episodes=episodes,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                torrent_content=torrent_content,
                custom_words=custom_words,
            )
        else:
            # 下载失败
            logger.error(f"{_media.title_year} 添加下载任务失败："
                         f"{_torrent.title} - {_torrent.enclosure}，{error_msg}")
            self._record_download_failure(
                context=context,
                error_msg=error_msg,
                downloader=_downloader or downloader or _site_downloader,
                source=source,
                episodes=episodes,
            )
            # 只发送给对应渠道和用户
            self.post_message(Message(
                channel=channel,
                source=source if channel else None,
                mtype=MessageType.Manual,
                title="添加下载任务失败：%s %s"
                      % (_media.title_year, _meta.season_episode),
                text=f"站点：{_torrent.site_name}\n"
                     f"种子名称：{_meta.org_string}\n"
                     f"错误信息：{error_msg}",
                image=_media.get_message_image(),
                userid=userid))
        if return_detail:
            return _hash, error_msg
        return _hash

    def batch_download(
                       self,
                       contexts: List[Context],
                       no_exists: Dict[str, Dict[int, NotExistMediaInfo]] = None,
                       save_path: Optional[str] = None,
                       channel: NotificationChannel = None,
                       source: Optional[str] = None,
                       userid: Optional[str] = None,
                       username: Optional[str] = None,
                       downloader: Optional[str] = None,
                       custom_words: Optional[str] = None
                       ) -> Tuple[List[Context], Dict[str, Dict[int, NotExistMediaInfo]]]:
        """
        兼容批量下载公开入口，委托给内部候选匹配阶段。

        该签名被订阅链、消息入口和插件调用；内部策略拆分不改变候选排序、失败冷却、
        完整覆盖判断或剩余缺集的返回结构。
        """
        return self._execute_batch_download(
            contexts=contexts,
            no_exists=no_exists,
            save_path=save_path,
            channel=channel,
            source=source,
            userid=userid,
            username=username,
            downloader=downloader,
            custom_words=custom_words,
        )

    def _execute_batch_download(self,
                       contexts: List[Context],
                       no_exists: Dict[str, Dict[int, NotExistMediaInfo]] = None,
                       save_path: Optional[str] = None,
                       channel: NotificationChannel = None,
                       source: Optional[str] = None,
                       userid: Optional[str] = None,
                       username: Optional[str] = None,
                       downloader: Optional[str] = None,
                       custom_words: Optional[str] = None
                       ) -> Tuple[List[Context], Dict[str, Dict[int, NotExistMediaInfo]]]:
        """
        根据缺失数据，自动种子列表中组合择优下载
        :param contexts:  资源上下文列表
        :param no_exists:  缺失的剧集信息
        :param save_path:  保存路径, 支持<storage>:<path>, 如rclone:/MP, smb:/server/share/Movies等
        :param channel:  通知渠道
        :param source:  来源（消息通知、订阅、手工下载等）
        :param userid:  用户ID
        :param username: 调用下载的用户名/插件名
        :param downloader: 下载器
        :param custom_words: 下载来源（如订阅）的完整自定义识别词文本，随下载记录存档，供整理时原样复现识别
        :return: 已下载资源列表及剩余缺集，键格式为 no_exists[source:id]
        """
        # 已下载的项目
        downloaded_list: List[Context] = []
        custom_word_list = custom_words.splitlines() if custom_words else None

        def __update_seasons(_mid: str, _need: list, _current: list) -> list:
            """
            更新need_tvs季数，返回剩余季数
            :param _mid: 统一媒体身份键
            :param _need: 需要下载的季数
            :param _current: 已经下载的季数
            """
            # 剩余季数
            need = list(set(_need).difference(set(_current)))
            # 清除已下载的季信息
            seas = copy.deepcopy(no_exists.get(_mid))
            if seas:
                for _sea in list(seas):
                    if _sea not in need:
                        no_exists[_mid].pop(_sea)
                    if not no_exists.get(_mid) and no_exists.get(_mid) is not None:
                        no_exists.pop(_mid)
                        break
            return need

        def __update_episodes(_mid: str, _sea: int, _need: list, _current: set) -> list:
            """
            更新need_tvs集数，返回剩余集数
            :param _mid: 统一媒体身份键
            :param _sea: 季数
            :param _need: 需要下载的集数
            :param _current: 已经下载的集数
            """
            # 剩余集数
            need = list(set(_need).difference(set(_current)))
            if need:
                not_exist = no_exists[_mid][_sea]
                no_exists[_mid][_sea] = NotExistMediaInfo(
                    season=not_exist.season,
                    episodes=need,
                    total_episode=not_exist.total_episode,
                    start_episode=not_exist.start_episode,
                    require_complete_coverage=not_exist.require_complete_coverage
                )
            else:
                no_exists[_mid].pop(_sea)
                if not no_exists.get(_mid) and no_exists.get(_mid) is not None:
                    no_exists.pop(_mid)
            return need

        def __get_season_episodes(_mid: str, season: int) -> int:
            """
            获取需要的季的集数
            """
            if not no_exists.get(_mid):
                return 9999
            no_exist = no_exists.get(_mid)
            if not no_exist.get(season):
                return 9999
            return no_exist[season].total_episode

        def __get_no_exist_media(_mid: str, season: int) -> Optional[NotExistMediaInfo]:
            """
            获取指定媒体和季的缺失信息。
            """
            if not no_exists or not no_exists.get(_mid):
                return None
            return no_exists.get(_mid).get(season)

        def __get_required_episodes(_mid: str, season: int) -> Set[int]:
            """
            获取整季候选必须覆盖的目标集范围。
            """
            tv = __get_no_exist_media(_mid, season)
            if not tv:
                return set()
            if not tv.total_episode:
                return set()
            start = tv.start_episode or 1
            return set(range(start, tv.total_episode + 1))

        def __requires_complete_coverage(_tv: Optional[NotExistMediaInfo]) -> bool:
            """
            判断当前缺失范围是否要求候选资源完整覆盖目标范围。
            """
            if not _tv:
                return False
            return bool(_tv.require_complete_coverage)

        def __apply_allowed_episodes(_need_episodes, _context: Context) -> Set[int]:
            """
            根据候选携带的允许集裁剪 need_episodes，返回真正可下载的剧集集合。

            语义：allowed_episodes 为 None 表示调用方未约束，沿用 need_episodes；
            非空集合则与 need_episodes 取交集；空集合（显式拒绝）会被交集自然消解为空。
            调用方根据返回集合是否为空决定是否跳过当前候选。
            """
            effective = set(_need_episodes)
            allowed = _context.allowed_episodes
            if allowed is not None:
                effective &= set(allowed)
            return effective

        def __get_movie_download_key(_context: Context) -> str:
            """
            获取电影下载去重键，确保失败候选不会阻断后续同名资源尝试。
            """
            return _context.media_info.title_year

        def __get_music_download_key(_context: Context) -> str:
            """获取音乐下载去重键，同一订阅目标失败后仍可尝试后续候选。"""
            media_source, media_id = resolve_media_identity(media=_context.media_info)
            return build_media_key(media_source, media_id) or _context.media_info.title_year

        # 仅排序，不提前按媒体控重；下载失败时需要继续尝试同组后续候选。
        contexts, active_failure_records = self._prepare_batch_download_contexts(
            contexts=contexts,
            downloader=downloader,
            source=source,
        )

        def __is_context_in_failure_cooldown(_context: Context) -> bool:
            """
            判断候选资源是否仍处于失败冷却期。
            """
            fingerprint = self._build_download_failure_fingerprint(_context)
            if fingerprint and fingerprint in active_failure_records:
                self._log_download_failure_cooldown(
                    _context,
                    active_failure_records[fingerprint],
                )
                return True
            return False

        def __remember_context_failure(_context: Context) -> None:
            """
            将本轮失败候选加入内存冷却集合，避免同一批次重复尝试。
            """
            fingerprint = self._build_download_failure_fingerprint(_context)
            if fingerprint:
                active_failure_records[fingerprint] = None

        self._download_movie_music_candidates(
            contexts=contexts,
            downloaded_list=downloaded_list,
            active_failure_records=active_failure_records,
            save_path=save_path,
            channel=channel,
            source=source,
            userid=userid,
            username=username,
            downloader=downloader,
            custom_words=custom_words,
        )

        # 电视剧整季匹配
        if no_exists:
            logger.info(f"开始匹配电视剧整季：{no_exists}")
            # 先把整季缺失的拿出来，看是否刚好有所有季都满足的种子 {source:id: [seasons]}
            need_seasons: Dict[str, list] = {}
            for need_mid, need_tv in no_exists.items():
                for tv in need_tv.values():
                    if not tv:
                        continue
                    # 季列表为空的，代表全季缺失
                    if not tv.episodes:
                        if not need_seasons.get(need_mid):
                            need_seasons[need_mid] = []
                        need_seasons[need_mid].append(tv.season if tv.season is not None else 1)
            logger.info(f"缺失整季：{need_seasons}")
            # 查找整季包含的种子，只处理整季没集的种子或者是集数超过季的种子
            for need_mid, need_season in need_seasons.items():
                # 循环种子
                for context in contexts:
                    if global_vars.is_system_stopped:
                        break
                    # 媒体信息
                    media = context.media_info
                    # 识别元数据
                    meta = context.meta_info
                    # 种子信息
                    torrent = context.torrent_info
                    # 排除电视剧
                    if media.type != MediaType.TV:
                        continue
                    # 种子的季清单
                    torrent_season = meta.season_list
                    # 没有季的默认为第1季
                    if not torrent_season:
                        torrent_season = [1]
                    # 种子有集的不要
                    if meta.episode_list:
                        continue
                    # 匹配TMDBID
                    if self._matches_media_identity(media, need_mid):
                        # 不重复添加
                        if context in downloaded_list:
                            continue
                        if __is_context_in_failure_cooldown(context):
                            continue
                        # 种子季是需要季或者子集
                        if set(torrent_season).issubset(set(need_season)):
                            complete_coverage_matched = False
                            if len(torrent_season) == 1:
                                # 只有一季的可能是命名错误，需要打开种子鉴别，只有实际集数大于等于总集数才下载
                                logger.info(f"开始下载种子 {torrent.title} ...")
                                content, _, torrent_files = self.download_torrent(torrent)
                                if not content:
                                    logger.warn(f"{torrent.title} 种子下载失败！")
                                    self._record_download_failure(
                                        context=context,
                                        error_msg="下载种子内容为空",
                                        downloader=downloader,
                                        source=source,
                                    )
                                    __remember_context_failure(context)
                                    continue
                                if isinstance(content, str):
                                    logger.warn(f"{meta.org_string} 下载地址是磁力链，无法确定种子文件集数")
                                    continue
                                torrent_episodes = TorrentHelper().get_torrent_episodes(
                                    torrent_files,
                                    custom_words=custom_word_list,
                                )
                                logger.info(f"{meta.org_string} 解析种子文件集数为 {torrent_episodes}")
                                if not torrent_episodes:
                                    continue
                                torrent_episodes_set = set(torrent_episodes)
                                # 更新集数范围
                                begin_ep = min(torrent_episodes)
                                end_ep = max(torrent_episodes)
                                meta.set_episodes(begin=begin_ep, end=end_ep)
                                # 需要目标集范围；完整覆盖场景必须覆盖范围内每一集，不能只按数量判断。
                                need_tv_info = __get_no_exist_media(need_mid, torrent_season[0])
                                required_episodes = __get_required_episodes(need_mid, torrent_season[0]) \
                                    if __requires_complete_coverage(need_tv_info) else set()
                                need_total = __get_season_episodes(need_mid, torrent_season[0])
                                complete_coverage_matched = bool(required_episodes) \
                                    and required_episodes.issubset(torrent_episodes_set)
                                if complete_coverage_matched:
                                    logger.info(
                                        f"{meta.org_string} 解析文件集数已完整覆盖目标范围："
                                        f"{episode_rules.format_ranges(sorted(required_episodes))}")
                                if required_episodes and not complete_coverage_matched:
                                    missing_episodes = sorted(required_episodes.difference(torrent_episodes_set))
                                    logger.info(
                                        f"{meta.org_string} 解析文件集数未覆盖目标范围，"
                                        f"缺少 {episode_rules.format_ranges(missing_episodes)}，先放弃这个种子")
                                    continue
                                if not required_episodes and need_total and len(torrent_episodes) < need_total:
                                    logger.info(
                                        f"{meta.org_string} 解析文件集数发现不是完整合集，先放弃这个种子")
                                    continue
                                else:
                                    # 下载
                                    logger.info(f"开始下载 {torrent.title} ...")
                                    download_id = self.download_single(
                                        context=context,
                                        torrent_content=content,
                                        save_path=save_path,
                                        channel=channel,
                                        source=source,
                                        userid=userid,
                                        username=username,
                                        downloader=downloader,
                                        custom_words=custom_words
                                    )
                            else:
                                # 下载
                                logger.info(f"开始下载 {torrent.title} ...")
                                download_id = self.download_single(context, save_path=save_path,
                                                                   channel=channel, source=source,
                                                                   userid=userid, username=username,
                                                                   downloader=downloader,
                                                                   custom_words=custom_words)

                            if download_id:
                                # 下载成功
                                if complete_coverage_matched:
                                    context.confirmed_full_coverage = True
                                logger.info(f"{torrent.title} 添加下载成功")
                                downloaded_list.append(context)
                                # 更新仍需季集
                                need_season = __update_seasons(_mid=need_mid,
                                                               _need=need_season,
                                                               _current=torrent_season)
                                logger.info(f"{need_mid} 剩余需要季：{need_season}")
                                if not need_season:
                                    # 全部下载完成
                                    break
                            else:
                                __remember_context_failure(context)
        # 电视剧季内的集匹配
        if no_exists:
            logger.info(f"开始电视剧完整集匹配：{no_exists}")
            # TMDBID列表
            need_tv_list = list(no_exists)
            for need_mid in need_tv_list:
                # dict[season, [NotExistMediaInfo]]
                need_tv = no_exists.get(need_mid)
                if not need_tv:
                    continue
                need_tv_copy = copy.deepcopy(no_exists.get(need_mid))
                # 循环每一季
                for sea, tv in need_tv_copy.items():
                    # 当前需要季
                    need_season = sea
                    # 当前需要集
                    need_episodes = tv.episodes
                    # TMDB总集数
                    total_episode = tv.total_episode
                    # 需要开始集
                    start_episode = tv.start_episode or 1
                    # 缺失整季的转化为缺失集进行比较
                    if not need_episodes:
                        need_episodes = list(range(start_episode, total_episode + 1))
                    # 循环种子
                    for context in contexts:
                        if global_vars.is_system_stopped:
                            break
                        # 媒体信息
                        media = context.media_info
                        # 识别元数据
                        meta = context.meta_info
                        # 非剧集不处理
                        if media.type != MediaType.TV:
                            continue
                        # 匹配TMDB
                        if self._matches_media_identity(media, need_mid):
                            # 不重复添加
                            if context in downloaded_list:
                                continue
                            if __is_context_in_failure_cooldown(context):
                                continue
                            # 种子季
                            torrent_season = meta.season_list
                            # 只处理单季含集的种子
                            if len(torrent_season) != 1 or torrent_season[0] != need_season:
                                continue
                            # 种子集列表
                            torrent_episodes = set(meta.episode_list)
                            # 整季的不处理
                            if not torrent_episodes:
                                continue
                            # 上游对本候选施加的允许集（如洗版按集允许列表）裁剪本季缺集，得到真正可下载范围。
                            effective_need = __apply_allowed_episodes(need_episodes, context)
                            if not effective_need:
                                continue
                            if __requires_complete_coverage(tv):
                                # 完整覆盖任务要求候选集数覆盖目标范围，允许资源包含范围外的额外集。
                                required_episodes = __get_required_episodes(need_mid, need_season)
                                match_episodes = required_episodes.issubset(torrent_episodes) \
                                    if required_episodes else False
                            else:
                                # 普通缺集下载保持原语义：候选自身必须是所需集的子集。
                                match_episodes = torrent_episodes.issubset(effective_need)
                            if match_episodes:
                                # 下载
                                logger.info(f"开始下载 {meta.title} ...")
                                download_id = self.download_single(context, save_path=save_path,
                                                                   channel=channel, source=source,
                                                                   userid=userid, username=username,
                                                                   downloader=downloader,
                                                                   custom_words=custom_words)
                                if download_id:
                                    # 下载成功
                                    if __requires_complete_coverage(tv):
                                        context.confirmed_full_coverage = True
                                    logger.info(f"{meta.title} 添加下载成功")
                                    downloaded_list.append(context)
                                    # 更新仍需集数
                                    need_episodes = __update_episodes(_mid=need_mid,
                                                                      _need=need_episodes,
                                                                      _sea=need_season,
                                                                      _current=torrent_episodes)
                                    logger.info(f"季 {need_season} 剩余需要集：{need_episodes}")
                                else:
                                    __remember_context_failure(context)

        # 仍然缺失的剧集，从整季中选择需要的集数文件下载，仅支持QB和TR
        if no_exists:
            logger.info(f"开始电视剧多集拆包匹配：{no_exists}")
            # TMDBID列表
            no_exists_list = list(no_exists)
            for need_mid in no_exists_list:
                # dict[season, [NotExistMediaInfo]]
                need_tv = no_exists.get(need_mid)
                if not need_tv:
                    continue
                # 需要季列表
                need_tv_list = list(need_tv)
                # 循环需要季
                for sea in need_tv_list:
                    # NotExistMediaInfo
                    tv = need_tv.get(sea)
                    # 当前需要季
                    need_season = sea
                    # 当前需要集
                    need_episodes = tv.episodes
                    if __requires_complete_coverage(tv):
                        continue
                    # 没有集的不处理
                    if not need_episodes:
                        continue
                    # 循环种子
                    for context in contexts:
                        if global_vars.is_system_stopped:
                            break
                        # 媒体信息
                        media = context.media_info
                        # 识别元数据
                        meta = context.meta_info
                        # 种子信息
                        torrent = context.torrent_info
                        # 非剧集不处理
                        if media.type != MediaType.TV:
                            continue
                        # 不重复添加
                        if context in downloaded_list:
                            continue
                        if __is_context_in_failure_cooldown(context):
                            continue
                        # 没有需要集后退出
                        if not need_episodes:
                            break
                        # 上游对本候选施加的允许集（如洗版按集允许列表）裁剪本季缺集，得到真正可下载范围。
                        effective_need = __apply_allowed_episodes(need_episodes, context)
                        if not effective_need:
                            continue
                        # 选中一个单季整季的或单季包括需要的所有集的
                        if self._matches_media_identity(media, need_mid) \
                                and (not meta.episode_list
                                     or set(meta.episode_list).intersection(effective_need)) \
                                and len(meta.season_list) == 1 \
                                and meta.season_list[0] == need_season:
                            # 检查种子看是否有需要的集
                            logger.info(f"开始下载种子 {torrent.title} ...")
                            content, _, torrent_files = self.download_torrent(torrent)
                            if not content:
                                logger.info(f"{torrent.title} 种子下载失败！")
                                self._record_download_failure(
                                    context=context,
                                    error_msg="下载种子内容为空",
                                    downloader=downloader,
                                    source=source,
                                )
                                __remember_context_failure(context)
                                continue
                            if isinstance(content, str):
                                logger.warn(f"{meta.org_string} 下载地址是磁力链，无法解析种子文件集数")
                                continue
                            # 种子全部集
                            torrent_episodes = TorrentHelper().get_torrent_episodes(
                                torrent_files,
                                custom_words=custom_word_list,
                            )
                            logger.info(f"{torrent.site_name} - {meta.org_string} 解析种子文件集数：{torrent_episodes}")
                            # 选中的集
                            selected_episodes = set(torrent_episodes).intersection(effective_need)
                            if not selected_episodes:
                                logger.info(f"{torrent.site_name} - {torrent.title} 没有需要的集，跳过...")
                                continue
                            logger.info(f"{torrent.site_name} - {torrent.title} 选中集数：{selected_episodes}")
                            # 添加下载
                            logger.info(f"开始下载 {torrent.title} ...")
                            download_id = self.download_single(
                                context=context,
                                torrent_content=content,
                                episodes=selected_episodes,
                                save_path=save_path,
                                channel=channel,
                                source=source,
                                userid=userid,
                                username=username,
                                downloader=downloader,
                                custom_words=custom_words
                            )
                            if not download_id:
                                __remember_context_failure(context)
                                continue
                            # 下载成功
                            logger.info(f"{torrent.title} 添加下载成功")
                            downloaded_list.append(context)
                            # 更新种子集数范围
                            begin_ep = min(torrent_episodes)
                            end_ep = max(torrent_episodes)
                            meta.set_episodes(begin=begin_ep, end=end_ep)
                            # 更新仍需集数
                            need_episodes = __update_episodes(_mid=need_mid,
                                                              _need=need_episodes,
                                                              _sea=need_season,
                                                              _current=selected_episodes)
                            logger.info(f"季 {need_season} 剩余需要集：{need_episodes}")

        # 返回下载的资源，剩下没下完的
        logger.info(f"成功下载种子数：{len(downloaded_list)}，剩余未下载的剧集：{no_exists}")
        return downloaded_list, no_exists

    def get_no_exists_info(self, meta: MetaBase,
                           mediainfo: MediaInfo | MusicInfo,
                           no_exists: Dict[str, Dict[int, NotExistMediaInfo]] = None,
                           totals: Dict[int, int] = None
                           ) -> Tuple[bool, Dict[str, Dict[int, NotExistMediaInfo]]]:
        """
        检查媒体库，查询电影或音乐是否存在；对于剧集同时返回不存在的季集信息
        :param meta: 元数据
        :param mediainfo: 已识别的媒体信息
        :param no_exists: 在调用该方法前已经存储的不存在的季集信息，有传入时该函数搜索的内容将会叠加后输出
        :param totals: 电视剧每季的总集数
        :return: 当前媒体是否缺失，各标题总的季集和缺失的季集
        """
        media_source, media_id = resolve_media_identity(media=mediainfo)
        if mediainfo.type == MediaType.TV and not build_media_key(media_source, media_id):
            logger.error("电视剧缺集检查需要有效的 media_source 和 media_id")
            return False, no_exists or {}

        if not no_exists:
            no_exists = {}

        if not totals:
            totals = {}

        mediaserver = MediaServerOper()
        if mediainfo.type == MediaType.MOVIE:
            # 电影
            itemid = mediaserver.get_item_id(mtype=mediainfo.type.value,
                                             title=mediainfo.title,
                                             media_source=media_source,
                                             media_id=media_id)
            exists_movies: Optional[ExistMediaInfo] = self.media_exists(mediainfo=mediainfo, itemid=itemid)
            if exists_movies:
                logger.info(f"媒体库中已存在电影：{mediainfo.title_year}")
                return True, {}
            return False, {}
        if mediainfo.type == MediaType.MUSIC:
            # 专辑在媒体库中按一个集合条目判断；单曲则由具体音乐服务器继续按曲名检索。
            itemid = mediaserver.get_item_id(
                mtype=mediainfo.type.value,
                title=mediainfo.title,
                year=mediainfo.year,
            )
            exists_music: Optional[ExistMediaInfo] = self.media_exists(
                mediainfo=mediainfo,
                itemid=itemid,
            )
            if exists_music:
                logger.info(f"媒体库中已存在音乐：{mediainfo.title_year}")
                return True, {}
            return False, {}
        else:
            if not mediainfo.seasons:
                # 补充媒体信息
                mediainfo: MediaInfo = MediaChain().recognize_media(mtype=mediainfo.type,
                                                            media_source=media_source,
                                                            media_id=media_id,
                                                            episode_group=mediainfo.episode_group)
                if not mediainfo:
                    logger.error(f"媒体信息识别失败！")
                    return False, {}
                if not mediainfo.seasons:
                    logger.error(f"媒体信息中没有季集信息：{mediainfo.title_year}")
                    return False, {}
            # 电视剧
            itemid = mediaserver.get_item_id(mtype=mediainfo.type.value,
                                             title=mediainfo.title,
                                             media_source=media_source,
                                             media_id=media_id,
                                             season=mediainfo.season)
            # 媒体库已存在的剧集
            exists_tvs: Optional[ExistMediaInfo] = self.media_exists(mediainfo=mediainfo, itemid=itemid)
            if not exists_tvs:
                # 所有季集均缺失
                for season, episodes in mediainfo.seasons.items():
                    if not episodes:
                        continue
                    # 全季不存在
                    if meta.sea \
                            and season not in meta.season_list:
                        continue
                    # 总集数
                    total_ep = totals.get(season) or len(episodes)
                    self._append_no_exists(
                        no_exists, media_source, media_id, season, [],
                        total_ep, min(episodes)
                    )
                return False, no_exists
            else:
                # 存在一些，检查每季缺失的季集
                for season, episodes in mediainfo.seasons.items():
                    if meta.sea \
                            and season not in meta.season_list:
                        continue
                    if not episodes:
                        continue
                    # 该季总集数
                    season_total = totals.get(season) or len(episodes)
                    # 该季已存在的集
                    exist_episodes = exists_tvs.seasons.get(season)
                    if exist_episodes:
                        # 已存在取差集
                        if totals.get(season):
                            # 按总集数计算缺失集（开始集为TMDB中的最小集）
                            lack_episodes = list(set(range(min(episodes),
                                                           season_total + min(episodes))
                                                     ).difference(set(exist_episodes)))
                        else:
                            # 按TMDB集数计算缺失集
                            lack_episodes = list(set(episodes).difference(set(exist_episodes)))
                        if not lack_episodes:
                            # 全部集存在
                            continue
                        # 添加不存在的季集信息
                        self._append_no_exists(
                            no_exists, media_source, media_id, season,
                            lack_episodes, season_total, min(lack_episodes)
                        )
                    else:
                        # 全季不存在
                        self._append_no_exists(
                            no_exists, media_source, media_id, season, [],
                            season_total, min(episodes)
                        )
            # 存在不完整的剧集
            if no_exists:
                logger.debug(f"媒体库中已存在部分剧集，缺失：{no_exists}")
                return False, no_exists
            # 全部存在
            return True, no_exists

    @staticmethod
    def _append_no_exists(
            no_exists: Dict[str, Dict[int, NotExistMediaInfo]],
            media_source: Optional[MediaSource],
            media_id: Optional[str],
            season: int,
            episodes: list,
            total: int,
            start: int,
    ) -> None:
        """把一季缺失信息合并到标准媒体身份对应的结果中。"""
        media_key = build_media_key(media_source, media_id)
        no_exists.setdefault(media_key, {})[season] = NotExistMediaInfo(
            season=season,
            episodes=episodes,
            total_episode=total,
            start_episode=start,
        )

    def remote_downloading(self, channel: NotificationChannel, userid: Union[str, int] = None, source: Optional[str] = None):
        """
        查询正在下载的任务，并发送消息
        """
        torrents = self.list_torrents(status=TorrentStatus.DOWNLOADING)
        if not torrents:
            self.post_message(Message(
                channel=channel,
                source=source,
                mtype=MessageType.Download,
                title="没有正在下载的任务！",
                userid=userid,
                link=self.runtime_config.downloading_url,
                save_history=False,
            ))
            return
        # 发送消息
        title = f"共 {len(torrents)} 个任务正在下载："
        messages = []
        index = 1
        for torrent in torrents:
            messages.append(f"{index}. {torrent.title} "
                            f"{size_tools.format_compact_size(torrent.size)} "
                            f"{round(torrent.progress, 1)}%")
            index += 1
        self.post_message(Message(
            channel=channel,
            source=source,
            mtype=MessageType.Download,
            title=title,
            text="\n".join(messages),
            userid=userid,
            link=self.runtime_config.downloading_url,
            save_history=False,
        ))

    def downloading(self, name: Optional[str] = None) -> List[DownloaderTorrent]:
        """
        查询正在下载的任务
        """
        return self._download_task_service().downloading(name)

    def set_downloading(self, hash_str, oper: str, name: Optional[str] = None) -> bool:
        """
        控制下载任务 start/stop
        """
        return self._download_task_service().set_downloading(
            hash_str,
            oper,
            name,
        )

    def remove_downloading(self, hash_str: str, name: Optional[str] = None) -> bool:
        """
        删除下载任务
        """
        return self._download_task_service().remove_downloading(hash_str, name)

    def _download_task_service(self) -> DownloadTaskService:
        """构造绑定当前下载器能力与历史仓储的任务服务。"""
        return DownloadTaskService(
            list_torrents=self.list_torrents,
            get_history_by_hashes=DownloadHistoryOper().get_by_hashes,
            start_torrents=self.start_torrents,
            stop_torrents=self.stop_torrents,
            remove_torrents=self.remove_torrents,
        )

    @eventmanager.register(EventType.DownloadFileDeleted)
    def download_file_deleted(self, event: Event):
        """
        下载文件删除时，同步删除下载任务
        """
        if not event:
            return
        hash_str = event.event_data.get("hash")
        if not hash_str:
            return
        logger.warn(f"检测到下载源文件被删除，删除下载任务（不含文件）：{hash_str}")
        # 先查询种子
        torrents: List[_SchemaDownloaderTorrent] = self.list_torrents(hashs=[hash_str])
        if torrents:
            self.remove_torrents(hashs=[hash_str], delete_file=False)
            # 发出下载任务删除事件，如需处理辅种，可监听该事件
            self.eventmanager.send_event(EventType.DownloadDeleted, {
                "hash": hash_str,
                    "torrents": [torrent.model_dump() for torrent in torrents]
            })
        else:
            logger.info(f"没有在下载器中查询到 {hash_str} 对应的下载任务")
