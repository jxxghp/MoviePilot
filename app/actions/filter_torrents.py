from typing import Optional, List

from pydantic import Field

from app.actions import BaseAction, ActionChain
from app.core.config import global_vars
from app.helper.torrent import TorrentHelper
from app.log import logger
from app.schemas import ActionParams, ActionContext


class FilterTorrentsParams(ActionParams):
    """
    过滤资源数据参数
    """
    rule_groups: Optional[List[str]] = Field([], description="规则组")
    quality: Optional[str] = Field(None, description="资源质量")
    resolution: Optional[str] = Field(None, description="资源分辨率")
    effect: Optional[str] = Field(None, description="特效")
    include: Optional[str] = Field(None, description="包含规则")
    exclude: Optional[str] = Field(None, description="排除规则")
    size: Optional[str] = Field(None, description="资源大小范围（MB）")


class FilterTorrentsAction(BaseAction):
    """
    过滤资源数据
    """

    _torrents = []

    def __init__(self, action_id: str):
        super().__init__(action_id)
        self.torrenthelper = TorrentHelper()
        self.chain = ActionChain()

    @classmethod
    @property
    def name(cls) -> str: # noqa
        return "过滤资源"

    @classmethod
    @property
    def description(cls) -> str: # noqa
        return "对资源列表数据进行过滤"

    @classmethod
    @property
    def data(cls) -> dict: # noqa
        return FilterTorrentsParams().dict()

    @property
    def success(self) -> bool:
        return self.done

    def execute(self, workflow_id: int, params: dict, context: ActionContext) -> ActionContext:
        """
        过滤torrents中的资源
        """
        params = FilterTorrentsParams(**params)
        for torrent in context.torrents:
            if global_vars.is_workflow_stopped(workflow_id):
                break
            if self.torrenthelper.filter_torrent(
                    torrent_info=torrent.torrent_info,
                    filter_params={
                        "quality": params.quality,
                        "resolution": params.resolution,
                        "effect": params.effect,
                        "include": params.include,
                        "exclude": params.exclude,
                        "size": params.size
                    }
            ):
                if self.chain.filter_torrents(
                        rule_groups=params.rule_groups,
                        torrent_list=[torrent.torrent_info],
                        mediainfo=torrent.media_info
                ):
                    self._torrents.append(torrent)

        logger.info(f"过滤后剩余 {len(self._torrents)} 个资源")

        context.torrents = self._torrents

        self.job_done(f"过滤后剩余 {len(self._torrents)} 个资源")
        return context
