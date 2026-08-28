"""整理剧集命名与格式规则推荐。"""

from pathlib import Path
from typing import List, Optional, Tuple, Union, cast

from app.application.configuration import (
    get_configured_system_config,
)
from app.application.formatting import EpisodeFormatRuleHelper
from app.chain._contracts import TransferMixinHost
from app.chain.storage import StorageChain
from app.chain.transfer.contract import _TransferOwnerBase
from app.domain.context import MediaInfo, MusicInfo
from app.domain.meta.metabase import MetaBase
from app.runtime.log import logger
from app.schemas.tmdb import TmdbEpisode
from app.schemas.transfer import EpisodeFormatRule as _SchemaEpisodeFormatRule
from app.schemas.types import (
    MediaType,
    SystemConfigKey,
)
from app.schemas.workflow import FileItem


class EpisodeFormatMixin(_TransferOwnerBase):
    """提供剧集命名建议、格式规则选择和样例发现。"""

    __mixin_host_protocol__ = TransferMixinHost


    def recommend_name(
        self,
        meta: Optional[MetaBase],
        mediainfo: Union[MediaInfo, MusicInfo],
    ) -> Optional[str]:
        """
        获取重命名后的名称
        :param meta: 元数据
        :param mediainfo: 媒体信息
        :return: 重命名后的名称（含目录）
        """
        if meta is None:
            return None
        # 获取集信息，供重命名模块使用
        episodes_info: Optional[List[TmdbEpisode]] = None
        if mediainfo.type == MediaType.TV:
            # 判断注意season为0的情况
            season_num = mediainfo.season
            if season_num is None and meta.season_seq:
                if meta.season_seq.isdigit():
                    season_num = int(meta.season_seq)
            # 默认值1
            if season_num is None:
                season_num = 1
            episodes_info = self.run_module(
                "tmdb_episodes",
                tmdbid=mediainfo.tmdb_id,
                season=season_num,
                episode_group=mediainfo.episode_group,
            )
        if episodes_info:
            return cast(
                Optional[str],
                self.run_module(
                    "recommend_name",
                    meta=meta,
                    mediainfo=mediainfo,
                    episodes_info=episodes_info,
                ),
            )
        # 电影或无集信息时保持原有参数集，避免影响旧签名的模块实现
        return cast(
            Optional[str],
            self.run_module("recommend_name", meta=meta, mediainfo=mediainfo),
        )

    def recommend_episode_format(
            self,
            fileitem: Optional[FileItem],
            fileitems: Optional[List[FileItem]] = None,
    ) -> Tuple[bool, str, Optional[dict]]:
        """
        根据目录样本推荐集数定位模板
        """
        if not fileitem and not fileitems:
            logger.warn("推荐集数定位模板失败：缺少目录参数")
            return False, "缺少目录参数", None

        rules = self._get_episode_format_rules()
        if fileitems:
            state, errmsg, sample_files = self._get_selected_episode_format_sample_files(
                fileitems
            )
            if not state:
                logger.warn(f"推荐集数定位模板失败：{errmsg}")
                return False, errmsg, None
            target_path = sample_files[0].path if sample_files else None
        else:
            if not fileitem or not fileitem.path:
                logger.warn("推荐集数定位模板失败：缺少目录参数")
                return False, "缺少目录参数", None
            directory = self._resolve_episode_format_directory(fileitem)
            if not directory or directory.type != "dir":
                logger.warn(f"推荐集数定位模板失败：目录不存在 - {fileitem.path}")
                return False, "目录不存在", None
            sample_files = self._get_episode_format_sample_files(directory)
            target_path = directory.path
        logger.info(
            f"开始匹配集数定位规则：{target_path}，规则数 {len(rules)}，样本数 {len(sample_files)}"
        )
        state, errmsg, data = EpisodeFormatRuleHelper().recommend(
            rules=rules,
            sample_files=sample_files,
        )
        if not state:
            logger.warn(f"集数定位模板推荐失败：{target_path} - {errmsg}")
            return state, errmsg, data
        logger.info(
            f"集数定位模板推荐成功：{target_path} - 规则 {data.get('rule_name') if data else None}"
        )
        return state, errmsg, data

    @staticmethod
    def _get_episode_format_rules() -> List[_SchemaEpisodeFormatRule]:
        """
        获取启用的集数定位规则
        """
        rule_items = get_configured_system_config().get(SystemConfigKey.EpisodeFormatRuleTable) or []
        rules: List[_SchemaEpisodeFormatRule] = []
        for item in rule_items:
            if not isinstance(item, dict):
                continue
            try:
                rule = _SchemaEpisodeFormatRule(**item)
            except Exception as err:
                logger.warn(f"忽略无效的集数定位规则：{err}")
                continue
            if rule.enabled:
                rules.append(rule)
        return sorted(rules, key=lambda item: item.order)

    def _resolve_episode_format_directory(
            self, fileitem: FileItem
    ) -> Optional[FileItem]:
        """
        将文件或目录入参归一化为目录对象
        """
        storage_chain = StorageChain()
        if fileitem.type == "dir":
            return storage_chain.get_item(fileitem)
        source_path = Path(fileitem.path)
        parent_item = FileItem(
            storage=fileitem.storage,
            path=source_path.parent.as_posix(),
            type="dir",
            name=source_path.parent.name,
        )
        return storage_chain.get_item(parent_item)

    def _get_selected_episode_format_sample_files(
            self, fileitems: List[FileItem]
    ) -> Tuple[bool, str, List[FileItem]]:
        """
        获取当前选择文件中可参与模板推荐的样本文件。
        """
        if not fileitems:
            return False, "没有可用于识别的样本文件", []

        expected_dir_key: Optional[Tuple[str, str]] = None
        selected_files: List[FileItem] = []
        seen_files = set()
        for item in fileitems:
            if not item or not item.path or item.type != "file":
                return False, "当前选择不满足智能识别条件", []

            dir_key = (
                item.storage or "local",
                Path(item.path).parent.as_posix(),
            )
            if expected_dir_key is None:
                expected_dir_key = dir_key
            elif dir_key != expected_dir_key:
                return False, "当前选择不满足智能识别条件", []

            file_key = (item.storage or "local", item.path)
            if file_key in seen_files:
                continue
            seen_files.add(file_key)

            if not (
                    self._is_media_file(item)
                    or self._is_subtitle_file(item)
                    or self._is_audio_file(item)
            ):
                continue
            if self._is_hidden_or_recycle_path(item.path):
                continue
            selected_files.append(item)

        if not selected_files:
            return False, "没有可用于识别的样本文件", []
        return True, "", selected_files

    def _get_episode_format_sample_files(
            self, directory: FileItem
    ) -> List[FileItem]:
        """
        获取目录下可参与模板推荐的样本文件。

        推荐结果最终会在手动整理链路中作为 `episode_format`
        交由 `FormatParser` 过滤主视频、字幕和外挂音频，因此这里需要把
        同目录下的主视频、字幕和外挂音频一起纳入推荐流程。
        """
        file_items = StorageChain().list_files(directory, recursion=False) or []
        sample_files: List[FileItem] = []
        for item in file_items:
            if not item or item.type != "file":
                continue
            if not (
                    self._is_media_file(item)
                    or self._is_subtitle_file(item)
                    or self._is_audio_file(item)
            ):
                continue
            if self._is_hidden_or_recycle_path(item.path):
                continue
            sample_files.append(item)
        return sample_files
