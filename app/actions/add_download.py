from typing import Optional

from pydantic import Field

from app.actions import BaseAction
from app.chain.download import DownloadChain
from app.chain.media import MediaChain
from app.core.config import global_vars
from app.core.metainfo import MetaInfo
from app.log import logger
from app.schemas import ActionParams, ActionContext, DownloadTask, MediaType


class AddDownloadParams(ActionParams):
    """
    添加下载资源参数
    """
    downloader: Optional[str] = Field(None, description="下载器")
    save_path: Optional[str] = Field(None, description="保存路径")
    labels: Optional[str] = Field(None, description="标签（,分隔）")
    only_lack: Optional[bool] = Field(False, description="仅下载缺失的资源")


class AddDownloadAction(BaseAction):
    """
    添加下载资源
    """

    # 已添加的下载
    _added_downloads = []
    _has_error = False

    def __init__(self, action_id: str):
        super().__init__(action_id)
        self.downloadchain = DownloadChain()
        self.mediachain = MediaChain()

    @classmethod
    @property
    def name(cls) -> str: # noqa
        return "添加下载"

    @classmethod
    @property
    def description(cls) -> str: # noqa
        return "根据资源列表添加下载任务"

    @classmethod
    @property
    def data(cls) -> dict: # noqa
        return AddDownloadParams().dict()

    @property
    def success(self) -> bool:
        return not self._has_error

    def execute(self, workflow_id: int,  params: dict, context: ActionContext) -> ActionContext:
        """
        将上下文中的torrents添加到下载任务中
        """
        params = AddDownloadParams(**params)
        for t in context.torrents:
            if global_vars.is_workflow_stopped(workflow_id):
                break
            # 检查缓存
            cache_key = f"{t.torrent_info.site}-{t.torrent_info.title}"
            if self.check_cache(workflow_id, cache_key):
                logger.info(f"{t.title} 已添加过下载，跳过")
                continue
            if not t.meta_info:
                t.meta_info = MetaInfo(title=t.title, subtitle=t.description)
            if not t.media_info:
                t.media_info = self.mediachain.recognize_media(meta=t.meta_info)
            if not t.media_info:
                self._has_error = True
                logger.warning(f"{t.title} 未识别到媒体信息，无法下载")
                continue
            if params.only_lack:
                exists_info = self.downloadchain.media_exists(t.media_info)
                if exists_info:
                    if t.media_info.type == MediaType.MOVIE:
                        # 电影
                        logger.warning(f"{t.title} 媒体库中已存在，跳过")
                        continue
                    else:
                        # 电视剧
                        exists_seasons = exists_info.seasons or {}
                        if len(t.meta_info.season_list) > 1:
                            # 多季不下载
                            logger.warning(f"{t.meta_info.title} 有多季，跳过")
                            continue
                        else:
                            exists_episodes = exists_seasons.get(t.meta_info.begin_season)
                            if exists_episodes:
                                if set(t.meta_info.episode_list).issubset(exists_episodes):
                                    logger.warning(f"{t.meta_info.title} 第 {t.meta_info.begin_season} 季第 {t.meta_info.episode_list} 集已存在，跳过")
                                    continue

            did = self.downloadchain.download_single(context=t,
                                                     downloader=params.downloader,
                                                     save_path=params.save_path,
                                                     label=params.labels)
            if did:
                self._added_downloads.append(did)
                # 保存缓存
                self.save_cache(workflow_id, cache_key)
            else:
                self._has_error = True

        if self._added_downloads:
            logger.info(f"已添加 {len(self._added_downloads)} 个下载任务")
            context.downloads.extend(
                [DownloadTask(download_id=did, downloader=params.downloader) for did in self._added_downloads]
            )

        self.job_done(f"已添加 {len(self._added_downloads)} 个下载任务")
        return context
