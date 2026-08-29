from dataclasses import dataclass
from threading import Lock
from typing import Optional, Tuple, Union

from app.domain.context import MediaInfo
from app.domain.media import is_media_source_enabled
from app.domain.meta.metabase import MetaBase
from app.modules import _ModuleBase
from app.modules.thetvdb import client
from app.runtime.execution import run_in_threadpool
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.schemas.types import (
    MediaRecognizeType,
    MediaSource,
    MediaSourceSelection,
    MediaType,
    ModuleType,
)


@dataclass(frozen=True, slots=True)
class _TvdbAuxiliaryLookup:
    """描述附加信息查询需要执行的单次 TVDB I/O。"""

    method_name: str
    argument: Union[int, str]


class TheTvDbModule(_ModuleBase):
    """
    TVDB媒体信息匹配
    """
    __timeout: int = 15
    tvdb: Optional[client.TVDB] = None
    __auth_lock = Lock()

    def init_module(self) -> None:
        pass

    def _initialize_tvdb_session(self, is_retry: bool = False) -> None:
        """
        创建或刷新 TVDB 登录会话。
        :param is_retry: 是否是由于token失效后的重试登录
        """
        action = "刷新" if is_retry else "创建"
        logger.info(f"开始{action}TVDB登录会话...")
        try:
            if not get_runtime_setting('TVDB_V4_API_KEY'):
                raise ConnectionError("TVDB API Key 未配置，无法初始化会话。")
            self.tvdb = client.TVDB(apikey=get_runtime_setting('TVDB_V4_API_KEY'),
                                              pin=get_runtime_setting('TVDB_V4_API_PIN'),
                                              proxy=get_runtime_setting('PROXY'),
                                              timeout=self.__timeout)
            if self.tvdb:
                logger.info(f"TVDB登录会话{action}成功。")
            else:
                raise ValueError(f"TVDB登录会话{action}后实例仍为None。")
        except Exception as e:
            self.tvdb = None
            raise ConnectionError(f"TVDB登录会话{action}失败: {str(e)}") from e

    def _ensure_tvdb_session(self, is_retry: bool = False) -> None:
        """
        确保TVDB会话存在。如果不存在或需要强制重新初始化，则进行初始化。
        :param is_retry: 是否重新初始化（例如token失效时）
        """
        # 第一次检查 (无锁)，提高性能，避免不必要锁竞争
        if not self.tvdb or is_retry:
            with self.__auth_lock:
                # 第二次检查 (有锁)，防止多个线程都通过第一次检查后重复初始化
                if not self.tvdb or is_retry:
                    self._initialize_tvdb_session(is_retry=is_retry)

    def _handle_tvdb_call(self, method_name: str, *args, **kwargs):
        """
        包裹 TVDB 调用，处理 token 失效情况并尝试重新初始化
        :param method_name: 要在 self.tvdb 实例上调用的方法的名称 (字符串)
        """
        try:
            self._ensure_tvdb_session()
            actual_method = getattr(self.tvdb, method_name)
            return actual_method(*args, **kwargs)
        except ValueError as e:
            if "Unauthorized" in str(e):
                logger.warning("TVDB Token 可能已失效，正在尝试重新登录...")
                try:
                    self._ensure_tvdb_session(is_retry=True)
                    actual_method = getattr(self.tvdb, method_name)
                    return actual_method(*args, **kwargs)
                except ConnectionError as conn_err:
                    logger.error(f"TVDB Token失效后重新登录失败: {conn_err}")
                    raise
            elif "NotFoundException" in str(e) or "ID not found" in str(e):
                logger.warning(f"TVDB 资源未找到 (调用 {method_name}): {e}")
                return None
            else:
                logger.error(f"TVDB 调用 ({method_name}) 时发生未处理的 ValueError: {str(e)}")
                raise
        except ConnectionError as e:
            logger.error(f"TVDB 连接会话错误: {str(e)}")
            raise
        except AttributeError as e:
            logger.error(f"TVDB 实例上没有方法 '{method_name}': {e}")
            raise
        except Exception as e:
            logger.error(f"TVDB 调用时发生未知错误: {str(e)}", exc_info=True)
            raise

    @staticmethod
    def get_name() -> str:
        return "TheTvDb"

    @staticmethod
    def get_type() -> ModuleType:
        """
        获取模块类型
        """
        return ModuleType.MediaRecognize

    @staticmethod
    def get_subtype() -> MediaRecognizeType:
        """
        获取模块子类型
        """
        return MediaRecognizeType.TVDB

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效
        """
        return 4

    def stop(self):
        with self.__auth_lock:
            self.tvdb = None

    def test(self) -> Tuple[bool, str]:
        """
        测试模块连接性
        """
        try:
            self._handle_tvdb_call("get_series", 81189)
            return True, ""
        except Exception as e:
            return False, str(e)

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        pass

    def tvdb_info(self, tvdbid: int) -> Optional[dict]:
        """
        获取TVDB信息
        :param tvdbid: int
        :return: TVDB信息
        """
        try:
            logger.info(f"开始获取TVDB信息: {tvdbid} ...")
            return self._handle_tvdb_call("get_series_extended", tvdbid)
        except Exception as err:
            logger.error(f"获取TVDB信息失败: {str(err)}")
            return None

    def tvdb_slug(self, tvdbid: int) -> Optional[str]:
        """
        获取TVDB剧集的 slug（别名），用于构建 TheTvDb 直达链接。
        :param tvdbid: int
        :return: slug 字符串，如 "speed-and-love"
        """
        try:
            result = self._handle_tvdb_call("get_series", tvdbid)
            if result and isinstance(result, dict):
                return result.get("slug")
            return None
        except Exception as err:
            logger.error(f"获取TVDB slug 失败: {str(err)}")
            return None

    def search_tvdb(self, title: str) -> list:
        """
        用标题搜索TVDB剧集
        :param title: 标题
        :return: TVDB信息
        """
        try:
            logger.info(f"开始用标题搜索TVDB剧集: {title} ...")
            res = self._handle_tvdb_call("search", title)
            if res is None:
                return []
            if not isinstance(res, list):
                logger.warning(f"TVDB 搜索 '{title}' 未返回列表：{type(res)}")
                return []
            return [item for item in res if isinstance(item, dict) and item.get("type") == "series"]
        except Exception as err:
            logger.error(f"用标题搜索TVDB剧集失败 ({title}): {str(err)}")
            return []

    @staticmethod
    def _tvdb_aliases(info: dict[str, object]) -> list[str]:
        """从 TVDB 搜索或详情响应中提取名称与翻译别名。"""
        values: list[object] = [info.get("name")]
        for field in ("aliases", "translations", "nameTranslations"):
            raw_value = info.get(field) or []
            if isinstance(raw_value, dict):
                raw_value = list(raw_value.values())
            if not isinstance(raw_value, (list, tuple, set)):
                raw_value = [raw_value]
            for item in raw_value:
                values.append(
                    item.get("name") or item.get("value")
                    if isinstance(item, dict)
                    else item
                )
        aliases: list[str] = []
        seen: set[str] = set()
        for value in values:
            alias = str(value or "").strip()
            normalized = " ".join(alias.casefold().split())
            if alias and normalized not in seen:
                aliases.append(alias)
                seen.add(normalized)
        return aliases

    @staticmethod
    def _tvdb_media_id(info: dict[str, object]) -> Optional[str]:
        """从不同形态的 TVDB 响应中提取纯数字来源 ID。"""
        raw_id = info.get("tvdb_id") or info.get("id")
        if raw_id is None:
            return None
        value = str(raw_id).rsplit("-", 1)[-1]
        return value if value.isdigit() else None

    def get_media_auxiliary_info(
            self,
            mediainfo: MediaInfo,
            media_source: Optional[MediaSourceSelection] = None,
            metainfo: Optional[MetaBase] = None,
    ) -> list[MediaInfo]:
        """从 TVDB 补充电视剧别名，不向主媒体写入 TVDB 专用字段。"""
        lookup = self._build_auxiliary_lookup(
            mediainfo=mediainfo,
            media_source=media_source,
            metainfo=metainfo,
        )
        if not lookup:
            return []
        candidates = self._load_auxiliary_candidates(lookup)
        return self._resolve_auxiliary_candidates(mediainfo, candidates)

    @staticmethod
    def _build_auxiliary_lookup(
            mediainfo: MediaInfo,
            media_source: Optional[MediaSourceSelection],
            metainfo: Optional[MetaBase],
    ) -> Optional[_TvdbAuxiliaryLookup]:
        """校验 TVDB 附加信息请求并选择原生 ID 或标题查询。"""
        if (
                not mediainfo
                or mediainfo.type != MediaType.TV
                or not is_media_source_enabled(media_source, MediaSource.TVDB)
        ):
            return None
        del metainfo
        if (
                mediainfo.media_source == MediaSource.TVDB
                and str(mediainfo.media_id or "").isdigit()
        ):
            return _TvdbAuxiliaryLookup(
                method_name="tvdb_info",
                argument=int(mediainfo.media_id),
            )
        return _TvdbAuxiliaryLookup(
            method_name="search_tvdb",
            argument=mediainfo.title,
        )

    def _load_auxiliary_candidates(
            self, lookup: _TvdbAuxiliaryLookup
    ) -> list[dict[str, object]]:
        """通过同步 TVDB I/O 获取候选详情。"""
        result = getattr(self, lookup.method_name)(lookup.argument)
        if lookup.method_name == "tvdb_info":
            return [result] if result else []
        return result or []

    async def _async_load_auxiliary_candidates(
            self, lookup: _TvdbAuxiliaryLookup
    ) -> list[dict[str, object]]:
        """仅在线程池中执行 TVDB 客户端的阻塞网络调用。"""
        result = await run_in_threadpool(
            getattr(self, lookup.method_name),
            lookup.argument,
        )
        if lookup.method_name == "tvdb_info":
            return [result] if result else []
        return result or []

    def _resolve_auxiliary_candidates(
            self, mediainfo: MediaInfo, candidates: list[dict[str, object]]
    ) -> list[MediaInfo]:
        """按年份、名称和来源 ID 统一解析 TVDB 候选。"""
        target_names = {
            " ".join(str(name).casefold().split())
            for name in [mediainfo.title, *(mediainfo.names or [])]
            if name
        }
        for info in candidates:
            candidate_year = str(
                info.get("year")
                or info.get("firstAired")
                or info.get("first_air_time")
                or ""
            )[:4]
            if mediainfo.year and candidate_year and str(mediainfo.year) != candidate_year:
                continue
            aliases = self._tvdb_aliases(info)
            normalized_aliases = {" ".join(alias.casefold().split()) for alias in aliases}
            if not target_names.intersection(normalized_aliases):
                continue
            media_id = self._tvdb_media_id(info)
            if not media_id or not aliases:
                continue
            return [MediaInfo(
                media_source=MediaSource.TVDB,
                media_id=media_id,
                type=MediaType.TV,
                title=aliases[0],
                year=str(info.get("year")) if info.get("year") else mediainfo.year,
                names=aliases,
            )]
        return []

    async def async_get_media_auxiliary_info(
            self,
            mediainfo: MediaInfo,
            media_source: Optional[MediaSourceSelection] = None,
            metainfo: Optional[MetaBase] = None,
    ) -> list[MediaInfo]:
        """异步获取 TVDB 候选，并复用同步入口的纯解析决策。"""
        lookup = self._build_auxiliary_lookup(
            mediainfo=mediainfo,
            media_source=media_source,
            metainfo=metainfo,
        )
        if not lookup:
            return []
        candidates = await self._async_load_auxiliary_candidates(lookup)
        return self._resolve_auxiliary_candidates(mediainfo, candidates)

    def clear_cache(self):
        """
        清除缓存
        """
        logger.info(f"开始清除{self.get_name()}缓存 ...")
        if tvdb := self.tvdb:
            tvdb.clear_cache()
        logger.info(f"{self.get_name()}缓存清除完成")
