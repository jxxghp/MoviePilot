"""
根据工作流上下文创建媒体订阅。
"""

from app.application.configuration import get_chain_runtime_config_snapshot
from app.chain.subscribe.facade import SubscribeChain
from app.domain.context import MediaInfo
from app.runtime.log import logger
from app.runtime.stop import runtime_stop_state
from app.schemas.subscribe import Subscribe
from app.schemas.workflow import ActionContext, ActionParams
from app.workflow.actions import BaseAction


class AddSubscribeParams(ActionParams):
    """
    添加订阅参数
    """
    pass


class AddSubscribeAction(BaseAction):
    """
    添加订阅
    """

    contract = {
        "inputs": [{"name": "medias", "label": "媒体", "kind": "list"}],
        "outputs": [{"name": "subscribes", "label": "订阅", "kind": "list"}],
    }

    def __init__(self, action_id: str) -> None:
        """
        初始化当前动作的结果集合与失败状态。
        """
        super().__init__(action_id)
        self._added_subscribes: list[int] = []
        self._has_error = False

    name = "添加订阅"
    description = "根据媒体列表添加订阅"
    data = AddSubscribeParams().model_dump()

    @property
    def success(self) -> bool:
        """
        返回动作是否未遇到终止性的处理失败。
        """
        return not self._has_error

    def execute(self, workflow_id: int, params: dict[str, object], context: ActionContext) -> ActionContext:
        """
        将medias中的信息添加订阅，如果订阅不存在的话
        """
        _started = False
        subscribechain = None
        for media in context.medias or []:
            if runtime_stop_state.is_workflow_stopped(workflow_id):
                break
            # 检查缓存
            cache_key = f"{media.type}-{media.title}-{media.year}-{media.season}"
            if self.check_cache(workflow_id, cache_key):
                logger.info(f"{media.title} {media.year} 已添加过订阅，跳过")
                continue
            mediainfo = MediaInfo()
            mediainfo.from_dict(media.model_dump())
            subscribechain = SubscribeChain()
            if subscribechain.exists(mediainfo):
                logger.info(f"{media.title} 已存在订阅")
                continue
            # 添加订阅
            _started = True
            sid, message = subscribechain.add(mtype=mediainfo.type,
                                              title=mediainfo.title or "",
                                              year=mediainfo.year or "",
                                              season=mediainfo.season,
                                              media_source=mediainfo.media_source,
                                              media_id=mediainfo.media_id,
                                              username=get_chain_runtime_config_snapshot().superuser)
            if sid:
                self._added_subscribes.append(sid)
                # 保存缓存
                self.save_cache(workflow_id, cache_key)

        if self._added_subscribes:
            logger.info(f"已添加 {len(self._added_subscribes)} 个订阅")
            if subscribechain is None:
                subscribechain = SubscribeChain()
            for sid in self._added_subscribes:
                subscribe = subscribechain.subscription_repository.get(sid)
                if subscribe:
                    if context.subscribes is None:
                        context.subscribes = []
                    context.subscribes.append(Subscribe.model_validate(subscribe.to_dict()))
        elif _started:
            self._has_error = True

        self.job_done(f"已添加 {len(self._added_subscribes)} 个订阅")
        return context
