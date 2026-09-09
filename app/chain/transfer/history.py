"""手动和远程整理历史入口及结果消息。"""

import re
from pathlib import Path
from typing import Any, List, Optional, Tuple, Union, cast

from app.chain.media import MediaChain
from app.chain.transfer.contract import _TransferOwnerBase
from app.domain.context import MediaInfo, MusicAlbumInfo, MusicInfo
from app.domain.meta.metabase import MetaBase
from app.runtime.log import logger
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
from app.schemas.workflow import FileItem


def _recognize_manual_media(
    *,
    media_source: MediaSource,
    media_id: str,
    mtype: Optional[MediaType],
    music_type: Optional[str],
    episode_group: Optional[str],
    music_release_regions: Optional[list[str]],
    music_release_scripts: Optional[list[str]],
) -> Optional[Union[MediaInfo, MusicInfo, MusicAlbumInfo]]:
    """识别手动指定的媒体，并为音乐专辑保留完整曲目表。"""
    if mtype == MediaType.MUSIC and music_type == MUSIC_ENTITY_ALBUM:
        album = MediaChain().get_music_album(
            media_source=media_source,
            media_id=media_id,
            music_release_regions=music_release_regions,
            music_release_scripts=music_release_scripts,
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
    """唯一持有手动历史、重整命令和通知公开入口。"""

    def _run_manual_transfer_request(
            self,
            transfer_kwargs: dict[str, Any],
            selected_fileitems: Optional[list[FileItem]],
            report_results: bool = False,
            skip_success: bool = False,
    ) -> Tuple[bool, Union[str, dict[str, Any]]]:
        """批次、回执或成功记录过滤走内部入口，普通请求保持公开签名兼容。"""
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
            userid: Union[str, int] = None,
            source: Optional[str] = None,
    ):
        """
        远程重新整理，参数为历史记录 ID，或媒体来源、原生 ID 与类型。
        """
        from app.runtime.errors import public_error_message

        def args_error():
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
        # 历史记录ID
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
        """
        logger.info(f"手动整理：{fileitem.path} ...")
        explicit_identity = media_source is not None or media_id is not None
        if explicit_identity and (not media_source or not media_id):
            return False, "手动整理需要同时提供 media_source 和 media_id"
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
            )
            if not mediainfo:
                return (
                    False,
                    "未识别到媒体信息，请检查媒体来源和媒体 ID 后重试",
                )
            if media_source and not isinstance(mediainfo, (MusicInfo, MusicAlbumInfo)):
                mediainfo.scrape_source = media_source
            if not isinstance(mediainfo, (MusicInfo, MusicAlbumInfo)):
                self.obtain_images(mediainfo=mediainfo)

            transfer_kwargs.update(mediainfo=mediainfo, media_id=media_id)

        state, errmsg = self._run_manual_transfer_request(
            transfer_kwargs, selected_fileitems, report_results, skip_success,
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
    ):
        """
        发送入库成功的消息
        :param meta: 文件元数据
        :param mediainfo: 识别的媒体信息
        :param transferinfo: 文件整理信息
        :param season_episode: 已入库季集文本
        :param episodes_info: 当前季的全部集信息
        :param username: 用户名
        """
        self.post_message(
            Message(
                mtype=MessageType.Organize,
                ctype=ContentType.OrganizeSuccess,
                image=mediainfo.get_message_image(),
                username=username,
                link=self.runtime_config.history_url,
            ),
            meta=meta,
            mediainfo=mediainfo,
            transferinfo=transferinfo,
            season_episode=season_episode,
            episodes_info=episodes_info,
            username=username,
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
            self, download_hash: str, downloader: str, transfer_exclude_words
    ) -> bool:
        """
        检查是否可以删除种子文件
        :param download_hash: 种子Hash
        :param downloader: 下载器名称
        :param transfer_exclude_words: 整理屏蔽词
        :return: 如果可以删除返回True，否则返回False
        """
        try:
            # 获取种子信息
            torrents = self.list_torrents(hashs=download_hash, downloader=downloader)
            if not torrents:
                return False

            # 未下载完成
            if torrents[0].progress < 100:
                return False

            # 获取种子文件列表
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
