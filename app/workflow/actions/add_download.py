"""
将工作流中的资源提交给下载链。
"""

from dataclasses import fields
from typing import Any, Iterator, Optional, cast

from pydantic import Field

from app.application.mediaserver import MEDIA_SERVER_EXISTENCE_QUERY
from app.chain.download import DownloadChain
from app.chain.media import MediaChain
from app.domain.context import Context as DownloadContext
from app.domain.context import MediaInfo as DownloadMediaInfo
from app.domain.meta.metabase import MetaBase
from app.domain.meta.metamusic import MetaMusic
from app.domain.metainfo import MetaInfo
from app.runtime.log import logger
from app.runtime.stop import runtime_stop_state
from app.schemas.context import Context as WorkflowTorrentContext
from app.schemas.download import DownloadTask
from app.schemas.types import MediaType
from app.schemas.workflow import ActionContext, ActionParams
from app.workflow.actions import BaseAction, domain_media_info_from_dict, domain_torrent_info_from_dict


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

    def __init__(self, action_id: str) -> None:
        """
        初始化下载结果集合和动作错误状态。
        """
        super().__init__(action_id)
        self._added_downloads: list[str] = []
        self._has_error = False

    name = "添加下载"
    description = "根据资源列表添加下载任务"
    data = AddDownloadParams().model_dump()

    @property
    def success(self) -> bool:
        """
        返回本次动作是否未遇到下载失败。
        """
        return not self._has_error

    def execute(self, workflow_id: int, params: dict[str, object], context: ActionContext) -> ActionContext:
        """
        将工作流资源还原为下载链所需的领域上下文并添加下载任务。
        """
        action_params = AddDownloadParams.model_validate(params)
        _started = False
        for t in self._iter_resources(workflow_id, context):
            torrent_info = t.torrent_info
            if torrent_info is None:
                self._has_error = True
                logger.warning("工作流资源缺少种子信息，跳过下载")
                continue
            # 检查缓存
            cache_key = f"{torrent_info.site}-{torrent_info.title}"
            if self.check_cache(workflow_id, cache_key):
                logger.info(f"{torrent_info.title} 已添加过下载，跳过")
                continue
            if (
                not self._recognize_resource(t, title=torrent_info.title, subtitle=torrent_info.description)
                or t.meta_info is None
            ):
                continue
            download_context = self._to_domain_context(t)
            download_media_info = download_context.media_info
            if action_params.only_lack and isinstance(download_media_info, DownloadMediaInfo):
                exists_info = DownloadChain().media_exists(download_media_info, query=MEDIA_SERVER_EXISTENCE_QUERY)
                if exists_info:
                    if download_media_info.type == MediaType.MOVIE:
                        # 电影
                        logger.warning(f"{torrent_info.title} 媒体库中已存在，跳过")
                        continue
                    else:
                        # 电视剧
                        exists_seasons = exists_info.seasons or {}
                        meta_info = cast(MetaBase, t.meta_info)
                        season_list = getattr(meta_info, "season_list", []) or []
                        if len(season_list) > 1:
                            # 多季不下载
                            logger.warning(f"{t.meta_info.title} 有多季，跳过")
                            continue
                        else:
                            begin_season = getattr(meta_info, "begin_season", None)
                            season_num = begin_season if begin_season is not None else (season_list[0] if season_list else 1)
                            exists_episodes = exists_seasons.get(season_num)
                            episode_list = getattr(meta_info, "episode_list", []) or []
                            if exists_episodes and episode_list:
                                if set(episode_list).issubset(exists_episodes):
                                    logger.warning(
                                        f"{meta_info.title} 第 {season_num} 季第 {episode_list} 集已存在，跳过"
                                    )
                                    continue

            _started = True
            did = DownloadChain().download_single(
                context=download_context,
                downloader=action_params.downloader,
                save_path=action_params.save_path,
                label=action_params.labels,
            )
            if did:
                download_id = cast(str, did[1] if isinstance(did, tuple) else did)
                self._added_downloads.append(download_id)
                # 保存缓存
                self.save_cache(workflow_id, cache_key)

        if self._added_downloads:
            logger.info(f"已添加 {len(self._added_downloads)} 个下载任务")
            if context.downloads is None:
                context.downloads = []
            context.downloads.extend(
                [DownloadTask(download_id=did, downloader=action_params.downloader) for did in self._added_downloads]
            )
        elif _started:
            self._has_error = True

        self.job_done(f"已添加 {len(self._added_downloads)} 个下载任务")
        return context

    def _recognize_resource(
            self,
            resource: WorkflowTorrentContext | DownloadContext,
            title: Optional[str],
            subtitle: Optional[str],
    ) -> bool:
        """
        在缓存检查后补齐资源的媒体信息，保留已有元数据及失败状态。
        """
        if not resource.meta_info:
            if title is None:
                self._has_error = True
                logger.warning("工作流资源缺少可识别的标题，跳过下载")
                return False
            resource.meta_info = MetaInfo(title=title, subtitle=subtitle)
        if not resource.media_info:
            resource.media_info = MediaChain().recognize_by_meta(
                resource.meta_info,
                obtain_images=False,
            )
        if not resource.media_info:
            self._has_error = True
            logger.warning(f"{title} 未识别到媒体信息，无法下载")
            return False
        return True

    def _iter_resources(
            self, workflow_id: int, context: ActionContext,
    ) -> Iterator[WorkflowTorrentContext | DownloadContext]:
        """
        恢复节点输出字典并保留实时领域对象；历史文本提示重跑并保留动作失败状态。
        """
        for resource in context.torrents or []:
            if runtime_stop_state.is_workflow_stopped(workflow_id):
                break
            if isinstance(resource, str):
                # 旧版文本无法可靠还原，避免把丢失资源报告为成功。
                self._has_error = True
                logger.warning("工作流历史资源已保存为文本，无法恢复，跳过该资源；请重新运行工作流获取资源")
                continue
            if isinstance(resource, dict):
                resource = WorkflowTorrentContext.model_validate(resource)
            yield resource

    @staticmethod
    def _to_domain_context(
            context: WorkflowTorrentContext | DownloadContext | dict[str, Any],
    ) -> DownloadContext:
        """
        复用 RSS 领域上下文，或把恢复的节点输出字典及传输模型还原为下载链领域对象。
        """
        if isinstance(context, dict):
            return AddDownloadAction._to_domain_context(WorkflowTorrentContext.model_validate(context))
        if context.media_info is None or context.torrent_info is None:
            raise ValueError("工作流下载上下文缺少媒体或种子信息")

        if isinstance(context, DownloadContext):
            return context

        context_data: dict[str, Any] = context.model_dump()
        media_data = context_data.get("media_info")
        if not isinstance(media_data, dict):
            raise ValueError("工作流下载上下文缺少有效媒体信息")
        domain_media_info = domain_media_info_from_dict(media_data)

        torrent_data = context_data.get("torrent_info")
        if not isinstance(torrent_data, dict):
            raise ValueError("工作流下载上下文缺少有效种子信息")
        domain_torrent_info = domain_torrent_info_from_dict(torrent_data)

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
