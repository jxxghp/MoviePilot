from typing import List, Optional

from pydantic import Field

from app.application.torrent.download import TorrentHelper
from app.domain.context import Context as DomainContext
from app.domain.context import MediaInfo as DomainMediaInfo
from app.domain.context import MusicInfo as DomainMusicInfo
from app.domain.context import TorrentInfo as DomainTorrentInfo
from app.runtime.log import logger
from app.runtime.stop import runtime_stop_state
from app.schemas.context import Context as WorkflowTorrentContext
from app.schemas.workflow import ActionContext, ActionParams
from app.workflow.actions import ActionChain, BaseAction, domain_media_info_from_dict, domain_torrent_info_from_dict


class FilterTorrentsParams(ActionParams):
    """
    过滤资源数据参数
    """
    rule_groups: Optional[List[str]] = Field(default=[], description="规则组")
    quality: Optional[str] = Field(default=None, description="资源质量")
    resolution: Optional[str] = Field(default=None, description="资源分辨率")
    effect: Optional[str] = Field(default=None, description="特效")
    include: Optional[str] = Field(default=None, description="包含规则")
    exclude: Optional[str] = Field(default=None, description="排除规则")
    size: Optional[str] = Field(default=None, description="资源大小范围（MB）")


class FilterTorrentsAction(BaseAction):
    """
    过滤资源数据
    """

    contract = {
        "inputs": [{"name": "torrents", "label": "资源", "kind": "list"}],
        "outputs": [{"name": "torrents", "label": "资源", "kind": "list", "merge": "replace"}],
    }

    def __init__(self, action_id: str):
        super().__init__(action_id)
        self._torrents = []

    name = "过滤资源"
    description = "对资源列表数据进行过滤"
    data = FilterTorrentsParams().model_dump()

    @property
    def success(self) -> bool:
        return self.done

    def execute(self, workflow_id: int, params: dict, context: ActionContext) -> ActionContext:
        """
        过滤torrents中的资源
        """
        params = FilterTorrentsParams(**params)
        for torrent in context.torrents:
            if runtime_stop_state.is_workflow_stopped(workflow_id):
                break
            torrent_info, media_info = self._to_filter_inputs(torrent)
            if TorrentHelper().filter_torrent(
                    torrent_info=torrent_info,
                    filter_params={
                        "quality": params.quality,
                        "resolution": params.resolution,
                        "effect": params.effect,
                        "include": params.include,
                        "exclude": params.exclude,
                        "size": params.size
                    }
            ):
                if ActionChain().filter_torrents(
                        rule_groups=params.rule_groups,
                        torrent_list=[torrent_info],
                        mediainfo=media_info
                ):
                    if torrent.torrent_info is not None and torrent.torrent_info is not torrent_info:
                        # 过滤链把规则优先级写在还原出的领域副本上，需同步回保留在结果中的 Schema 原对象。
                        torrent.torrent_info.pri_order = torrent_info.pri_order
                    self._torrents.append(torrent)

        logger.info(f"过滤后剩余 {len(self._torrents)} 个资源")

        context.torrents = self._torrents

        self.job_done(f"过滤后剩余 {len(self._torrents)} 个资源")
        return context

    @staticmethod
    def _to_filter_inputs(
            torrent: WorkflowTorrentContext | DomainContext,
    ) -> tuple[DomainTorrentInfo, DomainMediaInfo | DomainMusicInfo | None]:
        """
        取出过滤链所需的领域种子和媒体对象。

        过滤链要读取种子发布时间（pub_minutes）和媒体类型枚举（type.value）。RSS 产出的领域上下文直接复用；
        搜索资源或从数据库恢复的上下文是工作流 Schema，需先还原。只用于过滤判断，保留在结果里的仍是原对象。
        """
        if isinstance(torrent, DomainContext):
            if torrent.torrent_info is None:
                raise ValueError("工作流资源缺少种子信息")
            return torrent.torrent_info, torrent.media_info
        torrent_data = torrent.model_dump()
        media_data = torrent_data.get("media_info")
        return (
            domain_torrent_info_from_dict(torrent_data.get("torrent_info") or {}),
            domain_media_info_from_dict(media_data) if isinstance(media_data, dict) else None,
        )
