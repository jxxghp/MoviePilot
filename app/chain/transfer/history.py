"""
手动和远程整理历史入口及结果消息。
"""

import re
from copy import deepcopy
from pathlib import Path
from typing import Any, List, Optional, Tuple, Union, cast

from app.application.audio import use_music_cue
from app.application.transfer.models import TransferTask
from app.application.transfer.notifications import (
    TransferSuccessNotification,
    build_transfer_success_group_key,
)
from app.chain.media import MediaChain
from app.chain.transfer.contract import _TransferOwnerBase
from app.domain import episode as episode_rules
from app.domain.context import MediaInfo, MusicAlbumInfo, MusicInfo
from app.domain.meta.metabase import MetaBase
from app.runtime.log import logger
from app.runtime.loop import main_loop_registry
from app.schemas.file import FileItem
from app.schemas.message import Message
from app.schemas.tmdb import TmdbEpisode
from app.schemas.transfer import EpisodeFormat, TransferInfo
from app.schemas.types import (
    MUSIC_ENTITY_ALBUM,
    ContentType,
    MediaSource,
    MediaType,
    MessageType,
    NotificationChannel,
)


def _recognize_manual_media(
    *,
    media_source: MediaSource,
    media_id: str,
    mtype: Optional[MediaType],
    music_type: Optional[str],
    episode_group: Optional[str],
    music_release_regions: Optional[list[str]],
    music_release_scripts: Optional[list[str]],
    musicbrainz_release_id: Optional[str] = None,
) -> Optional[Union[MediaInfo, MusicInfo, MusicAlbumInfo]]:
    """
    识别手动指定的媒体，并为音乐专辑保留完整曲目表。
    """
    if mtype == MediaType.MUSIC and music_type == MUSIC_ENTITY_ALBUM:
        album = MediaChain().get_music_album(
            media_source=media_source,
            media_id=media_id,
            music_release_regions=music_release_regions,
            music_release_scripts=music_release_scripts,
            **({"musicbrainz_release_id": musicbrainz_release_id} if musicbrainz_release_id is not None else {}),
        )
        return album
    return MediaChain().recognize_media(
        media_source=media_source,
        media_id=media_id,
        music_type=music_type,
        mtype=mtype,
        episode_group=episode_group,
    )


class TransferHistoryOwner(_TransferOwnerBase):
    """
    唯一持有手动历史、重整命令和通知公开入口。
    """

    def _run_manual_transfer_request(
            self,
            transfer_kwargs: dict[str, Any],
            selected_fileitems: Optional[list[FileItem]],
            report_results: bool = False,
            skip_success: bool = False,
            music_cue_enable: Optional[bool] = None,
    ) -> Tuple[bool, Union[str, dict[str, Any]]]:
        """
        隔离本次 CUE 策略，批次与普通请求均保持既有整理入口兼容。
        """
        with use_music_cue(music_cue_enable):
            if selected_fileitems is not None or report_results or skip_success:
                return cast(
                    Tuple[bool, Union[str, dict[str, Any]]],
                    self._execute_transfer(
                        **transfer_kwargs,
                        selected_fileitems=selected_fileitems,
                        report_results=report_results,
                        skip_success=skip_success,
                    ),
                )
            return cast(
                Tuple[bool, Union[str, dict[str, Any]]],
                self.do_transfer(**transfer_kwargs),
            )

    def remote_transfer(
            self,
            arg_str: str,
            channel: NotificationChannel,
            userid: Optional[Union[str, int]] = None,
            source: Optional[str] = None,
    ) -> None:
        """
        远程重新整理，参数为历史记录 ID，或媒体来源、原生 ID 与类型。
        """
        from app.runtime.errors import public_error_message

        def args_error() -> None:
            """
            向原交互入口提示整理历史参数无效，不发起新的整理操作。
            """
            self.post_message(
                Message(
                    channel=channel,
                    source=source,
                    title="请输入正确的命令格式：/redo [id] 或 "
                          "/redo [id] [media_source]|[media_id]|[类型]，"
                          "[id] 为整理记录编号",
                    userid=userid,
                    save_history=False,
                )
            )

        if not arg_str:
            args_error()
            return
        arg_strs = str(arg_str).split()
        if len(arg_strs) not in (1, 2):
            args_error()
            return
        logid = arg_strs[0]
        if not logid.isdigit():
            args_error()
            return
        if len(arg_strs) == 1:
            state, errmsg = self.redo_transfer_history(int(logid))
            if not state:
                self.post_message(
                    Message(
                        channel=channel,
                        title="手动整理失败",
                        source=source,
                        text=public_error_message(errmsg, context="transfer"),
                        userid=userid,
                        link=self.runtime_config.history_url,
                        save_history=False,
                    )
                )
            return
        # 显式媒体身份固定为来源、原生 ID 和媒体类型三个字段。
        id_strs = arg_strs[1].split("|")
        if len(id_strs) != 3:
            args_error()
            return
        media_source, media_id, type_str = id_strs
        try:
            normalized_source = MediaSource(media_source)
        except ValueError:
            args_error()
            return
        if not type_str or type_str not in [
            MediaType.MOVIE.value,
            MediaType.TV.value,
            MediaType.MUSIC.value,
        ]:
            args_error()
            return
        state, errmsg = self._re_transfer(
            logid=int(logid),
            mtype=MediaType(type_str),
            media_source=normalized_source,
            media_id=media_id,
        )
        if not state:
            self.post_message(
                Message(
                    channel=channel,
                    title="手动整理失败",
                    source=source,
                    text=public_error_message(errmsg, context="transfer"),
                    userid=userid,
                    link=self.runtime_config.history_url,
                    save_history=False,
                )
            )
            return

    def manual_transfer(
            self,
            fileitem: FileItem,
            target_storage: Optional[str] = None,
            target_path: Optional[Path] = None,
            media_source: Optional[MediaSource] = None,
            media_id: Optional[str] = None,
            mtype: Optional[MediaType] = None,
            season: Optional[int] = None,
            episode_group: Optional[str] = None,
            transfer_type: Optional[str] = None,
            epformat: Optional[EpisodeFormat] = None,
            min_filesize: Optional[int] = 0,
            scrape: Optional[bool] = None,
            library_type_folder: Optional[bool] = None,
            library_category_folder: Optional[bool] = None,
            force: Optional[bool] = False,
            background: Optional[bool] = False,
            downloader: Optional[str] = None,
            download_hash: Optional[str] = None,
            preview: Optional[bool] = False,
            sync_extra_files: Optional[bool] = True,
            cleanup_dest_fileitem: Optional[FileItem] = None,
            reorganize: Optional[bool] = False,
            music_type: Optional[str] = None,
            music_release_regions: Optional[list[str]] = None,
            music_release_scripts: Optional[list[str]] = None,
            selected_fileitems: Optional[list[FileItem]] = None,
            report_results: bool = False,
            skip_success: bool = False,
            musicbrainz_release_id: Optional[str] = None,
            music_cue_enable: Optional[bool] = None,
    ) -> Tuple[bool, Union[str, dict[str, Any]]]:
        """
        手动整理，支持复杂条件，带进度显示

        :param fileitem: 文件项
        :param target_storage: 目标存储
        :param target_path: 目标路径
        :param media_source: 媒体数据源
        :param media_id: 数据源原生 ID，必须与 media_source 成对提供
        :param mtype: 媒体类型
        :param season: 季度
        :param episode_group: 剧集组
        :param transfer_type: 整理类型
        :param epformat: 剧集格式
        :param min_filesize: 最小文件大小(MB)
        :param scrape: 是否刮削元数据
        :param library_type_folder: 是否按类型建立目录
        :param library_category_folder: 是否按类别建立目录
        :param force: 是否强制整理
        :param background: 是否后台运行
        :param downloader: 下载器名称
        :param download_hash: 下载任务哈希
        :param preview: 是否仅预览
        :param reorganize: 是否清理已有成功记录后重新整理
        :param sync_extra_files: 是否同步整理同媒体附加文件
        :param cleanup_dest_fileitem: 确认存在待整理任务后需要清理的旧目标文件
        :param music_type: 音乐实体类型；为保持位置参数兼容，必须追加在签名末尾
        :param music_release_regions: 本次音乐整理的发行地区优先级，空值继承系统设置
        :param music_release_scripts: 本次音乐整理的文字字形优先级，空值继承系统设置
        :param selected_fileitems: 前端显式选中的批量文件
        :param report_results: 返回实际阶段回执，后台接收不表示入库完成
        :param skip_success: 预览和执行均跳过成功记录，优先于强制整理和重整
        :param musicbrainz_release_id: 指定属于 media_id 发行组的具体 MusicBrainz 发行版
        :param music_cue_enable: 本次是否识别 CUE，空值继承系统设置，不修改全局开关
        """
        logger.info(f"手动整理：{fileitem.path} ...")
        explicit_identity = media_source is not None or media_id is not None
        if explicit_identity and (not media_source or not media_id):
            return False, "手动整理需要同时提供 media_source 和 media_id"
        if musicbrainz_release_id is not None:
            if (media_source != MediaSource.MusicBrainz or not media_id or music_type != MUSIC_ENTITY_ALBUM
                    or mtype not in (None, MediaType.MUSIC)):
                return False, "指定音乐发行版需要 MusicBrainz 专辑身份"
            if any(item.storage != "local" for item in selected_fileitems or [fileitem]):
                return False, "指定发行版需要读取本地音频并对齐曲目，请先将所选音乐下载到本地"
            mtype = MediaType.MUSIC
        transfer_kwargs: dict[str, Any] = dict(
            fileitem=fileitem,
            target_storage=target_storage,
            target_path=target_path,
            media_source=media_source,
            mtype=mtype,
            transfer_type=transfer_type,
            season=season,
            epformat=epformat,
            min_filesize=min_filesize,
            scrape=scrape,
            library_type_folder=library_type_folder,
            library_category_folder=library_category_folder,
            force=force,
            background=background,
            manual=True,
            downloader=downloader,
            download_hash=download_hash,
            preview=preview,
            reorganize=reorganize,
            sync_extra_files=sync_extra_files,
            cleanup_dest_fileitem=cleanup_dest_fileitem,
            music_release_regions=music_release_regions,
            music_release_scripts=music_release_scripts,
        )
        if media_source and media_id:
            # 有输入媒体ID时预先识别，音乐与影视统一走 recognize_media 按类型分发
            mediainfo = _recognize_manual_media(
                media_source=media_source,
                media_id=media_id,
                mtype=mtype,
                music_type=music_type,
                episode_group=episode_group,
                music_release_regions=music_release_regions,
                music_release_scripts=music_release_scripts,
                musicbrainz_release_id=musicbrainz_release_id,
            )
            if not mediainfo:
                return (
                    False,
                    "未能读取所选发行版的曲目，或该版不属于所选专辑，请核对发行 ID 后重试"
                    if musicbrainz_release_id is not None else "未识别到媒体信息，请检查媒体来源和媒体 ID 后重试",
                )
            if media_source and not isinstance(mediainfo, (MusicInfo, MusicAlbumInfo)):
                mediainfo.scrape_source = media_source
            if not isinstance(mediainfo, (MusicInfo, MusicAlbumInfo)):
                self.obtain_images(mediainfo=mediainfo)

            transfer_kwargs.update(mediainfo=mediainfo, media_id=media_id)

        state, errmsg = self._run_manual_transfer_request(
            transfer_kwargs, selected_fileitems, report_results, skip_success, music_cue_enable,
        )
        if explicit_identity and state:
            logger.info(f"{fileitem.path} 整理请求处理完成")
            return True, errmsg if preview or report_results else ""
        return state, errmsg

    def send_transfer_message(
            self,
            meta: MetaBase,
            mediainfo: Union[MediaInfo, MusicInfo],
            transferinfo: TransferInfo,
            season_episode: Optional[str] = None,
            episodes_info: Optional[List[TmdbEpisode]] = None,
            username: Optional[str] = None,
            buttons: Optional[list[list[dict[str, Any]]]] = None,
    ) -> None:
        """
        通过入库通知模板发送单文件或聚合后的成功结果，不预填标题和正文。

        :param meta: 文件元数据
        :param mediainfo: 识别的媒体信息
        :param transferinfo: 文件整理信息
        :param season_episode: 已入库季集文本
        :param episodes_info: 当前季的全部集信息
        :param username: 用户名
        :param buttons: 批次通知使用的历史入口
        """
        message = Message(
            mtype=MessageType.Organize,
            ctype=ContentType.OrganizeSuccess,
            image=mediainfo.get_message_image(),
            username=username,
            link=self.runtime_config.history_url,
            buttons=buttons,
        )
        self.post_message(
            message,
            meta=meta,
            mediainfo=mediainfo,
            transferinfo=transferinfo,
            season_episode=season_episode,
            episodes_info=episodes_info,
            username=username,
        )

    def notify_successful_transfer(self, task: TransferTask, transferinfo: TransferInfo) -> None:
        """
        全局开启时聚合成功结果，目录关闭通知和前台手动整理仍保持静默。
        """
        if not transferinfo.target_item or not transferinfo.need_notify or (task.manual and not task.background):
            return
        assert task.meta is not None and task.mediainfo is not None
        if self.runtime_config.transfer_success_notification_aggregation:
            self.queue_successful_transfer_notification(task, transferinfo)

    def queue_successful_transfer_notification(self, task: TransferTask, transferinfo: TransferInfo) -> None:
        """
        仅缓冲成功展示快照，历史、完成事件和种子状态仍逐任务结算。
        """
        assert task.meta is not None and task.mediainfo is not None
        notification = TransferSuccessNotification(
            meta=deepcopy(task.meta),
            mediainfo=deepcopy(task.mediainfo),
            transferinfo=transferinfo.model_copy(deep=True),
            episodes_info=deepcopy(task.episodes_info),
            username=task.username,
        )
        try:
            self.success_notification_aggregator.schedule(
                group_key=build_transfer_success_group_key(task, transferinfo),
                notification=notification,
                callback=self._send_transfer_success_notifications,
                loop=None if task.transfer_batch_id else main_loop_registry.require(),
                defer_until=task.transfer_batch_id,
            )
        except Exception as err:
            logger.error(f"加入整理成功通知聚合缓冲失败，将立即发送：{err}")
            self._send_transfer_success_notifications([notification])

    def _send_transfer_success_notifications(self, notifications: list[TransferSuccessNotification]) -> None:
        """
        合并真实目标文件及大小，并以专辑、电影或季为模板主体发送批次通知。

        未配置访问域名时不附带整理记录按钮，避免通知渠道收到空链接。
        """
        if not notifications:
            return
        first = notifications[0]
        files: dict[str, int] = {}
        for notice in notifications:
            result = notice.transferinfo
            paths = result.file_list_new or ([result.target_item.path] if result.target_item and result.target_item.path else [])
            size, remainder = divmod(result.total_size or 0, len(paths) or 1)
            for index, path in enumerate(paths):
                files[path] = size + (1 if index < remainder else 0)
        merged = first.transferinfo.model_copy(update={
            "file_list_new": list(files), "file_count": len(files), "total_size": sum(files.values()),
        })
        media = first.mediainfo
        if isinstance(media, MusicInfo) and media.album:
            media = deepcopy(media)
            media.music_type = MUSIC_ENTITY_ALBUM
        episodes = sorted({
            episode for notice in notifications for episode in notice.meta.episode_list
        })
        season_episode = first.meta.season if media.type == MediaType.TV else None
        if season_episode and episodes:
            season_episode = f"{season_episode} {episode_rules.format_ranges(episodes)}"
        self.send_transfer_message(
            meta=first.meta, mediainfo=media, transferinfo=merged,
            season_episode=season_episode, episodes_info=first.episodes_info, username=first.username,
            buttons=[[{"text": "查看整理记录", "url": self.runtime_config.history_url}]]
            if len(files) > 1 and self.runtime_config.history_url else None,
        )

    @staticmethod
    def _is_blocked_by_exclude_words(
            file_path: str,
            exclude_words: list[str],
    ) -> bool:
        """
        检查文件是否被整理屏蔽词阻止处理

        :param file_path: 文件路径
        :param exclude_words: 整理屏蔽词列表
        :return: 如果被屏蔽返回True，否则返回False
        """
        if not exclude_words:
            return False

        for keyword in exclude_words:
            if keyword and re.search(r"%s" % keyword, file_path, re.IGNORECASE):
                logger.warn(f"{file_path} 命中屏蔽词 {keyword}")
                return True
        return False

    def _can_delete_torrent(
            self, download_hash: str, downloader: str, transfer_exclude_words: list[str]
    ) -> bool:
        """
        检查是否可以删除种子文件

        :param download_hash: 种子Hash
        :param downloader: 下载器名称
        :param transfer_exclude_words: 整理屏蔽词
        :return: 如果可以删除返回True，否则返回False
        """
        try:
            torrents = self.list_torrents(hashs=download_hash, downloader=downloader)
            if not torrents:
                return False

            if torrents[0].progress < 100:
                return False

            torrent_files = self.torrent_files(download_hash, downloader)
            if not torrent_files:
                return False

            if not isinstance(torrent_files, list):
                torrent_files = torrent_files.data

            # 检查是否有媒体文件未被屏蔽且存在
            save_path = torrents[0].path.parent
            for file in torrent_files:
                file_path = save_path / file.name
                # 如果存在未被屏蔽的媒体文件，则不删除种子
                if (
                        file_path.suffix in self._allowed_exts
                        and not self._is_blocked_by_exclude_words(
                            file_path.as_posix(), transfer_exclude_words
                        )
                        and file_path.exists()
                ):
                    return False

            # 所有媒体文件都被屏蔽或不存在，可以删除种子
            return True

        except Exception as e:
            logger.error(f"检查种子 {download_hash} 是否需要删除失败：{e}")
            return False
