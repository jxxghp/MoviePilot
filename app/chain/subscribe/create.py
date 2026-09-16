"""订阅创建与批量写入编排"""

from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Dict, Optional, Tuple, cast

from app.application.subscription.write import (
    SubscriptionBatchWritePort,
    SubscriptionCreateRequest,
    add_subscribe,
    async_add_subscribe,
    build_subscription_create_request,
)
from app.chain.media import MediaChain
from app.chain.subscribe.context import _SubscribeCreateContext, _SubscribePostCommitContext
from app.chain.subscribe.contract import _SubscribeOwnerBase
from app.chain.subscribe.identity import media_recognize_kwargs
from app.domain.meta.metamusic import MetaMusic
from app.domain.metainfo import MetaInfo
from app.runtime.log import logger
from app.schemas.common import JsonData
from app.schemas.media import resolve_media_identity
from app.schemas.types import (
    MediaSource,
    MediaType,
    NotificationChannel,
)


def _build_post_commit_callback(
    owner: Any,
    context: _SubscribePostCommitContext,
) -> Callable[[int], bool]:
    """冻结同步订阅提交后的副作用上下文。"""

    def after_commit(subscribe_id: int) -> bool:
        """把同步提交后的副作用委托给通知 owner。"""
        return cast(bool, owner._SubscribeChain__post_subscribe_added(subscribe_id, context))

    return after_commit


def _build_async_post_commit_callback(
    owner: Any,
    context: _SubscribePostCommitContext,
) -> Callable[[int], Awaitable[bool]]:
    """冻结异步订阅提交后的副作用上下文。"""

    async def after_commit(subscribe_id: int) -> bool:
        """把异步提交后的副作用委托给通知 owner。"""
        return cast(bool, await owner._SubscribeChain__async_post_subscribe_added(subscribe_id, context))

    return after_commit


class SubscribeCreateOwner(_SubscribeOwnerBase):
    """订阅创建与批量写入编排，作为 SubscribeChain 的单一职责实现 owner。"""

    def _SubscribeChain__get_default_kwargs(self, mtype: MediaType, **kwargs: Any) -> dict[str, Any]:
        """
        获取订阅默认配置
        :param mtype: 媒体类型
        :param key: 配置键
        :return: 配置值
        """
        defaults = {
            "quality": self._SubscribeChain__get_default_subscribe_config(mtype, "quality")
            if not kwargs.get("quality")
            else kwargs.get("quality"),
            "resolution": self._SubscribeChain__get_default_subscribe_config(mtype, "resolution")
            if not kwargs.get("resolution")
            else kwargs.get("resolution"),
            "effect": self._SubscribeChain__get_default_subscribe_config(mtype, "effect")
            if not kwargs.get("effect")
            else kwargs.get("effect"),
            "audio_quality": self._SubscribeChain__get_default_subscribe_config(mtype, "audio_quality")
            if not kwargs.get("audio_quality")
            else kwargs.get("audio_quality"),
            "audio_format": self._SubscribeChain__get_default_subscribe_config(mtype, "audio_format")
            if not kwargs.get("audio_format")
            else kwargs.get("audio_format"),
            "min_bitrate": self._SubscribeChain__get_default_subscribe_config(mtype, "min_bitrate")
            if not kwargs.get("min_bitrate")
            else kwargs.get("min_bitrate"),
            "min_bit_depth": self._SubscribeChain__get_default_subscribe_config(mtype, "min_bit_depth")
            if not kwargs.get("min_bit_depth")
            else kwargs.get("min_bit_depth"),
            "min_sample_rate": self._SubscribeChain__get_default_subscribe_config(mtype, "min_sample_rate")
            if not kwargs.get("min_sample_rate")
            else kwargs.get("min_sample_rate"),
            "include": self._SubscribeChain__get_default_subscribe_config(mtype, "include")
            if not kwargs.get("include")
            else kwargs.get("include"),
            "exclude": self._SubscribeChain__get_default_subscribe_config(mtype, "exclude")
            if not kwargs.get("exclude")
            else kwargs.get("exclude"),
            "best_version": self._SubscribeChain__get_default_subscribe_config(mtype, "best_version")
            if kwargs.get("best_version") is None
            else kwargs.get("best_version"),
            "best_version_full": self._SubscribeChain__get_default_subscribe_config(mtype, "best_version_full")
            if kwargs.get("best_version_full") is None
            else kwargs.get("best_version_full"),
            "search_imdbid": self._SubscribeChain__get_default_subscribe_config(mtype, "search_imdbid")
            if not kwargs.get("search_imdbid")
            else kwargs.get("search_imdbid"),
            "sites": self._SubscribeChain__get_default_subscribe_config(mtype, "sites") or None
            if not kwargs.get("sites")
            else kwargs.get("sites"),
            "downloader": self._SubscribeChain__get_default_subscribe_config(mtype, "downloader")
            if not kwargs.get("downloader")
            else kwargs.get("downloader"),
            "save_path": self._SubscribeChain__get_default_subscribe_config(mtype, "save_path")
            if not kwargs.get("save_path")
            else kwargs.get("save_path"),
            "filter_groups": self._SubscribeChain__get_default_subscribe_config(mtype, "filter_groups")
            if not kwargs.get("filter_groups")
            else kwargs.get("filter_groups"),
        }
        if mtype == MediaType.MUSIC:
            # 音乐允许按音质洗版，但没有电视剧整包洗版和 IMDB 搜索语义。
            defaults.update(
                {
                    "best_version_full": 0,
                    "search_imdbid": 0,
                }
            )
        return defaults

    @staticmethod
    def _SubscribeChain__build_subscribe_create_context(
        title: str,
        year: str,
        mtype: Optional[MediaType],
        episode_group: Optional[str],
        season: Optional[int],
        channel: Optional[NotificationChannel],
        source: Optional[str],
        userid: Optional[str],
        username: Optional[str],
        message: bool,
        exist_ok: bool,
        media_source: Optional[MediaSource],
        media_id: Optional[str],
        options: Dict[str, Any],
    ) -> Tuple[Optional[_SubscribeCreateContext], Optional[str]]:
        """规范订阅新增输入，并在任何识别 I/O 前拒绝不完整的显式媒体身份。"""
        explicit_identity = media_source is not None or media_id is not None
        media_source, media_id = resolve_media_identity(
            media_source=media_source,
            media_id=media_id,
        )
        if explicit_identity and (not media_source or not media_id):
            return None, "媒体来源和媒体 ID 必须同时提供"

        metainfo = MetaMusic.parse_query(title) if mtype == MediaType.MUSIC else MetaInfo(title)
        if year:
            metainfo.year = year
        if mtype:
            metainfo.type = mtype
        if season is not None:
            metainfo.type = MediaType.TV
            metainfo.begin_season = season
        if mtype == MediaType.MUSIC and media_id:
            metainfo.media_id = str(media_id)

        return _SubscribeCreateContext(
            title=title,
            year=year,
            mtype=mtype,
            episode_group=episode_group,
            season=season,
            channel=channel,
            source=source,
            userid=userid,
            username=username,
            message=bool(message),
            exist_ok=bool(exist_ok),
            options=options,
            explicit_identity=explicit_identity,
            media_source=media_source,
            media_id=media_id,
            requested_music_type=options.get("music_type"),
            metainfo=metainfo,
        ), None

    @staticmethod
    def _SubscribeChain__normalize_recognized_subscribe_media(context: _SubscribeCreateContext) -> None:
        """保留非 TMDB 影视标题清洗与从标题补季号的历史行为。"""
        mediainfo = context.mediainfo
        if context.mtype == MediaType.MUSIC or not mediainfo or mediainfo.media_source == MediaSource.TMDB:
            return
        meta = MetaInfo(mediainfo.title)
        mediainfo.title = meta.name
        if context.season is None:
            context.season = meta.begin_season

    def _SubscribeChain__recognize_subscribe_media(self, context: _SubscribeCreateContext) -> Optional[str]:
        """同步识别订阅目标；显式身份失败时禁止按标题换成另一个媒体。"""
        if context.media_source and context.media_id:
            context.mediainfo = MediaChain().recognize_media(
                meta=context.metainfo,
                mtype=context.mtype,
                media_source=context.media_source,
                media_id=context.media_id,
                music_type=context.requested_music_type,
                episode_group=context.episode_group,
                cache=False,
            )
        self._SubscribeChain__normalize_recognized_subscribe_media(context)
        if not context.mediainfo and not context.explicit_identity:
            context.mediainfo = MediaChain().recognize_by_meta(
                context.metainfo,
                media_source=context.media_source,
                episode_group=context.episode_group,
                obtain_images=False,
                music_type=context.requested_music_type,
            )
            if context.mtype == MediaType.MUSIC and context.mediainfo and not context.mediainfo.media_source:
                context.mediainfo = None
        return self._SubscribeChain__validate_recognized_subscribe_media(context)

    async def _SubscribeChain__async_recognize_subscribe_media(
        self,
        context: _SubscribeCreateContext,
    ) -> Optional[str]:
        """异步识别订阅目标，并与同步入口共享相同的规范化和校验规则。"""
        if context.media_source and context.media_id:
            context.mediainfo = await MediaChain().async_recognize_media(
                meta=context.metainfo,
                mtype=context.mtype,
                media_source=context.media_source,
                media_id=context.media_id,
                music_type=context.requested_music_type,
                episode_group=context.episode_group,
                cache=False,
            )
        self._SubscribeChain__normalize_recognized_subscribe_media(context)
        if not context.mediainfo and not context.explicit_identity:
            context.mediainfo = await MediaChain().async_recognize_by_meta(
                context.metainfo,
                media_source=context.media_source,
                episode_group=context.episode_group,
                obtain_images=False,
                music_type=context.requested_music_type,
            )
            if context.mtype == MediaType.MUSIC and context.mediainfo and not context.mediainfo.media_source:
                context.mediainfo = None
        return self._SubscribeChain__validate_recognized_subscribe_media(context)

    def _SubscribeChain__validate_recognized_subscribe_media(
        self,
        context: _SubscribeCreateContext,
    ) -> Optional[str]:
        """校验识别结果以及音乐订阅实体，返回兼容旧入口的错误文案。"""
        if not context.mediainfo:
            logger.warning(
                f"未识别到媒体信息，标题：{context.title}，媒体来源：{context.media_source}，"
                f"媒体 ID：{context.media_id}"
            )
            return "未识别到媒体信息"
        if context.mtype != MediaType.MUSIC:
            return None
        music_error = self._validate_music_subscribe_target(
            context.mediainfo,
            requested_music_type=context.requested_music_type,
        )
        if music_error:
            logger.warning(f"音乐订阅目标校验失败：{context.title} - {music_error}")
        return cast(Optional[str], music_error)

    def _SubscribeChain__prepare_subscribe_episodes(self, context: _SubscribeCreateContext) -> Optional[str]:
        """同步补齐电视剧季集信息，并保持外部集数刷新只能扩展创建目标。"""
        mediainfo = context.mediainfo
        if mediainfo.type != MediaType.TV:
            context.season = None
            return None
        if context.season is None:
            context.season = 1
        if not context.options.get("total_episode"):
            if not mediainfo.seasons or context.episode_group:
                mediainfo = MediaChain().recognize_media(
                    mtype=mediainfo.type,
                    **media_recognize_kwargs(mediainfo),
                    episode_group=context.episode_group,
                    cache=False,
                )
                context.mediainfo = mediainfo
                error = self._SubscribeChain__validate_subscribe_seasons(context)
                if error:
                    return error
            current_total_episode = len(mediainfo.seasons.get(context.season) or [])
            total_episode = self._SubscribeChain__apply_episodes_refresh(
                current_total_episode,
                season=context.season,
                mediainfo=mediainfo,
                media_source=resolve_media_identity(media=mediainfo)[0],
                media_id=resolve_media_identity(media=mediainfo)[1],
                scene="create",
            )
            error = self._SubscribeChain__store_subscribe_episode_total(
                context,
                current_total_episode,
                total_episode,
            )
            if error:
                return error
        self._SubscribeChain__fill_subscribe_lack_episode(context)
        return None

    async def _SubscribeChain__async_prepare_subscribe_episodes(
        self,
        context: _SubscribeCreateContext,
    ) -> Optional[str]:
        """异步补齐电视剧季集信息，并复用同步入口的结果校验和字段写入规则。"""
        mediainfo = context.mediainfo
        if mediainfo.type != MediaType.TV:
            context.season = None
            return None
        if context.season is None:
            context.season = 1
        if not context.options.get("total_episode"):
            if not mediainfo.seasons or context.episode_group:
                mediainfo = await MediaChain().async_recognize_media(
                    mtype=mediainfo.type,
                    **media_recognize_kwargs(mediainfo),
                    episode_group=context.episode_group,
                    cache=False,
                )
                context.mediainfo = mediainfo
                error = self._SubscribeChain__validate_subscribe_seasons(context)
                if error:
                    return error
            current_total_episode = len(mediainfo.seasons.get(context.season) or [])
            total_episode = await self._SubscribeChain__async_apply_episodes_refresh(
                current_total_episode,
                season=context.season,
                mediainfo=mediainfo,
                media_source=resolve_media_identity(media=mediainfo)[0],
                media_id=resolve_media_identity(media=mediainfo)[1],
                scene="create",
            )
            error = self._SubscribeChain__store_subscribe_episode_total(
                context,
                current_total_episode,
                total_episode,
            )
            if error:
                return error
        self._SubscribeChain__fill_subscribe_lack_episode(context)
        return None

    @staticmethod
    def _SubscribeChain__validate_subscribe_seasons(context: _SubscribeCreateContext) -> Optional[str]:
        """校验补充识别结果是否仍包含创建电视剧订阅所需的季集信息。"""
        if not context.mediainfo:
            logger.error("媒体信息识别失败！")
            return "媒体信息识别失败"
        if not context.mediainfo.seasons:
            logger.error(f"媒体信息中没有季集信息，标题：{context.title}")
            return "媒体信息中没有季集信息"
        return None

    @staticmethod
    def _SubscribeChain__store_subscribe_episode_total(
        context: _SubscribeCreateContext,
        current_total_episode: int,
        total_episode: int,
    ) -> Optional[str]:
        """写入创建场景最终集数，阻止外部刷新把可靠的当前集数向下覆盖。"""
        if current_total_episode and total_episode < current_total_episode:
            total_episode = current_total_episode
        if not total_episode:
            logger.error(f"未获取到总集数，标题：{context.title}")
            return f"未获取到第 {context.season} 季的总集数"
        context.options["total_episode"] = total_episode
        return None

    @staticmethod
    def _SubscribeChain__fill_subscribe_lack_episode(context: _SubscribeCreateContext) -> None:
        """未显式指定缺失集数时沿用总集数，保持旧创建默认值。"""
        if not context.options.get("lack_episode"):
            context.options["lack_episode"] = context.options.get("total_episode")

    def _SubscribeChain__finalize_subscribe_create_context(self, context: _SubscribeCreateContext) -> None:
        """同步补图并写入规范媒体身份和订阅默认配置。"""
        if context.mediainfo.type != MediaType.MUSIC:
            self.obtain_images(mediainfo=context.mediainfo)
        self._SubscribeChain__apply_subscribe_create_defaults(context)

    async def _SubscribeChain__async_finalize_subscribe_create_context(
        self,
        context: _SubscribeCreateContext,
    ) -> None:
        """异步补图并写入规范媒体身份和订阅默认配置。"""
        if context.mediainfo.type != MediaType.MUSIC:
            await self.async_obtain_images(mediainfo=context.mediainfo)
        self._SubscribeChain__apply_subscribe_create_defaults(context)

    def _SubscribeChain__apply_subscribe_create_defaults(self, context: _SubscribeCreateContext) -> None:
        """以最终识别结果覆盖身份字段，并补齐当前媒体类型的默认订阅参数。"""
        context.media_source, context.media_id = resolve_media_identity(media=context.mediainfo)
        context.options.update(
            {
                "media_source": context.media_source,
                "media_id": context.media_id,
            }
        )
        context.options.update(self._SubscribeChain__get_default_kwargs(context.mediainfo.type, **context.options))

    @staticmethod
    def _SubscribeChain__subscribe_post_commit_context(
        context: _SubscribeCreateContext,
        notification: Optional[dict[str, Any]] = None,
    ) -> _SubscribePostCommitContext:
        """从创建阶段状态冻结提交后副作用需要的最小快照。"""
        return _SubscribePostCommitContext(
            title=context.title,
            year=context.year,
            metainfo=context.metainfo,
            mediainfo=context.mediainfo,
            media_source=context.media_source,
            media_id=context.media_id,
            season=context.season,
            channel=context.channel,
            source=context.source,
            userid=context.userid,
            username=context.username,
            message=context.message,
            notification=notification,
        )

    def _SubscribeChain__persist_subscribe_create(self, context: _SubscribeCreateContext) -> Tuple[Optional[int], str]:
        """同步提交订阅，并在提交成功后按原顺序执行消息、事件和统计。"""
        post_commit_context = self._SubscribeChain__subscribe_post_commit_context(
            context,
            self._SubscribeChain__build_subscribe_notification(context),
        )
        try:
            sid, err_msg = add_subscribe(
                mediainfo=context.mediainfo,
                subscribe_oper=self.subscription_repository,
                season=context.season,
                username=context.username,
                after_commit=_build_post_commit_callback(self, post_commit_context),
                notification=post_commit_context.notification,
                occurrence_id=post_commit_context.occurrence_id,
                **context.options,
            )
        except ValueError as error:
            logger.error(f"订阅分类设置无效：{error}", exc_info=True)
            err_msg = "订阅分类设置无效，请重新选择分类后重试"
            self._SubscribeChain__notify_subscribe_create_failure(context, err_msg)
            return None, err_msg
        if not sid:
            self._SubscribeChain__notify_subscribe_create_failure(context, err_msg)
            return None, err_msg
        return sid, err_msg

    async def _SubscribeChain__async_persist_subscribe_create(
        self,
        context: _SubscribeCreateContext,
    ) -> Tuple[Optional[int], str]:
        """异步提交订阅，并在提交成功后按原顺序执行消息、事件和统计。"""
        post_commit_context = self._SubscribeChain__subscribe_post_commit_context(
            context,
            self._SubscribeChain__build_subscribe_notification(context),
        )
        try:
            sid, err_msg = await async_add_subscribe(
                mediainfo=context.mediainfo,
                subscribe_oper=self.subscription_repository,
                season=context.season,
                username=context.username,
                after_commit=_build_async_post_commit_callback(self, post_commit_context),
                notification=post_commit_context.notification,
                occurrence_id=post_commit_context.occurrence_id,
                **context.options,
            )
        except ValueError as error:
            logger.error(f"订阅分类设置无效：{error}", exc_info=True)
            err_msg = "订阅分类设置无效，请重新选择分类后重试"
            await self._SubscribeChain__async_notify_subscribe_create_failure(
                context,
                err_msg,
            )
            return None, err_msg
        if not sid:
            await self._SubscribeChain__async_notify_subscribe_create_failure(context, err_msg)
            return None, err_msg
        return sid, err_msg

    def add(
        self,
        title: str,
        year: str,
        mtype: Optional[MediaType] = None,
        episode_group: Optional[str] = None,
        season: Optional[int] = None,
        channel: Optional[NotificationChannel] = None,
        source: Optional[str] = None,
        userid: Optional[str] = None,
        username: Optional[str] = None,
        message: Optional[bool] = True,
        exist_ok: Optional[bool] = False,
        media_source: Optional[MediaSource] = None,
        media_id: Optional[str] = None,
        **kwargs: Any,
    ) -> Tuple[Optional[int], str]:
        """
        识别媒体信息并添加订阅
        """
        logger.info(f"开始添加订阅，标题：{title} ...")
        context, error = self._SubscribeChain__build_subscribe_create_context(
            title,
            year,
            mtype,
            episode_group,
            season,
            channel,
            source,
            userid,
            username,
            bool(message),
            bool(exist_ok),
            media_source,
            media_id,
            kwargs,
        )
        if error:
            return None, error
        error = self._SubscribeChain__recognize_subscribe_media(context)
        if error:
            return None, error
        error = self._SubscribeChain__prepare_subscribe_episodes(context)
        if error:
            return None, error
        self._SubscribeChain__finalize_subscribe_create_context(context)
        return self._SubscribeChain__persist_subscribe_create(context)

    async def async_add(
        self,
        title: str,
        year: str,
        mtype: Optional[MediaType] = None,
        episode_group: Optional[str] = None,
        season: Optional[int] = None,
        channel: Optional[NotificationChannel] = None,
        source: Optional[str] = None,
        userid: Optional[str] = None,
        username: Optional[str] = None,
        message: Optional[bool] = True,
        exist_ok: Optional[bool] = False,
        media_source: Optional[MediaSource] = None,
        media_id: Optional[str] = None,
        **kwargs: Any,
    ) -> Tuple[Optional[int], str]:
        """
        异步识别媒体信息并添加订阅
        """
        logger.info(f"开始添加订阅，标题：{title} ...")
        context, error = await self._SubscribeChain__async_prepare_subscribe_create(
            title,
            year,
            mtype,
            episode_group,
            season,
            channel,
            source,
            userid,
            username,
            bool(message),
            bool(exist_ok),
            media_source,
            media_id,
            kwargs,
        )
        if error or context is None:
            return None, error or "订阅准备失败"
        return await self._SubscribeChain__async_persist_subscribe_create(context)

    async def _SubscribeChain__async_prepare_subscribe_create(
        self,
        title: str,
        year: str,
        mtype: Optional[MediaType],
        episode_group: Optional[str],
        season: Optional[int],
        channel: Optional[NotificationChannel],
        source: Optional[str],
        userid: Optional[str],
        username: Optional[str],
        message: bool,
        exist_ok: bool,
        media_source: Optional[MediaSource],
        media_id: Optional[str],
        options: Dict[str, Any],
    ) -> Tuple[Optional[_SubscribeCreateContext], Optional[str]]:
        """执行异步新增的识别、季集补齐和默认配置准备，但不触发持久化。"""
        context, error = self._SubscribeChain__build_subscribe_create_context(
            title,
            year,
            mtype,
            episode_group,
            season,
            channel,
            source,
            userid,
            username,
            message,
            exist_ok,
            media_source,
            media_id,
            options,
        )
        if error or context is None:
            return None, error
        error = await self._SubscribeChain__async_recognize_subscribe_media(context)
        if error:
            return None, error
        error = await self._SubscribeChain__async_prepare_subscribe_episodes(context)
        if error:
            return None, error
        await self._SubscribeChain__async_finalize_subscribe_create_context(context)
        return context, None

    async def async_add_batch(
        self,
        *,
        title: str,
        year: str,
        seasons: Sequence[int],
        batch_writer: SubscriptionBatchWritePort,
        mtype: MediaType = None,
        episode_group: Optional[str] = None,
        channel: NotificationChannel = None,
        source: Optional[str] = None,
        userid: Optional[str] = None,
        username: Optional[str] = None,
        message: Optional[bool] = True,
        exist_ok: Optional[bool] = False,
        media_source: Optional[MediaSource] = None,
        media_id: Optional[str] = None,
        **kwargs: JsonData,
    ) -> Tuple[Optional[int], str]:
        """先完整准备各季订阅，再交给共享事务批量写端口原子提交。"""
        requests: list[SubscriptionCreateRequest] = []
        for season in seasons:
            context, error = await self._SubscribeChain__async_prepare_subscribe_create(
                title,
                year,
                mtype,
                episode_group,
                season,
                channel,
                source,
                userid,
                username,
                bool(message),
                bool(exist_ok),
                media_source,
                media_id,
                dict(kwargs),
            )
            if error or context is None:
                return None, error or "订阅准备失败"
            assert context.mediainfo is not None
            notification = self._SubscribeChain__build_subscribe_notification(context)
            post_commit_context = self._SubscribeChain__subscribe_post_commit_context(
                context,
                notification,
            )

            request = build_subscription_create_request(
                context.mediainfo,
                notification=notification,
                after_commit=_build_async_post_commit_callback(self, post_commit_context),
                occurrence_id=post_commit_context.occurrence_id,
                season=context.season,
                username=context.username,
                **context.options,
            )
            if request is None:
                return None, "媒体身份不完整"
            requests.append(request)

        results = await batch_writer.async_add(requests)
        if not results:
            return None, "未提供订阅季"
        result = results[-1]
        return result.subscribe_id or None, result.message
