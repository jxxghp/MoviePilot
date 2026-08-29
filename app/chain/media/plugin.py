"""插件识别选择、事件解析与辅助识别 owner。"""

from dataclasses import dataclass
from enum import Enum
from typing import (
    TYPE_CHECKING,
    Awaitable,
    Callable,
    Generator,
    Mapping,
    Optional,
    Protocol,
    Tuple,
    cast,
)

from app.application.configuration import get_chain_runtime_config_snapshot
from app.chain.media.contract import _MediaOwnerBase
from app.domain.context import (
    MediaInfo,
)
from app.domain.meta.metabase import MetaBase
from app.domain.meta.metamusic import MetaMusic
from app.runtime.log import logger
from app.schemas.types import (
    ChainEventType,
    MediaSource,
    MediaType,
)

RecognitionCallback = Callable[[], Optional[MediaInfo]]
AsyncRecognitionCallback = Callable[[], Awaitable[Optional[MediaInfo]]]
RecognitionPredicate = Callable[[Optional[MediaInfo]], bool]


class _RecognitionSource(Enum):
    """标识一次识别选择流程当前需要调用的真实 I/O 来源。"""

    NATIVE = "native"
    PLUGIN = "plugin"


@dataclass(frozen=True, slots=True)
class _RecognitionSelection:
    """统一同步与异步识别入口的来源顺序、回退和结果采信决策。"""

    plugin_first: bool
    plugin_available: bool
    is_recognized: RecognitionPredicate

    def flow(
        self,
    ) -> Generator[_RecognitionSource, Optional[MediaInfo], Optional[MediaInfo]]:
        """按相同状态机产出 I/O 动作，并接收各入口自己的调用结果。"""
        if self.plugin_first and self.plugin_available:
            helped = yield _RecognitionSource.PLUGIN
            if self.is_recognized(helped):
                return helped
            native = yield _RecognitionSource.NATIVE
            return native or helped

        native = yield _RecognitionSource.NATIVE
        if self.is_recognized(native) or not self.plugin_available:
            return native
        helped = yield _RecognitionSource.PLUGIN
        return helped if self.is_recognized(helped) else native


def _normalize_music_year(value: object) -> Optional[int]:
    """把本地或插件音乐年份统一为正整数，空值和非法文本返回空。"""
    text = str(value or "").strip()
    return int(text) if text.isdigit() else None


class _EventResult(Protocol):
    """声明插件链式事件返回值所需的最小读取合同。"""

    @property
    def event_data(self) -> Mapping[str, object]:
        """返回插件写入的链式事件数据。"""
        ...


class _EventManagerPort(Protocol):
    """声明插件识别 owner 使用的事件管理器能力。"""

    def check(self, event_type: ChainEventType) -> bool:
        """返回指定链式事件是否存在可用处理器。"""
        ...

    def send_event(
        self,
        event_type: ChainEventType,
        data: Mapping[str, object],
    ) -> Optional[_EventResult]:
        """同步发送链式事件并返回插件处理结果。"""
        ...

    async def async_send_event(
        self,
        event_type: ChainEventType,
        data: Mapping[str, object],
    ) -> Optional[_EventResult]:
        """异步发送链式事件并返回插件处理结果。"""
        ...


if TYPE_CHECKING:

    class _MediaPluginOwnerBase:
        """声明插件识别 owner 依赖的跨 owner 能力。"""

        eventmanager: _EventManagerPort

        def recognize_media(
            self,
            *,
            meta: Optional[MetaBase] = None,
            media_source: Optional[MediaSource] = None,
            share_meta: Optional[MetaBase] = None,
            episode_group: Optional[str] = None,
            music_type: Optional[str] = None,
        ) -> Optional[MediaInfo]:
            """按修正后的元数据同步重新识别媒体。"""
            ...

        async def async_recognize_media(
            self,
            *,
            meta: Optional[MetaBase] = None,
            media_source: Optional[MediaSource] = None,
            share_meta: Optional[MetaBase] = None,
            episode_group: Optional[str] = None,
            music_type: Optional[str] = None,
        ) -> Optional[MediaInfo]:
            """按修正后的元数据异步重新识别媒体。"""
            ...

else:
    _MediaPluginOwnerBase = _MediaOwnerBase


class MediaPluginOwner(_MediaPluginOwnerBase):
    """插件识别选择、事件解析与辅助识别 owner。"""

    def _recognition_selection(
        self,
        plugin_event: ChainEventType,
        is_recognized: Optional[RecognitionPredicate],
    ) -> _RecognitionSelection:
        """冻结本次识别的插件可用性、优先级和结果采信谓词。"""
        return _RecognitionSelection(
            plugin_first=get_chain_runtime_config_snapshot().recognize_plugin_first,
            plugin_available=self.eventmanager.check(plugin_event),
            is_recognized=is_recognized or bool,
        )

    @staticmethod
    def _log_recognition_action(
        action: _RecognitionSource,
        *,
        plugin_first: bool,
        log_name: str,
        log_context: str,
    ) -> None:
        """在状态机请求真实 I/O 前记录统一的来源切换原因。"""
        if action is _RecognitionSource.PLUGIN:
            if plugin_first:
                logger.info(
                    f"插件识别优先模式已开启。请求辅助识别，标题：{log_name} ..."
                )
            else:
                logger.info(
                    f"原生识别未识别到 {log_context} 的媒体信息，尝试使用辅助识别 ..."
                )
            return
        if plugin_first:
            logger.info(
                f"辅助识别未识别到 {log_context} 的媒体信息，尝试使用原生识别 ..."
            )
        else:
            logger.info(f"开始识别标题：{log_name} ...")

    def _run_recognition_selection(
        self,
        selection: _RecognitionSelection,
        *,
        log_name: str,
        log_context: str,
        native_fn: RecognitionCallback,
        plugin_fn: RecognitionCallback,
    ) -> Optional[MediaInfo]:
        """同步驱动共享选择状态机，并只在动作边界调用同步实现。"""
        flow = selection.flow()
        try:
            action = next(flow)
            while True:
                MediaPluginOwner._log_recognition_action(
                    action,
                    plugin_first=selection.plugin_first,
                    log_name=log_name,
                    log_context=log_context,
                )
                result = (
                    plugin_fn()
                    if action is _RecognitionSource.PLUGIN
                    else native_fn()
                )
                action = flow.send(result)
        except StopIteration as completed:
            return cast(Optional[MediaInfo], completed.value)

    async def _async_run_recognition_selection(
        self,
        selection: _RecognitionSelection,
        *,
        log_name: str,
        log_context: str,
        native_fn: AsyncRecognitionCallback,
        plugin_fn: AsyncRecognitionCallback,
    ) -> Optional[MediaInfo]:
        """异步驱动共享选择状态机，并只在动作边界等待异步实现。"""
        flow = selection.flow()
        try:
            action = next(flow)
            while True:
                MediaPluginOwner._log_recognition_action(
                    action,
                    plugin_first=selection.plugin_first,
                    log_name=log_name,
                    log_context=log_context,
                )
                result = (
                    await plugin_fn()
                    if action is _RecognitionSource.PLUGIN
                    else await native_fn()
                )
                action = flow.send(result)
        except StopIteration as completed:
            return cast(Optional[MediaInfo], completed.value)

    def select_recognize_source(
        self,
        log_name: str,
        log_context: str,
        native_fn: RecognitionCallback,
        plugin_fn: RecognitionCallback,
        is_recognized: Optional[RecognitionPredicate] = None,
        plugin_event: ChainEventType = ChainEventType.NameRecognize,
    ) -> Optional[MediaInfo]:
        """
        选择识别模式，插件优先或原生优先

        :param log_name: 用于日志“标题：...”处的名称（如 file_path.name 或 title）
        :param log_context: 用于日志“未识别到...的媒体信息”处的上下文（如 path 或 title）
        :param native_fn: 原生识别函数
        :param plugin_fn: 插件识别函数
        :param is_recognized: 判定识别结果是否有效的谓词；音乐原生兜底结果无远端身份，
            需视为未识别才会请求辅助识别，影视默认按非空判定
        :param plugin_event: 辅助识别对应的链式事件类型，音乐使用音乐名称识别事件
        """
        selection = MediaPluginOwner._recognition_selection(
            self, plugin_event, is_recognized
        )
        return MediaPluginOwner._run_recognition_selection(
            self,
            selection,
            log_name=log_name,
            log_context=log_context,
            native_fn=native_fn,
            plugin_fn=plugin_fn,
        )

    @staticmethod
    def _parse_recognize_event_number(value: object) -> Optional[int]:
        """
        解析辅助识别返回的季集号，兼容整数和数字字符串并保留数值 0。
        """
        if value is None:
            return None
        text = str(value).strip()
        return int(text) if text.isdigit() else None

    def recognize_help(
        self,
        title: str,
        org_meta: MetaBase,
        share_meta: Optional[MetaBase] = None,
        media_source: Optional[MediaSource] = None,
        episode_group: Optional[str] = None,
        music_type: Optional[str] = None,
    ) -> Optional[MediaInfo]:
        """
        请求辅助识别，返回媒体信息；影视与音乐共用同一流程，仅要素事件与重组方式不同

        :param title: 标题
        :param org_meta: 原始元数据
        :param share_meta: 共享识别查询/上报使用的原始元数据
        :param media_source: 请求级识别数据源
        :param episode_group: 剧集组
        :param music_type: 音乐实体类型，仅音乐辅助识别使用
        """
        # 音乐标题要素（曲名/艺术家/专辑/年份）与影视不同，走专用名称识别事件
        if isinstance(org_meta, MetaMusic):
            return self._recognize_music_help(
                title=title,
                org_meta=org_meta,
                share_meta=share_meta,
                media_source=media_source,
                music_type=music_type,
            )
        # 发送请求事件，等待结果
        result = self.eventmanager.send_event(
            ChainEventType.NameRecognize,
            {
                "title": title,
            },
        )
        if not result:
            return None
        # 获取返回事件数据
        event_data = result.event_data or {}
        logger.info(f"获取到辅助识别结果：{event_data}")
        # 处理数据格式
        recognized_title: Optional[str] = None
        recognized_year: Optional[str] = None
        if event_data.get("name"):
            recognized_title = str(event_data["name"]).split("/")[0].strip().replace(".", " ")
        if event_data.get("year"):
            recognized_year = str(event_data["year"]).split("/")[0].strip()
        season_number = self._parse_recognize_event_number(event_data.get("season"))
        episode_number = self._parse_recognize_event_number(event_data.get("episode"))
        if not recognized_title:
            return None
        if recognized_title == "Unknown":
            return None
        if not str(recognized_year).isdigit():
            recognized_year = None
        # 结果赋值
        if recognized_title == org_meta.name and recognized_year == org_meta.year:
            logger.info("辅助识别与原始识别结果一致，无需重新识别媒体信息")
            return None
        logger.info("辅助识别结果与原始识别结果不一致，重新匹配媒体信息 ...")
        org_meta.name = recognized_title
        org_meta.year = recognized_year
        org_meta.begin_season = season_number
        org_meta.begin_episode = episode_number
        if org_meta.begin_season is not None or org_meta.begin_episode is not None:
            org_meta.type = MediaType.TV
        # 重新识别
        return self.recognize_media(
            meta=org_meta,
            media_source=media_source,
            share_meta=share_meta,
            episode_group=episode_group,
        )

    def _recognize_music_help(
        self,
        title: str,
        org_meta: MetaMusic,
        share_meta: Optional[MetaBase] = None,
        media_source: Optional[MediaSource] = None,
        music_type: Optional[str] = None,
    ) -> Optional[MediaInfo]:
        """
        请求插件辅助识别音乐标题要素，并按修正后的要素重新匹配媒体信息

        :param title: 原始音乐标题
        :param org_meta: 原始音乐元数据
        :param share_meta: 共享识别查询/上报使用的原始元数据
        :param media_source: 请求级识别数据源
        :param music_type: 音乐实体类型
        """
        # 发送音乐名称识别事件，等待插件返回标题要素
        result = self.eventmanager.send_event(
            ChainEventType.MusicNameRecognize,
            {
                "title": title,
                "artist": org_meta.artist,
                "album": org_meta.album,
                "year": org_meta.year,
                "music_type": music_type,
            },
        )
        if not result:
            return None
        event_data = result.event_data or {}
        logger.info(f"获取到音乐辅助识别结果：{event_data}")
        name, artist, album, year = self._parse_music_recognize_event(event_data)
        if not name:
            return None
        # 辅助识别要素与原始一致时无需重新匹配
        if (
            name == org_meta.title
            and (not artist or artist in org_meta.artists)
            and (not album or album == org_meta.album)
            and (not year or year == _normalize_music_year(org_meta.year))
        ):
            logger.info("音乐辅助识别与原始识别结果一致，无需重新匹配媒体信息")
            return None
        logger.info("音乐辅助识别结果与原始识别结果不一致，重新匹配媒体信息 ...")
        new_meta = self._build_music_help_meta(
            org_meta=org_meta,
            name=name,
            artist=artist,
            album=album,
            year=year,
        )
        # 重新识别，仅采信取得远端身份的结果，否则由选择流程保留原生兜底
        mediainfo = self.recognize_media(
            meta=new_meta,
            media_source=media_source,
            share_meta=share_meta,
            music_type=music_type,
        )
        return mediainfo if mediainfo and mediainfo.media_source else None

    @staticmethod
    def _parse_music_recognize_event(
        event_data: Mapping[str, object],
    ) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[int]]:
        """
        解析音乐辅助识别返回的标题要素，曲名为空或未知时返回 None
        """
        name = None
        if event_data.get("name"):
            name = str(event_data["name"]).split("/")[0].strip().replace(".", " ")
        artist = None
        if event_data.get("artist"):
            artist = str(event_data["artist"]).split("/")[0].strip()
        album = None
        if event_data.get("album"):
            album = str(event_data["album"]).split("/")[0].strip()
        year = None
        year_text = str(event_data.get("year") or "").split("/")[0].strip()
        if year_text.isdigit():
            year = int(year_text)
        if not name or name == "Unknown":
            name = None
        return name, artist, album, year

    @staticmethod
    def _build_music_help_meta(
        org_meta: MetaMusic,
        name: str,
        artist: Optional[str],
        album: Optional[str],
        year: Optional[int],
    ) -> MetaMusic:
        """按插件修正标题要素，并保留本地轨道与音频判定证据。"""
        return MetaMusic(
            org_string=org_meta.org_string,
            title=name,
            artists=[artist] if artist else list(org_meta.artists or []),
            album=album or org_meta.album,
            album_artist=artist or org_meta.album_artist,
            year=year or _normalize_music_year(org_meta.year),
            disc_number=org_meta.disc_number,
            track_number=org_meta.track_number,
            total_discs=org_meta.total_discs,
            total_tracks=org_meta.total_tracks,
            version=org_meta.version,
            audio_format=org_meta.audio_format,
            audio_lossless=org_meta.audio_lossless,
            bit_depth=org_meta.bit_depth,
            sample_rate=org_meta.sample_rate,
            bitrate=org_meta.bitrate,
            duration=org_meta.duration,
            isrc=org_meta.isrc,
        )

    async def async_select_recognize_source(
        self,
        log_name: str,
        log_context: str,
        native_fn: AsyncRecognitionCallback,
        plugin_fn: AsyncRecognitionCallback,
        is_recognized: Optional[RecognitionPredicate] = None,
        plugin_event: ChainEventType = ChainEventType.NameRecognize,
    ) -> Optional[MediaInfo]:
        """
        选择识别模式，插件优先或原生优先（异步版本）

        :param log_name: 用于日志“标题：...”处的名称（如 file_path.name 或 title）
        :param log_context: 用于日志“未识别到...的媒体信息”处的上下文（如 path 或 title）
        :param native_fn: 原生识别函数
        :param plugin_fn: 插件识别函数
        :param is_recognized: 判定识别结果是否有效的谓词，语义同同步版本
        :param plugin_event: 辅助识别对应的链式事件类型，音乐使用音乐名称识别事件
        """
        selection = MediaPluginOwner._recognition_selection(
            self, plugin_event, is_recognized
        )
        return await MediaPluginOwner._async_run_recognition_selection(
            self,
            selection,
            log_name=log_name,
            log_context=log_context,
            native_fn=native_fn,
            plugin_fn=plugin_fn,
        )

    async def async_recognize_help(
        self,
        title: str,
        org_meta: MetaBase,
        share_meta: Optional[MetaBase] = None,
        media_source: Optional[MediaSource] = None,
        episode_group: Optional[str] = None,
        music_type: Optional[str] = None,
    ) -> Optional[MediaInfo]:
        """
        请求辅助识别，返回媒体信息（异步版本）；影视与音乐共用同一流程

        :param title: 标题
        :param org_meta: 原始元数据
        :param share_meta: 共享识别查询/上报使用的原始元数据
        :param media_source: 请求级识别数据源
        :param episode_group: 剧集组
        :param music_type: 音乐实体类型，仅音乐辅助识别使用
        """
        # 音乐标题要素（曲名/艺术家/专辑/年份）与影视不同，走专用名称识别事件
        if isinstance(org_meta, MetaMusic):
            return await self._async_recognize_music_help(
                title=title,
                org_meta=org_meta,
                share_meta=share_meta,
                media_source=media_source,
                music_type=music_type,
            )
        # 发送请求事件，等待结果
        result = await self.eventmanager.async_send_event(
            ChainEventType.NameRecognize,
            {
                "title": title,
            },
        )
        if not result:
            return None
        # 获取返回事件数据
        event_data = result.event_data or {}
        logger.info(f"获取到辅助识别结果：{event_data}")
        # 处理数据格式
        recognized_title: Optional[str] = None
        recognized_year: Optional[str] = None
        if event_data.get("name"):
            recognized_title = str(event_data["name"]).split("/")[0].strip().replace(".", " ")
        if event_data.get("year"):
            recognized_year = str(event_data["year"]).split("/")[0].strip()
        season_number = self._parse_recognize_event_number(event_data.get("season"))
        episode_number = self._parse_recognize_event_number(event_data.get("episode"))
        if not recognized_title:
            return None
        if recognized_title == "Unknown":
            return None
        if not str(recognized_year).isdigit():
            recognized_year = None
        # 结果赋值
        if recognized_title == org_meta.name and recognized_year == org_meta.year:
            logger.info("辅助识别与原始识别结果一致，无需重新识别媒体信息")
            return None
        logger.info("辅助识别结果与原始识别结果不一致，重新匹配媒体信息 ...")
        org_meta.name = recognized_title
        org_meta.year = recognized_year
        org_meta.begin_season = season_number
        org_meta.begin_episode = episode_number
        if org_meta.begin_season is not None or org_meta.begin_episode is not None:
            org_meta.type = MediaType.TV
        # 重新识别
        return await self.async_recognize_media(
            meta=org_meta,
            media_source=media_source,
            share_meta=share_meta,
            episode_group=episode_group,
        )

    async def _async_recognize_music_help(
        self,
        title: str,
        org_meta: MetaMusic,
        share_meta: Optional[MetaBase] = None,
        media_source: Optional[MediaSource] = None,
        music_type: Optional[str] = None,
    ) -> Optional[MediaInfo]:
        """
        请求插件辅助识别音乐标题要素，并按修正后的要素重新匹配媒体信息（异步版本）

        :param title: 原始音乐标题
        :param org_meta: 原始音乐元数据
        :param share_meta: 共享识别查询/上报使用的原始元数据
        :param media_source: 请求级识别数据源
        :param music_type: 音乐实体类型
        """
        # 发送音乐名称识别事件，等待插件返回标题要素
        result = await self.eventmanager.async_send_event(
            ChainEventType.MusicNameRecognize,
            {
                "title": title,
                "artist": org_meta.artist,
                "album": org_meta.album,
                "year": org_meta.year,
                "music_type": music_type,
            },
        )
        if not result:
            return None
        event_data = result.event_data or {}
        logger.info(f"获取到音乐辅助识别结果：{event_data}")
        name, artist, album, year = self._parse_music_recognize_event(event_data)
        if not name:
            return None
        # 辅助识别要素与原始一致时无需重新匹配
        if (
            name == org_meta.title
            and (not artist or artist in org_meta.artists)
            and (not album or album == org_meta.album)
            and (not year or year == _normalize_music_year(org_meta.year))
        ):
            logger.info("音乐辅助识别与原始识别结果一致，无需重新匹配媒体信息")
            return None
        logger.info("音乐辅助识别结果与原始识别结果不一致，重新匹配媒体信息 ...")
        new_meta = self._build_music_help_meta(
            org_meta=org_meta,
            name=name,
            artist=artist,
            album=album,
            year=year,
        )
        # 重新识别，仅采信取得远端身份的结果，否则由选择流程保留原生兜底
        mediainfo = await self.async_recognize_media(
            meta=new_meta,
            media_source=media_source,
            share_meta=share_meta,
            music_type=music_type,
        )
        return mediainfo if mediainfo and mediainfo.media_source else None
