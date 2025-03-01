from app.actions import BaseAction
from app.chain.subscribe import SubscribeChain
from app.core.config import settings, global_vars
from app.core.context import MediaInfo
from app.db.subscribe_oper import SubscribeOper
from app.log import logger
from app.schemas import ActionParams, ActionContext


class AddSubscribeParams(ActionParams):
    """
    添加订阅参数
    """
    pass


class AddSubscribeAction(BaseAction):
    """
    添加订阅
    """

    _added_subscribes = []
    _has_error = False

    def __init__(self):
        super().__init__()
        self.subscribechain = SubscribeChain()
        self.subscribeoper = SubscribeOper()

    @classmethod
    @property
    def name(cls) -> str:
        return "添加订阅"

    @classmethod
    @property
    def description(cls) -> str:
        return "根据媒体列表添加订阅"

    @classmethod
    @property
    def data(cls) -> dict:
        return AddSubscribeParams().dict()

    @property
    def success(self) -> bool:
        return not self._has_error

    def execute(self, workflow_id: int, params: dict, context: ActionContext) -> ActionContext:
        """
        将medias中的信息添加订阅，如果订阅不存在的话
        """
        for media in context.medias:
            if global_vars.is_workflow_stopped(workflow_id):
                break
            mediainfo = MediaInfo()
            mediainfo.from_dict(media.dict())
            if self.subscribechain.exists(mediainfo):
                logger.info(f"{media.title} 已存在订阅")
                continue
            # 添加订阅
            sid, message = self.subscribechain.add(mtype=mediainfo.type,
                                                   title=mediainfo.title,
                                                   year=mediainfo.year,
                                                   tmdbid=mediainfo.tmdb_id,
                                                   season=mediainfo.season,
                                                   doubanid=mediainfo.douban_id,
                                                   bangumiid=mediainfo.bangumi_id,
                                                   username=settings.SUPERUSER)
            if sid:
                self._added_subscribes.append(sid)
            else:
                self._has_error = True

        if self._added_subscribes:
            logger.info(f"已添加 {len(self._added_subscribes)} 个订阅")
            for sid in self._added_subscribes:
                context.subscribes.append(self.subscribeoper.get(sid))

        self.job_done()
        return context
