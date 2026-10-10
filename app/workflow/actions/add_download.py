from dataclasses import fields
from typing import Any, Optional

from pydantic import Field

from app.chain.download import DownloadChain
from app.chain.media import MediaChain
from app.domain.context import Context as DownloadContext
from app.domain.context import MediaInfo as DownloadMediaInfo
from app.domain.context import MusicInfo as DownloadMusicInfo
from app.domain.context import TorrentInfo as DownloadTorrentInfo
from app.domain.meta.metabase import MetaBase
from app.domain.meta.metamusic import MetaMusic
from app.domain.metainfo import MetaInfo
from app.runtime.log import logger
from app.runtime.stop import runtime_stop_state
from app.schemas.context import Context as WorkflowTorrentContext
from app.schemas.types import MediaType
from app.schemas.workflow import ActionContext, ActionParams, DownloadTask
from app.workflow.actions import BaseAction


class AddDownloadParams(ActionParams):
    """
    添加下载资源参数
    """
    downloader: Optional[str] = Field(default=None, description="下载器")
    save_path: Optional[str] = Field(default=None, description="保存路径, 支持<storage>:<path>, 如rclone:/MP, smb:/server/share/Movies等")
    labels: Optional[str] = Field(default=None, description="标签（,分隔）")
    only_lack: Optional[bool] = Field(default=False, description="仅下载缺失的资源")


class AddDownloadAction(BaseAction):
    """
    添加下载资源
    """

    contract = {
        "inputs": [{"name": "torrents", "label": "资源", "kind": "list"}],
        "outputs": [{"name": "downloads", "label": "下载任务", "kind": "list"}],
        "concurrency_key": "download",
    }

    def __init__(self, action_id: str):
        """初始化下载结果集合和动作错误状态。"""
        super().__init__(action_id)
        self._added_downloads = []
        self._has_error = False

    name = "添加下载"
    description = "根据资源列表添加下载任务"
    data = AddDownloadParams().model_dump()

    @property
    def success(self) -> bool:
        """返回本次动作是否未遇到下载失败。"""
        return not self._has_error

    def execute(self, workflow_id: int, params: dict, context: ActionContext) -> ActionContext:
        """
        将工作流资源还原为下载链所需的领域上下文并添加下载任务。
        """
        params = AddDownloadParams(**params)
        _started = False
        for t in context.torrents:
            if runtime_stop_state.is_workflow_stopped(workflow_id):
                break
            # 检查缓存
            cache_key = f"{t.torrent_info.site}-{t.torrent_info.title}"
            if self.check_cache(workflow_id, cache_key):
                logger.info(f"{t.torrent_info.title} 已添加过下载，跳过")
                continue
            if not t.meta_info:
                t.meta_info = MetaInfo(title=t.torrent_info.title, subtitle=t.torrent_info.description)
            if not t.media_info:
                t.media_info = MediaChain().recognize_by_meta(
                    t.meta_info,
                    obtain_images=False,
                )
            if not t.media_info:
                self._has_error = True
                logger.warning(f"{t.torrent_info.title} 未识别到媒体信息，无法下载")
                continue
            download_context = self._to_domain_context(t)
            download_media_info = download_context.media_info
            if params.only_lack and isinstance(download_media_info, DownloadMediaInfo):
                exists_info = DownloadChain().media_exists(download_media_info)
                if exists_info:
                    if download_media_info.type == MediaType.MOVIE:
                        # 电影
                        logger.warning(f"{t.torrent_info.title} 媒体库中已存在，跳过")
                        continue
                    else:
                        # 电视剧
                        exists_seasons = exists_info.seasons or {}
                        if len(t.meta_info.season_list) > 1:
                            # 多季不下载
                            logger.warning(f"{t.meta_info.title} 有多季，跳过")
                            continue
                        else:
                            season_num = (
                                t.meta_info.begin_season
                                if t.meta_info.begin_season is not None
                                else (t.meta_info.season_list[0] if t.meta_info.season_list else 1)
                            )
                            exists_episodes = exists_seasons.get(season_num)
                            if exists_episodes and t.meta_info.episode_list:
                                if set(t.meta_info.episode_list).issubset(exists_episodes):
                                    logger.warning(
                                        f"{t.meta_info.title} 第 {season_num} 季第 {t.meta_info.episode_list} 集已存在，跳过")
                                    continue

            _started = True
            did = DownloadChain().download_single(context=download_context,
                                                  downloader=params.downloader,
                                                  save_path=params.save_path,
                                                  label=params.labels)
            if did:
                self._added_downloads.append(did)
                # 保存缓存
                self.save_cache(workflow_id, cache_key)

        if self._added_downloads:
            logger.info(f"已添加 {len(self._added_downloads)} 个下载任务")
            context.downloads.extend(
                [DownloadTask(download_id=did, downloader=params.downloader) for did in self._added_downloads]
            )
        elif _started:
            self._has_error = True

        self.job_done(f"已添加 {len(self._added_downloads)} 个下载任务")
        return context

    @staticmethod
    def _to_domain_context(context: WorkflowTorrentContext | DownloadContext) -> DownloadContext:
        """复用 RSS 的领域上下文，或把工作流传输模型还原为下载链领域对象。"""
        if context.media_info is None or context.torrent_info is None:
            raise ValueError("工作流下载上下文缺少媒体或种子信息")

        if isinstance(context, DownloadContext):
            return context

        context_data: dict[str, Any] = context.model_dump()
        media_data = context_data.get("media_info")
        if not isinstance(media_data, dict):
            raise ValueError("工作流下载上下文缺少有效媒体信息")
        domain_media_info: DownloadMediaInfo | DownloadMusicInfo
        if media_data.get("type") == MediaType.MUSIC.value:
            domain_media_info = DownloadMusicInfo.from_dict(media_data)
        else:
            domain_media_info = DownloadMediaInfo()
            domain_media_info.from_dict(media_data)

        torrent_data = context_data.get("torrent_info")
        if not isinstance(torrent_data, dict):
            raise ValueError("工作流下载上下文缺少有效种子信息")
        domain_torrent_info = DownloadTorrentInfo()
        domain_torrent_info.from_dict(torrent_data)

        domain_meta_info: MetaBase | None = None
        meta_data = context_data.get("meta_info")
        if isinstance(meta_data, dict):
            meta_type = meta_data.get("type")
            if meta_type == MediaType.MUSIC.value:
                domain_meta_info = MetaMusic.from_dict(meta_data)
            else:
                title = meta_data.get("org_string") or meta_data.get("title") or torrent_data.get("title")
                video_meta_info = MetaInfo(
                    title=title if isinstance(title, str) else "",
                    subtitle=meta_data.get("subtitle"),
                    mtype=MediaType(meta_type) if meta_type else None,
                )
                for item in fields(video_meta_info):
                    if item.name not in meta_data:
                        continue
                    value = meta_data[item.name]
                    if item.name == "type" and value:
                        value = MediaType(value)
                    if item.name == "title" and value is None:
                        continue
                    setattr(video_meta_info, item.name, value)
                name = meta_data.get("name")
                if isinstance(name, str) and name:
                    video_meta_info.name = name
                domain_meta_info = video_meta_info

        return DownloadContext(
            meta_info=domain_meta_info,
            media_info=domain_media_info,
            torrent_info=domain_torrent_info,
            resource_source=context.resource_source or "unknown",
            match_source=context.match_source or "unknown",
            candidate_recognized=bool(context.candidate_recognized),
            media_info_is_target=bool(context.media_info_is_target),
            match_status=context.match_status,
            match_reason=context.match_reason,
            confirmed_full_coverage=bool(context.confirmed_full_coverage),
            music_track_keys=list(context.music_track_keys) if context.music_track_keys else None,
        )
