"""整理失败历史的人工、回调与 Agent 重试编排。"""

from pathlib import Path
from typing import List, Optional, Tuple, Union, cast

from app.application.agent import build_manual_redo_prompt, get_running_agent_manager
from app.application.history import (
    TransferHistoryRepository,
    TransferHistorySnapshot,
)
from app.application.transfer.execution import (
    TransferExecutionCommand,
    TransferExecutionRepository,
)
from app.chain._contracts import TransferMixinHost
from app.chain.media import MediaChain
from app.chain.storage import StorageChain
from app.chain.transfer.contract import _TransferOwnerBase
from app.domain.context import MusicInfo
from app.runtime.errors import public_error_message
from app.runtime.log import logger
from app.runtime.loop import main_loop_registry
from app.runtime.tasks import get_task_registry
from app.schemas.message import Message
from app.schemas.types import (
    MediaSource,
    MediaType,
    NotificationChannel,
    ReplyMode,
)
from app.schemas.workflow import FileItem


def _request_durable_transfer_retry(
        history: TransferHistorySnapshot,
        *,
        requested_by: str,
        repository: TransferExecutionRepository,
) -> Optional[Tuple[bool, str]]:
    """将 durable 历史重试交还持久调度器，旧历史返回 ``None``。"""
    task_id = getattr(history, "transfer_task_id", None)
    if not task_id:
        return None
    try:
        result = TransferExecutionCommand(repository).request_retry(
            task_id=task_id,
            reason=f"用户请求重试整理历史 #{history.id}",
            requested_by=requested_by,
        )
    except (RuntimeError, ValueError) as error:
        logger.warning(
            "登记 durable 整理重试失败：history_id=%s task_id=%s error=%s",
            history.id,
            task_id,
            error,
        )
        return False, "整理任务暂时无法重试，请稍后重试"
    return result.accepted, result.message



class FailedRetryMixin(_TransferOwnerBase):
    """提供失败整理的按钮、回调和兼容重试流程。"""

    __mixin_host_protocol__ = TransferMixinHost

    transfer_history_repository: TransferHistoryRepository
    transfer_execution_repository: TransferExecutionRepository

    @staticmethod
    def build_failed_transfer_buttons(
            history_id: Optional[int],
    ) -> Optional[List[List[dict]]]:
        """
        构建整理失败通知的操作按钮。
        """
        if not history_id:
            return None
        return [
            [
                {"text": "重试", "callback_data": f"transfer_retry_{history_id}"},
                {
                    "text": "智能助手接管",
                    "callback_data": f"transfer_ai_retry_{history_id}",
                },
            ]
        ]

    def redo_transfer_history(self, history_id: int) -> Tuple[bool, str]:
        """
        按历史记录直接重新整理，自动重新识别媒体信息。
        """
        return self._re_transfer(logid=history_id)

    @staticmethod
    def parse_failed_transfer_callback(
            callback_data: str,
    ) -> Optional[tuple[str, int]]:
        """
        解析整理失败通知按钮回调。
        """
        for prefix, action in (
                ("transfer_retry_", "retry"),
                ("transfer_ai_retry_", "ai_retry"),
        ):
            if callback_data.startswith(prefix):
                history_id = callback_data.replace(prefix, "", 1)
                if history_id.isdigit():
                    return action, int(history_id)
        return None

    def handle_failed_transfer_callback(
            self,
            *,
            callback_data: str,
            channel: NotificationChannel,
            source: Optional[str],
            userid: Union[str, int],
            username: Optional[str],
    ) -> bool:
        """
        处理整理失败通知中的重试类按钮。
        """
        callback = self.parse_failed_transfer_callback(callback_data)
        if not callback:
            return False

        action, history_id = callback
        if action == "retry":
            self._retry_transfer_history(
                history_id=history_id,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            )
        else:
            self._take_over_transfer_history_by_ai(
                history_id=history_id,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
            )
        return True

    def _retry_transfer_history(
            self,
            history_id: int,
            channel: NotificationChannel,
            source: Optional[str],
            userid: Union[str, int],
            username: Optional[str],
    ) -> None:
        """
        立即重新整理一条失败的整理记录。
        """
        self.post_message(
            Message(
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                title=f"开始重新整理记录 #{history_id} ...",
                save_history=False,
            )
        )

        state, errmsg = self.redo_transfer_history(history_id)
        if state:
            public_message = public_error_message(errmsg, context="transfer") if errmsg else ""
            self.post_message(
                Message(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title=public_message or f"整理记录 #{history_id} 已重新整理",
                    link=self.runtime_config.history_url,
                    save_history=False,
                )
            )
            return

        self.post_message(
            Message(
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                title="重新整理失败",
                text=public_error_message(errmsg, context="transfer"),
                link=self.runtime_config.history_url,
                save_history=False,
            )
        )

    def _take_over_transfer_history_by_ai(
            self,
            history_id: int,
            channel: NotificationChannel,
            source: Optional[str],
            userid: Union[str, int],
            username: Optional[str],
    ) -> None:
        """
        由智能助手接管一条失败的整理记录。
        """

        history = self.transfer_history_repository.get(history_id)
        if not history:
            host = cast(TransferMixinHost, self)
            host.post_message(
                Message(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title="重新整理失败",
                    text=f"整理记录 #{history_id} 不存在",
                    link=host.runtime_config.history_url,
                    save_history=False,
                )
            )
            return

        durable_retry = _request_durable_transfer_retry(
            history,
            requested_by="ai_retry_button",
            repository=self.transfer_execution_repository,
        )
        if durable_retry is not None:
            accepted, message = durable_retry
            public_message = public_error_message(message, context="transfer")
            self.post_message(
                Message(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title=public_message if accepted else "重新整理失败",
                    text=None if accepted else public_message,
                    link=self.runtime_config.history_url,
                    save_history=False,
                )
            )
            return

        if not self.runtime_config.ai_agent_enable:
            self.post_message(
                Message(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title="MoviePilot智能助手未启用，请在系统设置中启用",
                    save_history=False,
                )
            )
            return

        redo_prompt = build_manual_redo_prompt(history)

        async def _run_ai_takeover():
            final_output = ""

            def _capture_output(text_output: str):
                nonlocal final_output
                final_output = text_output or ""

            try:
                await self.async_post_message(
                    Message(
                        channel=channel,
                        source=source,
                        userid=userid,
                        username=username,
                        title=f"已将整理记录 #{history_id} 交给智能助手处理",
                        text="处理完成后会在这里回复结果。",
                        link=self.runtime_config.history_url,
                        save_history=False,
                    )
                )
                manager = get_running_agent_manager()
                if manager is None:
                    raise RuntimeError("智能助手服务未运行")
                await manager.run_background_prompt(
                    message=redo_prompt,
                    session_prefix=f"__agent_manual_redo_{history_id}",
                    output_callback=_capture_output,
                    reply_mode=ReplyMode.CAPTURE_ONLY,
                    allow_message_tools=False,
                )
                await self.async_post_message(
                    Message(
                        channel=channel,
                        source=source,
                        userid=userid,
                        username=username,
                        title="智能助手整理完成",
                        text=final_output.strip()
                             or f"整理记录 #{history_id} 已由智能助手处理完成。",
                        link=self.runtime_config.history_url,
                        save_history=False,
                    )
                )
            except Exception as e:
                logger.error(f"智能助手重新整理失败：{e}", exc_info=True)
                await self.async_post_message(
                    Message(
                        channel=channel,
                        source=source,
                        userid=userid,
                        username=username,
                        title="智能助手整理失败",
                        text="智能助手整理失败，请稍后重试",
                        link=self.runtime_config.history_url,
                        save_history=False,
                    )
                )

        try:
            registry = get_task_registry()
            loop = main_loop_registry.require()
            registry.submit_threadsafe(
                _run_ai_takeover(),
                loop=loop,
                owner="chain.transfer.ai_takeover",
            )
        except RuntimeError as error:
            logger.warning("智能助手整理任务提交失败：%s", error)
            self.post_message(
                Message(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    title="智能助手整理失败",
                    text="系统正在关闭，无法提交处理任务，请稍后重试。",
                    link=self.runtime_config.history_url,
                    save_history=False,
                )
            )
            return

    def _re_transfer(
            self,
            logid: int,
            mtype: MediaType = None,
            media_source: Optional[MediaSource] = None,
            media_id: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """
        根据历史记录，重新识别整理，只支持简单条件
        :param logid: 历史记录ID
        :param mtype: 媒体类型
        :param media_source: 媒体数据源
        :param media_id: 数据源原生 ID，必须与 media_source 成对提供
        """
        # 查询历史记录
        history = self.transfer_history_repository.get(logid)
        if not history:
            logger.error(f"整理记录不存在，ID：{logid}")
            return False, "整理记录不存在"
        # 显式媒体身份代表用户要求重新规划；普通 /redo 才复用原 durable 计划。
        explicit_identity = media_source is not None or media_id is not None
        if explicit_identity and (not media_source or not media_id):
            return False, "媒体重新识别需要同时提供 media_source 和 media_id"
        if not explicit_identity:
            durable_retry = _request_durable_transfer_retry(
                history,
                requested_by="history_redo",
                repository=self.transfer_execution_repository,
            )
            if durable_retry is not None:
                return durable_retry
        # 按源目录路径重新整理
        src_path = Path(history.src)
        if not src_path.exists():
            return False, f"源目录不存在：{src_path}"
        # 查询媒体信息
        if explicit_identity:
            mediainfo = MediaChain().recognize_media(
                mtype=mtype,
                media_source=media_source,
                media_id=media_id,
                music_type=(
                    getattr(history, "music_type", None)
                    if mtype == MediaType.MUSIC
                    else None
                ),
                episode_group=history.episode_group,
            )
            if mediainfo and not isinstance(mediainfo, MusicInfo):
                # 更新媒体图片
                self.obtain_images(mediainfo=mediainfo)
        elif history.media_source and history.media_id:
            try:
                history_type = mtype or MediaType(history.type)
            except ValueError:
                history_type = mtype
            mediainfo = MediaChain().recognize_media(
                mtype=history_type,
                media_source=history.media_source,
                media_id=history.media_id,
                music_type=(
                    getattr(history, "music_type", None)
                    if history_type == MediaType.MUSIC
                    else None
                ),
                episode_group=history.episode_group,
            )
            mtype = history_type
            if mediainfo and not isinstance(mediainfo, MusicInfo):
                self.obtain_images(mediainfo=mediainfo)
        elif mtype == MediaType.MUSIC or self._is_music_retry_source(history, src_path):
            # 音乐重新整理走音乐识别链，避免默认影视识别误入 TMDB
            mtype = MediaType.MUSIC
            mediainfo = self._recognize_music_retry_media(history, src_path)
        else:
            recognize_context = MediaChain().recognize_by_path(
                str(src_path),
                episode_group=history.episode_group,
                obtain_images=True,
            )
            mediainfo = recognize_context.media_info if recognize_context else None
        # 音乐专辑目录允许无预识别信息，由整理链按音频后缀逐文件解析识别
        if not mediainfo and not (mtype == MediaType.MUSIC and src_path.is_dir()):
            return False, "未识别到媒体信息，请检查媒体来源和媒体 ID 后重试"
        # 重新执行整理
        if mediainfo:
            logger.info(f"{src_path.name} 识别为：{mediainfo.title_year}")

        # 删除旧的已整理文件
        if getattr(history, "transfer_task_id", None):
            state, errmsg = self._delete_manual_transfer_history(
                history=history,
                transfer_history_oper=self.transfer_history_repository,
            )
            if not state:
                return False, errmsg
        elif history.dest_fileitem:
            if not isinstance(history.dest_fileitem, dict):
                return False, "目标文件历史数据无效"
            # 解析目标文件对象
            dest_fileitem = FileItem(**history.dest_fileitem)
            StorageChain().delete_file(dest_fileitem)

        # 强制整理
        if history.src_fileitem:
            if not isinstance(history.src_fileitem, dict):
                return False, "源文件历史数据无效"
            state, errmsg = self.do_transfer(
                fileitem=FileItem(**history.src_fileitem),
                mediainfo=mediainfo,
                mtype=mtype,
                media_source=media_source,
                media_id=media_id,
                download_hash=history.download_hash,
                force=True,
                background=False,
                manual=True,
            )
            if not state:
                return False, errmsg

        return True, ""
