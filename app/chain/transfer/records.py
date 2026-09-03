"""整理记录匹配、文件键与手动历史辅助能力。"""

from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple, TypeVar, Union, cast

from app.application.classification.reference import (
    apply_persisted_classification_snapshot,
    persisted_classification_snapshot,
)
from app.application.history import (
    DownloadFileSnapshot,
    DownloadHistoryQueryPort,
    DownloadHistorySnapshot,
    TransferHistoryRepository,
    TransferHistorySnapshot,
    clear_transfer_failures,
    resolve_history,
)
from app.application.transfer.execution import (
    TransferExecutionCommand,
    TransferExecutionRepository,
)
from app.chain._contracts import TransferMixinHost
from app.chain.storage import StorageChain
from app.chain.subscribe.facade import SubscribeChain
from app.chain.transfer.contract import _TransferOwnerBase
from app.domain.context import MediaInfo, MusicInfo
from app.domain.meta.metabase import MetaBase
from app.foundation import text as text_tools
from app.schemas.types import (
    MediaType,
)
from app.schemas.workflow import FileItem

from .retry import _request_durable_transfer_retry

TransferMediaT = TypeVar("TransferMediaT", MediaInfo, MusicInfo)


def apply_download_history_classification(
    media: TransferMediaT,
    history: DownloadHistorySnapshot,
) -> TransferMediaT:
    """按下载发生时的分类标量恢复媒体，不读取或解析当前活动策略。"""
    snapshot = persisted_classification_snapshot(
        category_id=getattr(history, "media_category_id", None),
        category_path=getattr(history, "media_category", None),
        rule_id=getattr(history, "classification_rule_id", None),
        policy_revision=getattr(history, "classification_policy_revision", None),
        source=getattr(history, "classification_source", None),
    )
    restored = apply_persisted_classification_snapshot(media, snapshot)
    return cast(TransferMediaT, restored or media)

# 字幕文件常见语言、默认和强制标记；匹配主视频时只剥离这些字幕专属尾缀。
SUBTITLE_STEM_TAGS = {
    "cc",
    "chi",
    "chs",
    "cht",
    "cn",
    "default",
    "en",
    "eng",
    "english",
    "forced",
    "gb",
    "gb2312",
    "hk",
    "ja",
    "jap",
    "japanese",
    "jp",
    "jpn",
    "sc",
    "sdh",
    "tc",
    "zh",
    "zh-cn",
    "zh-hans",
    "zh-hant",
    "zh-tw",
    "zh_cn",
    "zh_hans",
    "zh_hant",
    "zh_tw",
    "zho",
    "中英",
    "中字",
    "双语",
    "简中",
    "简体",
    "繁中",
    "繁体",
}


class HistoryMatchMixin(_TransferOwnerBase):
    """提供下载历史定位与媒体身份一致性判定。"""

    __mixin_host_protocol__ = TransferMixinHost

    @staticmethod
    def _match_download_file(
            download_file: DownloadFileSnapshot,
            file_path: Path,
            save_path: Path,
    ) -> bool:
        """
        判断下载文件记录是否明确对应当前文件。
        """
        if download_file.fullpath == file_path.as_posix():
            return True

        filepath = download_file.filepath
        if not filepath:
            return False

        try:
            return (save_path / Path(filepath)).as_posix() == file_path.as_posix()
        except (TypeError, ValueError):
            return False

    def _resolve_history_from_download_files(
            self,
            repository: DownloadHistoryQueryPort,
            download_files: List[DownloadFileSnapshot],
            file_path: Optional[Path] = None,
            save_path: Optional[Path] = None,
    ) -> Optional[DownloadHistorySnapshot]:
        """
        从下载文件记录中解析唯一的下载历史。
        """
        if file_path and save_path:
            download_files = [
                download_file
                for download_file in download_files
                if self._match_download_file(
                    download_file=download_file,
                    file_path=file_path,
                    save_path=save_path,
                )
            ]

        download_hashes = {
            download_file.download_hash
            for download_file in download_files
            if download_file.download_hash
        }
        if len(download_hashes) == 1:
            return repository.get_by_hash(next(iter(download_hashes)))
        return None

    def _resolve_download_history(
            self,
            repository: DownloadHistoryQueryPort,
            file_path: Path,
            bluray_dir: bool = False,
            download_hash: Optional[str] = None,
    ) -> Optional[DownloadHistorySnapshot]:
        """
        根据显式 hash、文件路径或种子根目录回查下载历史。
        """
        if download_hash:
            return repository.get_by_hash(download_hash)

        if bluray_dir:
            return repository.get_by_path(file_path.as_posix())

        download_file = repository.get_file_by_fullpath(file_path.as_posix())
        if download_file and download_file.download_hash:
            return repository.get_by_hash(download_file.download_hash)

        # 多文件种子里的字幕/附加文件可能没有稳定的 fullpath 记录，
        # 退回到父目录和 savepath 继续查找，尽量补齐同一种子的关联信息。
        shared_download_roots = self._get_shared_download_roots(file_path)

        for parent_path in file_path.parents:
            parent_posix = parent_path.as_posix()
            download_files = repository.get_files_by_savepath(parent_posix) or []

            if parent_posix in shared_download_roots:
                # 共享下载根目录只能接受有明确文件记录的匹配，
                # 避免单文件/磁力任务把整个根目录污染成同一媒体。
                history = self._resolve_history_from_download_files(
                    repository=repository,
                    download_files=download_files,
                    file_path=file_path,
                    save_path=parent_path,
                )
                if history:
                    return history
                break

            download_history = repository.get_by_path(parent_posix)
            if download_history:
                return download_history

            history = self._resolve_history_from_download_files(
                repository=repository,
                download_files=download_files,
            )
            if history:
                return history

        return None

    @staticmethod
    def _is_movie_year_conflict(
            file_meta: MetaBase,
            media: Union[DownloadHistorySnapshot, MediaInfo, MusicInfo]
    ) -> bool:
        """
        判断文件名年份是否与已识别电影年份冲突。

        多电影合集只保存一条下载历史，不能把合集首部电影的媒体 ID 套用到其它年份的文件；
        电视剧季包仍应继续复用同一条下载历史。
        """
        file_year = getattr(file_meta, "year", None)
        media_year = getattr(media, "year", None)
        if not file_meta or not media or not file_year or not media_year:
            return False
        media_type = getattr(media, "type", None)
        if not isinstance(media_type, MediaType):
            try:
                media_type = MediaType(media_type)
            except (TypeError, ValueError):
                return False
        return (
                media_type == MediaType.MOVIE
                and str(file_year) != str(media_year)
        )

    @staticmethod
    def _optional_attr_equal(
            source: MetaBase,
            target: MetaBase,
            attr: str,
            normalizer: Callable = None,
    ) -> bool:
        """
        比较可选识别字段。

        字段两边都没有识别到时不参与判断；只要任意一边识别到了，就要求两边值一致，
        避免把同名不同年份或不同季集的附加文件误归到当前主视频。
        """
        source_value = getattr(source, attr, None)
        target_value = getattr(target, attr, None)
        if source_value is None and target_value is None:
            return True
        if source_value is None or target_value is None:
            return False
        if normalizer:
            source_value = normalizer(source_value)
            target_value = normalizer(target_value)
        return source_value == target_value

    def _is_same_media_meta(
            self, source_meta: MetaBase, target_meta: MetaBase
    ) -> bool:
        """
        判断两个文件识别出的媒体身份是否一致。
        """
        if not source_meta or not target_meta:
            return False
        if source_meta.type != target_meta.type:
            return False
        if text_tools.normalize_upper(source_meta.name) != text_tools.normalize_upper(
                target_meta.name
        ):
            return False
        if not self._optional_attr_equal(source_meta, target_meta, "year", str):
            return False
        for attr in (
                "begin_season",
                "end_season",
                "begin_episode",
                "end_episode",
        ):
            if not self._optional_attr_equal(source_meta, target_meta, attr, int):
                return False
        return True


class FileKeyMixin(_TransferOwnerBase):
    """提供文件、目录及同名旁挂文件的稳定匹配键。"""

    __mixin_host_protocol__ = TransferMixinHost

    @staticmethod
    def _get_file_key(fileitem: FileItem) -> Tuple[str, str]:
        """
        获取文件缓存键。
        """
        normalized_path = Path(str(fileitem.path).replace("\\", "/")).as_posix()
        return fileitem.storage or "local", normalized_path

    @staticmethod
    def _get_file_stem(fileitem: FileItem) -> str:
        """
        获取文件主干名，用于判断同名附加文件。
        """
        file_name = fileitem.name or Path(fileitem.path).name
        return Path(file_name).stem.lower()

    @classmethod
    def _get_subtitle_media_stem(cls, subtitle_fileitem: FileItem) -> str:
        """
        获取字幕对应主视频的候选主干名。
        """
        current_stem = cls._get_file_stem(subtitle_fileitem)
        while current_stem:
            media_stem, separator, suffix = current_stem.rpartition(".")
            if not separator or suffix not in SUBTITLE_STEM_TAGS:
                return current_stem
            current_stem = media_stem
        return current_stem

    def _get_extra_media_stem(self, extra_fileitem: FileItem) -> str:
        """
        获取附加文件对应主视频的候选主干名。
        """
        if self._is_subtitle_file(extra_fileitem):
            return self._get_subtitle_media_stem(extra_fileitem)
        if self._is_music_lyrics_file(extra_fileitem):
            file_name = extra_fileitem.name or Path(extra_fileitem.path).name
            lowered = file_name.casefold()
            suffix = ".lyricsfile.yaml" if lowered.endswith(".lyricsfile.yaml") else Path(file_name).suffix
            return file_name[:-len(suffix)].casefold() if suffix else lowered
        return self._get_file_stem(extra_fileitem)

    def _get_related_main_file_key(
            self,
            extra_fileitem: FileItem,
            main_fileitems: List[FileItem],
    ) -> Optional[Tuple[str, str]]:
        """
        获取与附加文件名完全匹配的主视频键。
        """
        if not (
                self._is_subtitle_file(extra_fileitem)
                or self._is_audio_file(extra_fileitem)
                or self._is_music_lyrics_file(extra_fileitem)
        ):
            return None

        extra_media_stem = self._get_extra_media_stem(extra_fileitem)
        matched_items: List[FileItem] = []
        for main_fileitem in main_fileitems:
            main_stem = self._get_file_stem(main_fileitem)
            if main_stem and main_stem == extra_media_stem:
                matched_items.append(main_fileitem)

        if len(matched_items) != 1:
            return None
        return self._get_file_key(matched_items[0])

    @staticmethod
    def _normalize_dir_path(dir_path: Union[str, Path]) -> str:
        """
        归一化目录路径，用于同一父目录候选缓存。
        """
        normalized = Path(dir_path).as_posix().rstrip("/")
        return normalized or "/"

    def _get_dir_key(self, dir_item: FileItem) -> Tuple[str, str]:
        """
        获取目录缓存键。
        """
        return dir_item.storage, self._normalize_dir_path(dir_item.path)

    def _get_file_parent_key(self, current_item: FileItem) -> Tuple[str, str]:
        """
        获取文件父目录缓存键。
        """
        return (
            current_item.storage,
            self._normalize_dir_path(Path(current_item.path).parent),
        )


class ManualHistoryMixin(_TransferOwnerBase):
    """提供手动整理历史查询、清理及持久重试入口。"""

    __mixin_host_protocol__ = TransferMixinHost

    transfer_history_repository: TransferHistoryRepository
    transfer_execution_repository: TransferExecutionRepository

    @staticmethod
    def _get_subscribe_custom_words(
            history_record: Optional[DownloadHistorySnapshot],
    ) -> Optional[List[str]]:
        """
        获取整理用自定义识别词：优先使用下载时保存的快照，无快照（历史旧记录）时再按来源实时反查订阅。

        快照优先可避免整理阶段因订阅季号漂移、来源解析失败或订阅完成被删导致识别词丢失，从而原样入库到偏移前的季集。
        """
        if not history_record:
            return None
        # 下载时保存的完整订阅识别词快照优先
        if history_record.custom_words:
            return history_record.custom_words.split("\n")
        # 兜底：历史旧记录无快照时，按下载来源实时反查订阅
        if not isinstance(history_record.note, dict):
            return None
        source = history_record.note.get("source")
        if not source:
            return None
        subscribe = SubscribeChain().get_subscribe_by_source(str(source))
        return (
            subscribe.custom_words.split("\n")
            if subscribe and subscribe.custom_words
            else None
        )

    @staticmethod
    def _is_successful_move_history(
            history: Optional[TransferHistorySnapshot],
    ) -> bool:
        """判断历史记录是否为已成功完成的移动类整理。"""
        return bool(
            history
            and history.status
            and history.mode
            and "move" in history.mode
        )

    def _get_manual_transfer_history(
            self,
            fileitem: FileItem,
            transfer_history_oper: TransferHistoryRepository,
            include_move_dest: bool = False,
    ) -> Optional[TransferHistorySnapshot]:
        """查询文件源路径历史，并兼容从成功移动后的目标现址重新整理。"""
        if not fileitem.path:
            return None
        # resolve_history 在命中失败记录时会再确认一次有无成功记录，
        # 避免 get_by_src 无排序导致同源多行时返回哪条不确定
        history = resolve_history(
            fileitem.path,
            storage=fileitem.storage,
            transfer_history_oper=transfer_history_oper,
        )
        if history or not include_move_dest:
            return history

        history = transfer_history_oper.get_by_dest(
            fileitem.path,
            storage=fileitem.storage,
        )
        return history if self._is_successful_move_history(history) else None

    def get_manual_transfer_histories(
            self,
            fileitems: List[FileItem],
    ) -> List[TransferHistorySnapshot]:
        """
        查询文件或目录命中的成功整理记录，供手动整理界面显示重整状态。

        :param fileitems: 待查询的文件或目录项
        :return: 去重后的成功整理记录
        """
        transfer_history_oper = self.transfer_history_repository
        histories: Dict[int, TransferHistorySnapshot] = {}
        for fileitem in fileitems or []:
            if not fileitem or not fileitem.path:
                continue
            storage = fileitem.storage or "local"
            if fileitem.type == "dir":
                matched_histories = transfer_history_oper.list_success_by_src(
                    fileitem.path,
                    storage=storage,
                    recursive=True,
                )
                matched_histories.extend(
                    transfer_history_oper.list_success_move_by_dest(
                        fileitem.path,
                        storage=storage,
                        recursive=True,
                    )
                )
            else:
                history = self._get_manual_transfer_history(
                    fileitem=fileitem,
                    transfer_history_oper=transfer_history_oper,
                    include_move_dest=True,
                )
                matched_histories = [history] if history and history.status else []

            for history in matched_histories:
                histories[history.id] = history
        return list(histories.values())

    def _request_durable_transfer_retry(
            self,
            history: TransferHistorySnapshot,
            *,
            requested_by: str,
    ) -> Optional[Tuple[bool, str]]:
        """将 durable 历史重试交还持久调度器，旧历史返回 ``None``。

        普通重试和 AI 接管只登记重试意图；显式重新整理由
        ``_delete_manual_transfer_history`` 先放弃确定失败任务，再重新准入。

        :param history: 整理历史
        :param requested_by: 发起重试的稳定入口身份
        :return: durable 请求结果；旧历史返回 ``None`` 继续兼容流程
        """
        return _request_durable_transfer_retry(
            history,
            requested_by=requested_by,
            repository=self.transfer_execution_repository,
        )

    def _delete_manual_transfer_history(
            self,
            history: TransferHistorySnapshot,
            transfer_history_oper: TransferHistoryRepository,
    ) -> Tuple[bool, str]:
        """删除手动重整历史；失败 durable 回执先原子放弃再清理旧目标。"""
        task_id = getattr(history, "transfer_task_id", None)
        if task_id:
            settlement_revision = getattr(
                history,
                "transfer_settlement_revision",
                None,
            )
            if not settlement_revision:
                return False, "持久整理失败记录缺少结算版本，请刷新后重试"
            discard = TransferExecutionCommand(
                self.transfer_execution_repository
            ).discard_failed(
                task_id=task_id,
                history_id=history.id,
                settlement_revision=settlement_revision,
            )
            if not discard.discarded:
                return False, discard.message
        if (
                history.dest_fileitem
                and not ManualHistoryMixin._is_successful_move_history(history)
        ):
            if not isinstance(history.dest_fileitem, dict):
                return False, "目标文件历史数据无效"
            dest_fileitem = FileItem(**history.dest_fileitem)
            storage_chain = StorageChain()
            if (
                    storage_chain.exists(dest_fileitem)
                    and not storage_chain.delete_media_file(dest_fileitem)
            ):
                return False, f"{dest_fileitem.path} 删除失败"
        transfer_history_oper.delete(history.id)
        # 删除记录是用户显式要求重来，失败计数一并清零，否则重整仍会受上一轮次数限制
        clear_transfer_failures(history.src, history.src_storage)
        return True, ""
