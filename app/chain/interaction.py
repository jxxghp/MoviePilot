import math
import re
from typing import Any, Dict, List, Optional, Tuple, Union

from app.application.directory import DirectoryHelper
from app.application.messaging.media import (
    PendingMediaInteraction,
    media_interaction_manager,
)
from app.application.torrent.download import TorrentHelper
from app.chain.base import ChainBase
from app.chain.download import DownloadChain
from app.chain.media import MediaChain
from app.chain.search.facade import SearchChain
from app.chain.subscribe.facade import SubscribeChain
from app.domain import episode as episode_rules
from app.domain import title as title_rules
from app.domain.context import Context, MediaInfo
from app.domain.meta.metabase import MetaBase
from app.foundation import url as url_tools
from app.runtime.log import logger
from app.schemas.download import DownloadDirectory
from app.schemas.file import FileURI
from app.schemas.media import build_media_key, resolve_media_identity
from app.schemas.mediaserver import NotExistMediaInfo
from app.schemas.message import Message
from app.schemas.notification import ChannelCapabilityManager
from app.schemas.system import TransferDirectoryConf
from app.schemas.types import MediaType, NotificationChannel


class MediaInteractionChain(ChainBase):
    """
    处理媒体搜索、订阅、资源选择和翻页等交互流程。
    """

    _button_page_size = 8
    _text_page_size = 8
    _auto_download_dir_name = "自动匹配目录"

    @staticmethod
    def has_pending_interaction(user_id: Union[str, int]) -> bool:
        """
        判断用户当前是否存在未结束的媒体交互。
        """
        return media_interaction_manager.get_by_user(user_id) is not None

    @staticmethod
    def _get_noexits_info(
            meta: MetaBase, mediainfo: MediaInfo
    ) -> Dict[Union[int, str], Dict[int, NotExistMediaInfo]]:
        """
        构造媒体缺失集信息，用于全量重搜或自动下载补全集数。
        """
        if mediainfo.type == MediaType.TV:
            if not mediainfo.seasons:
                mediainfo = MediaChain().recognize_media(
                    mtype=mediainfo.type,
                    media_source=resolve_media_identity(media=mediainfo)[0],
                    media_id=resolve_media_identity(media=mediainfo)[1],
                    cache=False,
                )
                if not mediainfo:
                    logger.warn("媒体信息识别失败，无法补充季集信息")
                    return {}
                if not mediainfo.seasons:
                    logger.warn(
                        "媒体信息中没有季集信息，标题：%s，tmdbid：%s，doubanid：%s",
                        mediainfo.title,
                        mediainfo.tmdb_id,
                        mediainfo.douban_id,
                    )
                    return {}

            media_source, media_id = resolve_media_identity(media=mediainfo)
            mediakey = build_media_key(media_source, media_id)
            no_exists = {mediakey: {}}
            if meta.begin_season is not None:
                episodes = mediainfo.seasons.get(meta.begin_season)
                if not episodes:
                    return {}
                no_exists[mediakey][meta.begin_season] = NotExistMediaInfo(
                    season=meta.begin_season,
                    episodes=[],
                    total_episode=len(episodes),
                    start_episode=episodes[0],
                )
            else:
                for sea, eps in mediainfo.seasons.items():
                    if not eps:
                        continue
                    no_exists[mediakey][sea] = NotExistMediaInfo(
                        season=sea,
                        episodes=[],
                        total_episode=len(eps),
                        start_episode=eps[0],
                    )
            return no_exists
        return {}

    @staticmethod
    def parse_callback(
            callback_data: str,
    ) -> Optional[Tuple[Optional[str], str, Optional[int]]]:
        """
        解析新旧两种媒体交互按钮格式。
        """
        if callback_data.startswith("media:"):
            parts = callback_data.split(":")
            if len(parts) < 3:
                return None
            request_id = parts[1]
            action = parts[2]
            index = None
            if len(parts) >= 4 and parts[3].isdigit():
                index = int(parts[3])
            return request_id, action, index

        match = re.match(r"^(select|download)_(\d+)$", callback_data)
        if match:
            return None, match.group(1), int(match.group(2))
        if callback_data == "page_p":
            return None, "page-prev", None
        if callback_data == "page_n":
            return None, "page-next", None
        return None

    def handle_callback_interaction(
            self,
            callback_data: str,
            channel: NotificationChannel,
            source: str,
            userid: Union[str, int],
            username: str,
            original_message_id: Optional[Union[str, int]] = None,
            original_chat_id: Optional[str] = None,
    ) -> bool:
        """
        处理按钮回调，并将当前视图刷新到原消息上。
        """
        parsed = self.parse_callback(callback_data)
        if not parsed:
            return False

        request_id, action, index = parsed
        if request_id:
            request = media_interaction_manager.get_by_id(request_id, userid)
        else:
            request = media_interaction_manager.get_by_user(userid)

        if not request:
            self.post_message(
                Message(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title="交互已失效，请重新搜索或订阅",
                    save_history=False,
                )
            )
            return True

        request.channel = channel
        request.source = source
        request.username = username

        if action == "page-prev":
            if request.page <= 0:
                self._post_invalid_input(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title="已经是第一页了！",
                )
                return True
            request.page -= 1
            self._render_interaction(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                original_message_id=original_message_id,
                original_chat_id=original_chat_id,
            )
            return True

        if action == "page-next":
            if not self._has_next_page(request):
                self._post_invalid_input(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title="已经是最后一页了！",
                )
                return True
            request.page += 1
            self._render_interaction(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                original_message_id=original_message_id,
                original_chat_id=original_chat_id,
            )
            return True

        if action == "select":
            self._handle_media_selection(
                request=request,
                page_index=index,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                original_message_id=original_message_id,
                original_chat_id=original_chat_id,
            )
            return True

        if action == "download":
            self._handle_torrent_selection(
                request=request,
                page_index=index,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            )
            return True

        if action == "download-dir":
            self._handle_download_dir_selection(
                request=request,
                page_index=index,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            )
            return True

        return False

    def handle_text_interaction(
            self,
            channel: NotificationChannel,
            source: str,
            userid: Union[str, int],
            username: str,
            text: str,
    ) -> bool:
        """
        处理文本式交互。

        有会话时优先处理数字选择和翻页；无会话时负责识别搜索/订阅类入口。
        """
        request = media_interaction_manager.get_by_user(userid)
        normalized = (text or "").strip()
        lowered = normalized.lower()

        if request and lowered in {"退出", "关闭", "q", "quit", "exit"}:
            media_interaction_manager.remove(request.request_id)
            self.post_message(
                Message(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title="媒体交互已结束",
                    save_history=False,
                )
            )
            return True

        if normalized.isdigit():
            if not request:
                self._post_invalid_input(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                )
                return True
            request.channel = channel
            request.source = source
            request.username = username
            index = int(normalized)
            if request.phase == "download-dir":
                self._handle_download_dir_selection(
                    request=request,
                    page_index=index,
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                )
            elif request.phase == "torrent":
                self._handle_torrent_selection(
                    request=request,
                    page_index=index,
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                )
            else:
                self._handle_media_selection(
                    request=request,
                    page_index=index,
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                )
            return True

        if lowered in {"p", "prev", "上一页"}:
            if not request:
                self._post_invalid_input(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                )
                return True
            if request.page <= 0:
                self._post_invalid_input(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title="已经是第一页了！",
                )
                return True
            request.page -= 1
            request.channel = channel
            request.source = source
            request.username = username
            self._render_interaction(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
            )
            return True

        if lowered in {"n", "next", "下一页"}:
            if not request:
                self._post_invalid_input(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                )
                return True
            if not self._has_next_page(request):
                self._post_invalid_input(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title="已经是最后一页了！",
                )
                return True
            request.page += 1
            request.channel = channel
            request.source = source
            request.username = username
            self._render_interaction(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
            )
            return True

        action, content = self._resolve_action(normalized)
        if not action:
            return False

        self._start_media_interaction(
            action=action,
            content=content,
            channel=channel,
            source=source,
            userid=userid,
            username=username,
        )
        return True

    @staticmethod
    def _resolve_action(text: str) -> Tuple[Optional[str], str]:
        """
        将用户输入归类为搜索、订阅或普通聊天。
        """
        if text.startswith("订阅"):
            return "Subscribe", re.sub(r"订阅[:：\s]*", "", text)
        if text.startswith("洗版"):
            return "ReSubscribe", re.sub(r"洗版[:：\s]*", "", text)
        if text.startswith("搜索") or text.startswith("下载"):
            return "ReSearch", re.sub(r"(搜索|下载)[:：\s]*", "", text)
        if url_tools.is_link(text):
            return None, text
        if not title_rules.is_media_title_like(text):
            return None, text
        return "Search", text

    def _start_media_interaction(
            self,
            action: str,
            content: str,
            channel: NotificationChannel,
            source: str,
            userid: Union[str, int],
            username: str,
    ) -> None:
        """
        根据用户输入搜索媒体，并进入媒体选择阶段。
        """
        meta, medias = MediaChain().search(content)
        if not meta.name:
            self._post_invalid_input(
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                title="无法识别输入内容！",
            )
            return
        if not medias:
            self.post_message(
                Message(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title=f"{meta.name} 没有找到对应的媒体信息！",
                    save_history=False,
                )
            )
            return

        logger.info("搜索到 %s 条相关媒体信息", len(medias))
        request = media_interaction_manager.create_or_replace(
            user_id=userid,
            channel=channel,
            source=source,
            username=username,
            action=action,
            keyword=content,
            title=meta.name,
            meta=meta,
            items=medias,
        )
        self._render_interaction(
            request=request,
            channel=channel,
            source=source,
            userid=userid,
        )

    def _handle_media_selection(
            self,
            request: PendingMediaInteraction,
            page_index: Optional[int],
            channel: NotificationChannel,
            source: str,
            userid: Union[str, int],
            username: str,
            original_message_id: Optional[Union[str, int]] = None,
            original_chat_id: Optional[str] = None,
    ) -> None:
        """
        处理媒体选择阶段的序号输入。
        """
        page_items, page, _ = self._page_items(
            items=request.items,
            page=request.page,
            page_size=self._page_size(request.channel),
        )
        request.page = page
        if not page_index or page_index < 1 or page_index > len(page_items):
            self._post_invalid_input(
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            )
            return

        mediainfo: MediaInfo = page_items[page_index - 1]
        request.current_media = mediainfo

        if request.action in {"Search", "ReSearch"}:
            self._search_media_resources(
                request=request,
                mediainfo=mediainfo,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                original_message_id=original_message_id,
                original_chat_id=original_chat_id,
            )
            return

        if request.action in {"Subscribe", "ReSubscribe"}:
            self._subscribe_media(
                request=request,
                mediainfo=mediainfo,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            )

    def _search_media_resources(
            self,
            request: PendingMediaInteraction,
            mediainfo: MediaInfo,
            channel: NotificationChannel,
            source: str,
            userid: Union[str, int],
            username: str,
            original_message_id: Optional[Union[str, int]] = None,
            original_chat_id: Optional[str] = None,
    ) -> None:
        """
        根据已选媒体搜索资源，并切换到资源选择阶段。
        """
        exist_flag, no_exists = DownloadChain().get_no_exists_info(
            meta=request.meta,
            mediainfo=mediainfo,
        )
        if exist_flag and request.action == "Search":
            self.post_message(
                Message(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title=f"【{mediainfo.title_year}{request.meta.sea} 媒体库中已存在，如需重新下载请发送：搜索 名称 或 下载 名称】",
                    save_history=False,
                )
            )
            return
        if exist_flag:
            no_exists = self._get_noexits_info(request.meta, mediainfo)

        messages = self._build_no_exists_messages(
            mediainfo=mediainfo,
            no_exists=no_exists,
            show_missing_only=request.action == "Search",
        )
        if messages:
            self.post_message(
                Message(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title=f"{mediainfo.title_year}：\n" + "\n".join(messages),
                    save_history=False,
                )
            )

        logger.info("开始搜索 %s ...", mediainfo.title_year)
        self.post_message(
            Message(
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                title=f"开始搜索 {mediainfo.type.value} {mediainfo.title_year} ...",
                save_history=False,
            )
        )

        contexts = SearchChain().process(mediainfo=mediainfo, no_exists=no_exists)
        if not contexts:
            self.post_message(
                Message(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title=f"{mediainfo.title}{request.meta.sea} 未搜索到需要的资源！",
                    save_history=False,
                )
            )
            return

        contexts = TorrentHelper().sort_torrents(contexts)
        if self._should_auto_download(userid):
            logger.info("用户 %s 在自动下载用户中，开始自动择优下载 ...", userid)
            request.phase = "torrent"
            request.page = 0
            request.title = mediainfo.title
            request.items = list(contexts)
            if self._prompt_download_dir_selection(
                    request=request,
                    download_mode="auto",
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    no_exists=no_exists,
                    original_message_id=original_message_id,
                    original_chat_id=original_chat_id,
            ):
                return
            self._auto_download(
                request=request,
                cache_list=contexts,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                no_exists=no_exists,
            )
            return

        request.phase = "torrent"
        request.page = 0
        request.title = mediainfo.title
        request.items = list(contexts)
        self._render_interaction(
            request=request,
            channel=channel,
            source=source,
            userid=userid,
            original_message_id=original_message_id,
            original_chat_id=original_chat_id,
        )

    def _subscribe_media(
            self,
            request: PendingMediaInteraction,
            mediainfo: MediaInfo,
            channel: NotificationChannel,
            source: str,
            userid: Union[str, int],
            username: str,
    ) -> None:
        """
        根据已选媒体创建订阅或洗版订阅。
        """
        best_version = request.action == "ReSubscribe"
        if not best_version:
            exist_flag, _ = DownloadChain().get_no_exists_info(
                meta=request.meta,
                mediainfo=mediainfo,
            )
            if exist_flag:
                self.post_message(
                    Message(
                        channel=channel,
                        source=source,
                        userid=userid,
                        username=username,
                        title=f"【{mediainfo.title_year}{request.meta.sea} 媒体库中已存在，如需洗版请发送：洗版 XXX】",
                        save_history=False,
                    )
                )
                return

        mp_name = (
            self.user_repository.find_name_by_bindings(
                {f"{channel.name.lower()}_userid": userid}
            )
            if channel
            else None
        )
        SubscribeChain().add(
            title=mediainfo.title,
            year=mediainfo.year,
            mtype=mediainfo.type,
            media_source=mediainfo.media_source,
            media_id=mediainfo.media_id,
            season=request.meta.begin_season,
            channel=channel,
            source=source,
            userid=str(userid),
            username=mp_name or username,
            best_version=best_version,
        )

    def _handle_torrent_selection(
            self,
            request: PendingMediaInteraction,
            page_index: Optional[int],
            channel: NotificationChannel,
            source: str,
            userid: Union[str, int],
            username: str,
    ) -> None:
        """
        处理资源选择阶段的下载操作。
        """
        if request.phase != "torrent":
            self._post_invalid_input(
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            )
            return

        if page_index == 0:
            if self._prompt_download_dir_selection(
                    request=request,
                    download_mode="auto",
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
            ):
                return
            self._auto_download(
                request=request,
                cache_list=request.items,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            )
            return

        page_items, page, _ = self._page_items(
            items=request.items,
            page=request.page,
            page_size=self._page_size(request.channel),
        )
        request.page = page
        if not page_index or page_index < 1 or page_index > len(page_items):
            self._post_invalid_input(
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            )
            return

        context: Context = page_items[page_index - 1]
        if self._prompt_download_dir_selection(
                request=request,
                download_mode="single",
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                context=context,
        ):
            return
        DownloadChain().download_single(
            context,
            channel=channel,
            source=source,
            userid=userid,
            username=username,
        )

    def _prompt_download_dir_selection(
            self,
            request: PendingMediaInteraction,
            download_mode: str,
            channel: NotificationChannel,
            source: str,
            userid: Union[str, int],
            username: str,
            context: Optional[Context] = None,
            no_exists: Optional[Dict[Union[int, str], Dict[int, NotExistMediaInfo]]] = None,
            original_message_id: Optional[Union[str, int]] = None,
            original_chat_id: Optional[str] = None,
    ) -> bool:
        """
        在下载前进入目录选择阶段；没有配置下载目录时保持原下载流程。
        """
        media_info = context.media_info if context else request.current_media
        download_dirs = self._get_download_dirs(media_info)
        if not download_dirs:
            return False
        if len(download_dirs) == 1 and not self._is_auto_download_dir(download_dirs[0]):
            return False

        request.pending_torrent_page = request.page
        request.phase = "download-dir"
        request.page = 0
        request.download_dirs = download_dirs
        request.pending_download_mode = download_mode
        request.pending_download_context = context
        request.pending_no_exists = no_exists
        self._post_download_dirs_message(
            request=request,
            channel=channel,
            source=source,
            userid=userid,
            original_message_id=original_message_id,
            original_chat_id=original_chat_id,
        )
        return True

    def _handle_download_dir_selection(
            self,
            request: PendingMediaInteraction,
            page_index: Optional[int],
            channel: NotificationChannel,
            source: str,
            userid: Union[str, int],
            username: str,
    ) -> None:
        """
        处理下载目录阶段的序号输入，并继续执行挂起的下载动作。
        """
        if request.phase != "download-dir":
            self._post_invalid_input(
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            )
            return

        page_items, page, _ = self._page_items(
            items=request.download_dirs,
            page=request.page,
            page_size=self._page_size(request.channel),
        )
        request.page = page
        if not page_index or page_index < 1 or page_index > len(page_items):
            self._post_invalid_input(
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            )
            return

        download_dir = page_items[page_index - 1]
        if self._is_auto_download_dir(download_dir):
            self._execute_pending_download(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                save_path=None,
            )
            return

        save_path = download_dir.save_path or download_dir.download_path
        if not save_path:
            self._post_invalid_input(
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                title="下载目录配置无效！",
            )
            return
        self._execute_pending_download(
            request=request,
            channel=channel,
            source=source,
            userid=userid,
            username=username,
            save_path=save_path,
        )

    def _execute_pending_download(
            self,
            request: PendingMediaInteraction,
            channel: NotificationChannel,
            source: str,
            userid: Union[str, int],
            username: str,
            save_path: Optional[str],
    ) -> None:
        """
        使用用户确认的下载目录执行单资源下载或自动择优下载。
        """
        download_mode = request.pending_download_mode
        if download_mode == "single" and request.pending_download_context:
            context = request.pending_download_context
            self._restore_torrent_phase(request)
            DownloadChain().download_single(
                context,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                save_path=save_path,
            )
            return

        if download_mode == "auto":
            cache_list = list(request.items or [])
            no_exists = request.pending_no_exists
            self._restore_torrent_phase(request)
            self._auto_download(
                request=request,
                cache_list=cache_list,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                no_exists=no_exists,
                save_path=save_path,
            )
            return

        self._restore_torrent_phase(request)
        self._post_invalid_input(
            channel=channel,
            source=source,
            userid=userid,
            username=username,
            title="下载操作已失效，请重新选择资源",
        )

    @staticmethod
    def _restore_torrent_phase(request: PendingMediaInteraction) -> None:
        """
        下载动作完成或失效后恢复到资源列表阶段，便于用户继续选择其它资源。
        """
        request.phase = "torrent"
        request.page = request.pending_torrent_page
        request.download_dirs = []
        request.pending_download_mode = None
        request.pending_download_context = None
        request.pending_no_exists = None
        request.pending_torrent_page = 0

    def _auto_download(
            self,
            request: PendingMediaInteraction,
            cache_list: List[Context],
            channel: NotificationChannel,
            source: str,
            userid: Union[str, int],
            username: str,
            no_exists: Optional[Dict[Union[int, str], Dict[int, NotExistMediaInfo]]] = None,
            save_path: Optional[str] = None,
    ) -> None:
        """
        自动择优下载当前资源列表，并在未完成时补建订阅。
        """
        downloadchain = DownloadChain()
        if no_exists is None:
            exist_flag, no_exists = downloadchain.get_no_exists_info(
                meta=request.meta,
                mediainfo=request.current_media,
            )
            if exist_flag:
                no_exists = self._get_noexits_info(request.meta, request.current_media)

        downloads, lefts = downloadchain.batch_download(
            contexts=cache_list,
            no_exists=no_exists,
            save_path=save_path,
            channel=channel,
            source=source,
            userid=userid,
            username=username,
        )
        if downloads and not lefts:
            logger.info("%s 下载完成", request.current_media.title_year)
            return

        logger.info("%s 未下载未完整，添加订阅 ...", request.current_media.title_year)
        if downloads and request.current_media.type == MediaType.TV:
            note = [
                download.meta_info.begin_episode
                for download in downloads
                if download.meta_info.begin_episode
            ]
        else:
            note = None

        mp_name = (
            self.user_repository.find_name_by_bindings(
                {f"{channel.name.lower()}_userid": userid}
            )
            if channel
            else None
        )
        SubscribeChain().add(
            title=request.current_media.title,
            year=request.current_media.year,
            mtype=request.current_media.type,
            media_source=request.current_media.media_source,
            media_id=request.current_media.media_id,
            season=request.meta.begin_season,
            channel=channel,
            source=source,
            userid=str(userid),
            username=mp_name or username,
            state="R",
            note=note,
        )

    def _render_interaction(
            self,
            request: PendingMediaInteraction,
            channel: NotificationChannel,
            source: str,
            userid: Union[str, int],
            original_message_id: Optional[Union[str, int]] = None,
            original_chat_id: Optional[str] = None,
    ) -> None:
        """
        按当前阶段渲染媒体列表或资源列表。
        """
        if request.phase == "download-dir":
            self._post_download_dirs_message(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                original_message_id=original_message_id,
                original_chat_id=original_chat_id,
            )
        elif request.phase == "torrent":
            self._post_torrents_message(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                original_message_id=original_message_id,
                original_chat_id=original_chat_id,
            )
        else:
            self._post_medias_message(
                request=request,
                channel=channel,
                source=source,
                userid=userid,
                original_message_id=original_message_id,
                original_chat_id=original_chat_id,
            )

    def _post_medias_message(
            self,
            request: PendingMediaInteraction,
            channel: NotificationChannel,
            source: str,
            userid: Union[str, int],
            original_message_id: Optional[Union[str, int]] = None,
            original_chat_id: Optional[str] = None,
    ) -> None:
        """
        发送或更新媒体选择列表。
        """
        page_items, page, total_pages = self._page_items(
            items=request.items,
            page=request.page,
            page_size=self._page_size(channel),
        )
        request.page = page
        total = len(request.items)
        if self._supports_interactive_buttons(channel):
            title = f"【{request.title}】共找到{total}条相关信息，请选择操作"
            buttons = self._create_media_buttons(
                channel=channel,
                request=request,
                items=page_items,
                total=total,
                total_pages=total_pages,
            )
        else:
            if total > self._page_size(channel):
                title = f"【{request.title}】共找到{total}条相关信息，请回复对应数字选择（p: 上一页 n: 下一页）"
            else:
                title = f"【{request.title}】共找到{total}条相关信息，请回复对应数字选择"
            buttons = None

        self.post_medias_message(
            Message(
                channel=channel,
                source=source,
                title=title,
                userid=userid,
                buttons=buttons,
                original_message_id=original_message_id,
                original_chat_id=original_chat_id,
                save_history=False,
            ),
            medias=page_items,
        )

    def _post_torrents_message(
            self,
            request: PendingMediaInteraction,
            channel: NotificationChannel,
            source: str,
            userid: Union[str, int],
            original_message_id: Optional[Union[str, int]] = None,
            original_chat_id: Optional[str] = None,
    ) -> None:
        """
        发送或更新资源选择列表。
        """
        page_items, page, total_pages = self._page_items(
            items=request.items,
            page=request.page,
            page_size=self._page_size(channel),
        )
        request.page = page
        total = len(request.items)
        if self._supports_interactive_buttons(channel):
            title = f"【{request.title}】共找到{total}条相关资源，请选择下载"
            buttons = self._create_torrent_buttons(
                channel=channel,
                request=request,
                items=page_items,
                total=total,
                total_pages=total_pages,
            )
        else:
            if total > self._page_size(channel):
                title = f"【{request.title}】共找到{total}条相关资源，请回复对应数字下载（0: 自动选择 p: 上一页 n: 下一页）"
            else:
                title = f"【{request.title}】共找到{total}条相关资源，请回复对应数字下载（0: 自动选择）"
            buttons = None

        self.post_torrents_message(
            Message(
                channel=channel,
                source=source,
                title=title,
                userid=userid,
                link=self.runtime_config.resource_url,
                buttons=buttons,
                original_message_id=original_message_id,
                original_chat_id=original_chat_id,
                save_history=False,
            ),
            torrents=page_items,
        )

    def _post_download_dirs_message(
            self,
            request: PendingMediaInteraction,
            channel: NotificationChannel,
            source: str,
            userid: Union[str, int],
            original_message_id: Optional[Union[str, int]] = None,
            original_chat_id: Optional[str] = None,
    ) -> None:
        """
        发送或更新下载目录选择列表。
        """
        page_items, page, total_pages = self._page_items(
            items=request.download_dirs,
            page=request.page,
            page_size=self._page_size(channel),
        )
        request.page = page
        total = len(request.download_dirs)
        if self._supports_interactive_buttons(channel):
            title = f"【{request.title}】请选择下载目录"
            buttons = self._create_download_dir_buttons(
                channel=channel,
                request=request,
                items=page_items,
                total=total,
                total_pages=total_pages,
            )
        else:
            if total > self._page_size(channel):
                title = f"【{request.title}】请选择下载目录，请回复对应数字（p: 上一页 n: 下一页）"
            else:
                title = f"【{request.title}】请选择下载目录，请回复对应数字"
            buttons = None

        text = "\n".join(
            f"{index}. {self._format_download_dir_label(download_dir)}"
            for index, download_dir in enumerate(page_items, start=1)
        )
        self.post_message(
            Message(
                channel=channel,
                source=source,
                title=title,
                text=text,
                userid=userid,
                buttons=buttons,
                original_message_id=original_message_id,
                original_chat_id=original_chat_id,
                save_history=False,
            )
        )

    def _create_media_buttons(
            self,
            channel: NotificationChannel,
            request: PendingMediaInteraction,
            items: List[MediaInfo],
            total: int,
            total_pages: int,
    ) -> List[List[Dict[str, str]]]:
        """
        为媒体列表生成选择和翻页按钮。
        """
        buttons: List[List[Dict[str, str]]] = []
        max_text_length = ChannelCapabilityManager.get_max_button_text_length(channel)
        max_per_row = ChannelCapabilityManager.get_max_buttons_per_row(channel)

        current_row: List[Dict[str, str]] = []
        for index, media in enumerate(items, start=1):
            if max_per_row == 1:
                button_text = f"{index}. {media.title_year}"
                if len(button_text) > max_text_length:
                    button_text = button_text[: max_text_length - 3] + "..."
                buttons.append(
                    [
                        {
                            "text": button_text,
                            "callback_data": f"media:{request.request_id}:select:{index}",
                        }
                    ]
                )
                continue

            current_row.append(
                {
                    "text": f"{index}",
                    "callback_data": f"media:{request.request_id}:select:{index}",
                }
            )
            if len(current_row) == max_per_row or index == len(items):
                buttons.append(current_row)
                current_row = []

        if total > self._page_size(channel):
            buttons.extend(self._navigation_buttons(request, total_pages))
        return buttons

    def _create_torrent_buttons(
            self,
            channel: NotificationChannel,
            request: PendingMediaInteraction,
            items: List[Context],
            total: int,
            total_pages: int,
    ) -> List[List[Dict[str, str]]]:
        """
        为资源列表生成下载和翻页按钮。
        """
        buttons: List[List[Dict[str, str]]] = [
            [
                {
                    "text": "🤖 自动选择下载",
                    "callback_data": f"media:{request.request_id}:download:0",
                }
            ]
        ]
        max_text_length = ChannelCapabilityManager.get_max_button_text_length(channel)
        max_per_row = ChannelCapabilityManager.get_max_buttons_per_row(channel)

        current_row: List[Dict[str, str]] = []
        for index, context in enumerate(items, start=1):
            torrent = context.torrent_info
            if max_per_row == 1:
                button_text = f"{index}. {torrent.site_name} - {torrent.seeders}↑"
                if len(button_text) > max_text_length:
                    button_text = button_text[: max_text_length - 3] + "..."
                buttons.append(
                    [
                        {
                            "text": button_text,
                            "callback_data": f"media:{request.request_id}:download:{index}",
                        }
                    ]
                )
                continue

            current_row.append(
                {
                    "text": f"{index}",
                    "callback_data": f"media:{request.request_id}:download:{index}",
                }
            )
            if len(current_row) == max_per_row or index == len(items):
                buttons.append(current_row)
                current_row = []

        if total > self._page_size(channel):
            buttons.extend(self._navigation_buttons(request, total_pages))
        return buttons

    def _create_download_dir_buttons(
            self,
            channel: NotificationChannel,
            request: PendingMediaInteraction,
            items: List[DownloadDirectory],
            total: int,
            total_pages: int,
    ) -> List[List[Dict[str, str]]]:
        """
        为下载目录列表生成选择和翻页按钮。
        """
        buttons: List[List[Dict[str, str]]] = []
        max_text_length = ChannelCapabilityManager.get_max_button_text_length(channel)
        max_per_row = ChannelCapabilityManager.get_max_buttons_per_row(channel)

        current_row: List[Dict[str, str]] = []
        for index, download_dir in enumerate(items, start=1):
            if max_per_row == 1:
                button_text = f"{index}. {self._format_download_dir_label(download_dir)}"
                if len(button_text) > max_text_length:
                    button_text = button_text[: max_text_length - 3] + "..."
                buttons.append(
                    [
                        {
                            "text": button_text,
                            "callback_data": f"media:{request.request_id}:download-dir:{index}",
                        }
                    ]
                )
                continue

            current_row.append(
                {
                    "text": f"{index}",
                    "callback_data": f"media:{request.request_id}:download-dir:{index}",
                }
            )
            if len(current_row) == max_per_row or index == len(items):
                buttons.append(current_row)
                current_row = []

        if total > self._page_size(channel):
            buttons.extend(self._navigation_buttons(request, total_pages))
        return buttons

    def _has_next_page(self, request: PendingMediaInteraction) -> bool:
        """
        判断当前视图是否还有下一页。
        """
        _, page, total_pages = self._page_items(
            items=self._get_current_phase_items(request),
            page=request.page,
            page_size=self._page_size(request.channel),
        )
        return page < total_pages - 1

    @staticmethod
    def _get_current_phase_items(request: PendingMediaInteraction) -> List[Any]:
        """
        获取当前阶段用于分页的数据列表。
        """
        if request.phase == "download-dir":
            return request.download_dirs
        return request.items

    @staticmethod
    def _navigation_buttons(
            request: PendingMediaInteraction,
            total_pages: int,
    ) -> List[List[Dict[str, str]]]:
        """
        按当前页状态生成上一页和下一页按钮。
        """
        buttons: List[List[Dict[str, str]]] = []
        nav_row: List[Dict[str, str]] = []
        if request.page > 0:
            nav_row.append(
                {
                    "text": "⬅️ 上一页",
                    "callback_data": f"media:{request.request_id}:page-prev",
                }
            )
        if request.page < total_pages - 1:
            nav_row.append(
                {
                    "text": "下一页 ➡️",
                    "callback_data": f"media:{request.request_id}:page-next",
                }
            )
        if nav_row:
            buttons.append(nav_row)
        return buttons

    @staticmethod
    def _page_items(
            items: List[Any],
            page: int,
            page_size: int,
    ) -> Tuple[List[Any], int, int]:
        """
        返回当前页数据，并把页码限制在有效范围内。
        """
        total_pages = max(1, math.ceil(len(items) / page_size)) if page_size else 1
        page = min(max(0, page), total_pages - 1)
        start = page * page_size
        end = start + page_size
        return items[start:end], page, total_pages

    @classmethod
    def _get_download_dirs(cls, media_info: Optional[MediaInfo] = None) -> List[DownloadDirectory]:
        """
        获取可供消息交互选择的下载目录。
        """
        dir_infos = [
            dir_info
            for dir_info in DirectoryHelper().get_download_dirs()
            if dir_info.download_path
        ]
        download_dirs = [
            DownloadDirectory(
                name=dir_info.name,
                storage=dir_info.storage or "local",
                download_path=dir_info.download_path,
                save_path=FileURI(
                    storage=dir_info.storage or "local",
                    path=dir_info.download_path,
                ).uri,
                priority=dir_info.priority,
                media_type=dir_info.media_type,
                media_category=dir_info.media_category,
                media_category_id=dir_info.media_category_id,
            )
            for dir_info in dir_infos
            if cls._match_download_dir_media(dir_info, media_info)
        ]
        if not download_dirs:
            return []
        if len(download_dirs) == 1:
            return download_dirs
        return [cls._build_auto_download_dir(), *download_dirs]

    @classmethod
    def _build_auto_download_dir(cls) -> DownloadDirectory:
        """
        构造自动匹配下载目录选项。
        """
        return DownloadDirectory(
            name=cls._auto_download_dir_name,
            storage="local",
            priority=-1,
        )

    @classmethod
    def _is_auto_download_dir(cls, download_dir: DownloadDirectory) -> bool:
        """
        判断是否为自动匹配下载目录选项。
        """
        return (
                download_dir.name == cls._auto_download_dir_name
                and not download_dir.download_path
                and not download_dir.save_path
        )

    @staticmethod
    def _match_download_dir_media(
            dir_info: TransferDirectoryConf,
            media_info: Optional[MediaInfo],
    ) -> bool:
        """
        判断下载目录是否适用于当前媒体。
        """
        return DirectoryHelper().matches_media(dir_info, media_info)

    @staticmethod
    def _format_download_dir_label(download_dir: DownloadDirectory) -> str:
        """
        格式化下载目录展示名称，优先显示用户配置的目录名称。
        """
        save_path = download_dir.save_path or download_dir.download_path or ""
        name = download_dir.name or save_path or "下载目录"
        if save_path and name != save_path:
            return f"{name} ({save_path})"
        return name

    def _page_size(self, channel: Optional[NotificationChannel]) -> int:
        """
        按渠道交互能力选择分页大小。
        """
        return (
            self._button_page_size
            if self._supports_interactive_buttons(channel)
            else self._text_page_size
        )

    @staticmethod
    def _supports_interactive_buttons(channel: Optional[NotificationChannel]) -> bool:
        """
        判断渠道是否同时支持按钮展示与按钮回调。
        """
        return bool(
            channel
            and ChannelCapabilityManager.supports_buttons(channel)
            and ChannelCapabilityManager.supports_callbacks(channel)
        )

    @staticmethod
    def _build_no_exists_messages(
            mediainfo: MediaInfo,
            no_exists: Optional[Dict[Union[int, str], Dict[int, NotExistMediaInfo]]],
            show_missing_only: bool,
    ) -> List[str]:
        """
        将缺失集信息转换为可发送的文案。
        """
        if not no_exists:
            return []
        media_source, media_id = resolve_media_identity(media=mediainfo)
        mediakey = build_media_key(media_source, media_id)
        season_map = no_exists.get(mediakey) or {}
        if show_missing_only:
            return [
                f"第 {sea} 季缺失 {episode_rules.compact_numbers(no_exist.episodes) if no_exist.episodes else no_exist.total_episode} 集"
                for sea, no_exist in season_map.items()
            ]
        return [
            f"第 {sea} 季总 {no_exist.total_episode} 集"
            for sea, no_exist in season_map.items()
        ]

    def _should_auto_download(self, userid: Union[str, int]) -> bool:
        """
        判断当前用户是否命中自动下载名单。
        """
        auto_download_user = self.runtime_config.auto_download_user
        return bool(
            auto_download_user
            and (
                    auto_download_user == "all"
                    or any(userid == user for user in auto_download_user.split(","))
            )
        )

    def _post_invalid_input(
            self,
            channel: NotificationChannel,
            source: str,
            userid: Union[str, int],
            username: Optional[str],
            title: str = "输入有误！",
    ) -> None:
        """
        发送统一的非法输入提示。
        """
        self.post_message(
            Message(
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                title=title,
                save_history=False,
            )
        )
