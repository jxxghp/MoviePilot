import asyncio
import re
import threading
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Iterable, Optional, Tuple, Union

from app.adapters.network.http import AsyncRequestUtils, RequestUtils
from app.domain.classification.evaluator import read_fact
from app.domain.classification.facts import build_classification_facts
from app.domain.context import (
    MusicAlbumInfo,
    MusicArtistInfo,
    MusicInfo,
    MusicRelease,
)
from app.domain.media import is_media_source_selected
from app.domain.meta.metabase import MetaBase
from app.domain.meta.metamusic import MetaMusic
from app.foundation.text import convert as zhconv_convert
from app.modules import _ModuleBase
from app.modules.musicbrainz.cache import MusicBrainzCache
from app.runtime.cache import cached
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.schemas.category import (
    ClassificationEnrichmentMatch,
    ClassificationEnrichmentRequest,
    ClassificationEnrichmentResponse,
)
from app.schemas.types import (
    MUSIC_ENTITY_ALBUM,
    MUSIC_ENTITY_RECORDING,
    MediaRecognizeType,
    MediaSource,
    MediaSourceSelection,
    MediaType,
    ModuleType,
)


@dataclass(frozen=True, slots=True)
class _MusicBrainzRecognitionPlan:
    """描述 MusicBrainz 识别的准入身份、实体范围和缓存策略。"""

    meta: Optional[MetaMusic]
    media_source: MediaSource
    media_id: Optional[str]
    music_type: Optional[str]
    cache_enabled: bool = True

    @property
    def search_recording(self) -> bool:
        """是否允许尝试单曲详情或候选。"""
        return bool(self.music_type != MUSIC_ENTITY_ALBUM)

    @property
    def search_album(self) -> bool:
        """是否允许在单曲未命中后继续尝试专辑。"""
        return bool(self.music_type != MUSIC_ENTITY_RECORDING)

    def require_meta(self) -> MetaMusic:
        """返回候选识别计划必有的音乐元数据。"""
        if self.meta is None:
            raise RuntimeError("MusicBrainz 候选识别计划缺少音乐元数据")
        return self.meta

    def require_media_id(self) -> str:
        """返回详情识别计划必有的原生 ID。"""
        if self.media_id is None:
            raise RuntimeError("MusicBrainz 详情识别计划缺少原生 ID")
        return self.media_id

    def detail_kwargs(self) -> dict[str, str]:
        """生成详情入口兼容旧签名所需的可选实体参数。"""
        return (
            {"music_type": self.music_type}
            if self.music_type is not None
            else {}
        )


@dataclass(frozen=True, slots=True)
class _MusicBrainzResponseDecision:
    """描述一次 MusicBrainz 响应的投影结果与退避决策。"""

    payload: Optional[dict[str, Any]] = None
    retry_delay: Optional[float] = None


@dataclass(frozen=True, slots=True)
class _MusicBrainzRequestPlan:
    """冻结 MusicBrainz 请求路径与参数，供同步异步 I/O 外壳共用。"""

    path: str
    params: dict[str, Any]


class MusicBrainzModule(_ModuleBase):
    """通过 MusicBrainz 提供音乐元数据搜索和详情识别。"""

    _source = MediaSource.MusicBrainz
    _base_url = "https://musicbrainz.org/ws/2"
    _detail_url = "https://musicbrainz.org/recording"
    _album_detail_url = "https://musicbrainz.org/release-group"
    _artist_detail_url = "https://musicbrainz.org/artist"
    _cover_url = "https://coverartarchive.org/release-group"
    _request_interval = 1.0
    _request_lock = threading.Lock()
    _last_request_at = 0.0
    # 本地识别缓存，由模块管理器初始化时挂载
    cache: MusicBrainzCache = None
    # 全局复用 HTTP 客户端：keep-alive 省去每次请求的 DNS+TLS 握手（约 6s → 0.4s）
    _request: Optional[RequestUtils] = None
    _client_lock = threading.Lock()
    # 服务端繁忙（429/5xx）时的重试次数与退避基数，重试间隔随次数翻倍递增
    _busy_retries = 2
    _busy_backoff = 5.0
    # 关联艺术家按关系可读性排序，纪念性质的致敬关系数量庞大且价值低，放到最后
    _artist_relation_priority = (
        "member of band",
        "subgroup",
        "collaboration",
        "founder",
        "artist rename",
        "supporting musician",
        "conductor position",
        "involved with",
        "teacher",
        "sibling",
        "parent",
        "married",
    )
    # 艺术家外链只保留对用户有意义的官方与流媒体入口
    _artist_link_types = (
        "official homepage",
        "wikidata",
        "wikipedia",
        "discogs",
        "allmusic",
        "social network",
        "free streaming",
        "streaming",
        "youtube",
        "purchase for download",
    )

    def init_module(self) -> None:
        """初始化 MusicBrainz 模块并挂载本地识别缓存。"""
        self.cache = MusicBrainzCache()

    def init_setting(self) -> Optional[Tuple[str, Union[str, bool]]]:
        """MusicBrainz 无需独立密钥或启用开关。"""
        return None

    def stop(self) -> None:
        """停止模块，退出前持久化识别缓存。"""
        if self.cache:
            try:
                self.cache.save()
            except Exception as err:
                logger.error(f"保存音乐识别缓存失败：{str(err)}")
        if self._request is not None:
            self._request.close()
            self.__class__._request = None

    def scheduler_job(self) -> None:
        """定时任务，每10分钟持久化一次音乐识别缓存。"""
        if self.cache:
            self.cache.save()

    def clear_cache(self) -> None:
        """响应全局缓存清理事件，清空音乐识别缓存。"""
        logger.info("开始清除音乐识别缓存 ...")
        if self.cache:
            self.cache.clear()
        logger.info("音乐识别缓存清除完成")

    def music_cache_items(self) -> list[dict]:
        """查询音乐识别缓存条目列表。"""
        return self.cache.list_items() if self.cache else []

    def music_cache_delete(self, cache_key: str) -> dict:
        """按缓存键删除单条音乐识别缓存。"""
        return self.cache.delete(cache_key) if self.cache else {}

    def music_cache_clear(self) -> None:
        """清空全部音乐识别缓存。"""
        if self.cache:
            self.cache.clear()

    def test(self) -> Tuple[bool, str]:
        """测试 MusicBrainz 搜索接口连通性。"""
        result = self._request_json(
            "/recording",
            params={"query": "recording:test", "limit": 1, "fmt": "json"},
        )
        return (True, "") if result is not None else (False, "MusicBrainz 网络连接失败")

    @staticmethod
    def get_name() -> str:
        """返回模块展示名称。"""
        return "MusicBrainz"

    @staticmethod
    def get_classification_enrichment_sources() -> tuple[MediaSource, ...]:
        """声明本模块只能以 MusicBrainz 来源补充音乐标准事实。"""
        return (MediaSource.MusicBrainz,)

    def get_media_classification_facts(
        self,
        request: ClassificationEnrichmentRequest,
    ) -> ClassificationEnrichmentResponse | None:
        """通过精确 ISRC 匹配单曲，并仅返回请求中的缺失标准事实。"""
        if request.media_type != MediaType.MUSIC.value:
            return None
        isrc = request.external_ids.get("isrc")
        if not isrc:
            return None
        payload = self._request_json(
            "/recording",
            params={
                "query": f'isrc:"{self._escape_query(isrc)}"',
                "limit": 5,
                "fmt": "json",
            },
        )
        candidate = next(
            (
                item
                for item in self._project_recording_search(payload)
                if item.media_id and self._same_text(item.isrc, isrc)
            ),
            None,
        )
        if candidate is None:
            return None
        candidate_id = candidate.media_id
        if not candidate_id:
            return None
        detail = self.recognize_music(
            MediaSource.MusicBrainz,
            candidate_id,
            music_type=MUSIC_ENTITY_RECORDING,
        )
        if detail is None:
            return None
        facts = build_classification_facts(detail)
        supplied = {}
        for field_id in request.missing_fields:
            value, missing = read_fact(facts, field_id)
            if not missing:
                supplied[field_id] = value
        return ClassificationEnrichmentResponse(
            media_source=MediaSource.MusicBrainz.value,
            match=ClassificationEnrichmentMatch(
                kind="external_id",
                media_source="isrc",
                media_id=isrc,
            ),
            facts=supplied,
        )

    @staticmethod
    def get_music_source() -> MediaSource:
        """返回音乐识别使用的数据源标识。"""
        return MusicBrainzModule._source

    @staticmethod
    def get_type() -> ModuleType:
        """返回模块所属的媒体识别类型。"""
        return ModuleType.MediaRecognize

    @staticmethod
    def get_subtype() -> MediaRecognizeType:
        """返回 MusicBrainz 模块子类型。"""
        return MediaRecognizeType.MusicBrainz

    @staticmethod
    def get_priority() -> int:
        """音乐识别在所有 MediaRecognize 模块中最先响应，避免音乐请求被影视模块误识别。"""
        return 0

    def search_music(
            self,
            meta: MetaMusic,
            limit: int = 20,
            media_source: Optional[MediaSourceSelection] = None,
    ) -> Optional[list[MusicInfo]]:
        """搜索单曲、专辑和艺术家，并交错返回可浏览的 MusicBrainz 候选。"""
        if not is_media_source_selected(media_source, self._source):
            return None
        normalized_limit = max(1, min(limit, 100))
        recordings = self._search_recordings(meta, limit=normalized_limit)
        albums = self._search_albums(meta, limit=normalized_limit)
        artists = self._search_artists(meta, limit=normalized_limit)
        return self._interleave_results(
            recordings,
            albums,
            artists,
            limit=normalized_limit,
        )

    def _search_recordings(self, meta: MetaMusic, limit: int) -> list[MusicInfo]:
        """按音频标签条件搜索 Recording，供全局搜索和文件识别复用。"""
        for query in self._recording_queries(meta):
            payload = self._request_json(
                "/recording",
                params={"query": query, "limit": max(1, min(limit, 100)), "fmt": "json"},
            )
            results = self._project_recording_search(payload)
            if results:
                return results
        return []

    async def _async_search_recordings(
            self,
            meta: MetaMusic,
            limit: int,
    ) -> list[MusicInfo]:
        """异步按音频标签条件搜索 Recording 候选。"""
        for query in self._recording_queries(meta):
            payload = await self._async_request_json(
                "/recording",
                params={
                    "query": query,
                    "limit": max(1, min(limit, 100)),
                    "fmt": "json",
                },
            )
            results = self._project_recording_search(payload)
            if results:
                return results
        return []

    @classmethod
    def _project_recording_search(
            cls, payload: Optional[dict[str, Any]]
    ) -> list[MusicInfo]:
        """把 Recording 搜索响应投影为统一音乐候选。"""
        return [
            info
            for item in (payload or {}).get("recordings") or []
            if (info := cls._recording_to_info(item))
        ]

    @classmethod
    def _recording_queries(cls, meta: MetaMusic) -> list[str]:
        """构造 Recording 检索式阶梯，由严到宽逐级放宽避免零命中。

        条目括号多为全角、艺术家署名存在变体（如外文艺名），精确 AND 条件容易零命中；
        放宽后由候选评分负责收紧（已知艺术家时必须艺术家命中），不会产生错误身份。
        """
        title = cls._search_title(meta.title)
        if not title:
            return []
        artist = meta.artists[0] if meta.artists else None
        # 括号内的影视 tie-in、版本说明多为半角，与条目全角写法不一致，准备去注释曲名兜底
        bare_title = cls._strip_parenthetical(title)
        # 曲名开头的艺术家署名前缀是命名习惯不是曲名内容，用主体名检索
        title = cls._strip_artist_prefix(title, meta.artists)
        bare_title = cls._strip_artist_prefix(bare_title, meta.artists)
        queries: list[str] = []
        for query in [
            cls._build_query(meta),
            f"recording:{cls._query_phrase(title)}" if title else None,
            f'recording:{cls._query_phrase(bare_title)} AND artist:"{cls._escape_query(artist)}"'
            if artist and bare_title and bare_title != title else None,
            # 艺术家署名变体（外文艺名等）导致 AND 条件零命中时，仅按主体曲名检索，
            # 候选挑选阶段要求艺术家命中兜住同名异曲
            f"recording:{cls._query_phrase(bare_title)}" if bare_title else None,
        ]:
            if query and query not in queries:
                queries.append(query)
        return queries

    @classmethod
    def _strip_parenthetical(cls, value: str) -> str:
        """去除标题中的括号注释，保留主体曲名。"""
        text = re.sub(r"[\(（][^\)）]*[\)）]", " ", str(value or ""))
        return cls._normalize_text(text)

    @classmethod
    def _main_title(cls, value: Optional[str]) -> str:
        """提取冒号副标题结构的主标题，兼容全角/半角冒号。"""
        text = re.split(r"\s*[：:]\s*", str(value or ""), maxsplit=1)[0]
        return cls._normalize_text(text)

    @classmethod
    def _head_title(cls, value: Optional[str]) -> str:
        """提取候选「曲名-歌手」「曲名《巡演名》…」命名的首段曲名。"""
        text = re.split(r"\s*[-–—−－：:]\s*|\s*《", str(value or ""), maxsplit=1)[0]
        return cls._normalize_text(text)

    @classmethod
    def _lead_token(cls, value: Optional[str]) -> str:
        """提取候选标题首个空白分隔段（「愛情電影主題曲 雲且留住」的主体名）。"""
        text = str(value or "").strip()
        return cls._normalize_text(text.split(" ", 1)[0]) if text else ""

    # 系列专辑的卷号后缀（好歌茹芸, Vol. 3）是发行分卷标记，条目本体不含卷号
    _VOLUME_SUFFIX_RE = re.compile(r",?\s*vol\.?\s*\d+$", re.IGNORECASE)
    # 卷号提取：标题任意位置的 Vol. N 写法，系列专辑弱匹配时用于分卷一致性校验
    _VOLUME_NUMBER_RE = re.compile(r"vol\.?\s*(\d+)", re.IGNORECASE)

    @classmethod
    def _strip_volume_suffix(cls, value: Optional[str]) -> str:
        """剔除标题尾部的卷号后缀，返回专辑本体名。"""
        return cls._normalize_text(cls._VOLUME_SUFFIX_RE.sub("", str(value or "")))

    @classmethod
    def _volume_number(cls, value: Optional[str]) -> Optional[str]:
        """提取标题中的卷号（Vol. 3 -> 3），无卷号返回 None。"""
        match = cls._VOLUME_NUMBER_RE.search(str(value or ""))
        return match.group(1) if match else None

    # 原声带资源标题的通用描述词尾部（条目本体是电影名）；
    # 尾部描述词保留时作为原声带形态标记参与弱匹配判定
    _SOUNDTRACK_SUFFIX_RE = re.compile(
        r"\s*(?:original\s+motion\s+picture|motion\s+picture)?\s*"
        r"(?<![A-Za-z0-9])(?:original\s+)?(?:soundtrack|score|ost)$",
        re.IGNORECASE,
    )

    @classmethod
    def _soundtrack_body(cls, value: Optional[str]) -> str:
        """剔除原声带标题尾部的通用描述词，返回电影名本体；无描述词返回空串。"""
        text = str(value or "").strip()
        body = cls._SOUNDTRACK_SUFFIX_RE.sub("", text)
        if not body or body == text:
            return ""
        return cls._normalize_text(body)

    # 演唱会资源的标题常带演出后缀（S.H.E十七音乐会），条目仅保留演出名本体
    _PERFORMANCE_SUFFIX_RE = re.compile(
        r"\s*(?:音乐会|音樂會|演唱会|演唱會|巡回|巡演|Live|Tour)$", re.IGNORECASE)

    @classmethod
    def _performance_title(cls, value: Optional[str]) -> str:
        """剔除标题尾部的演出形态后缀，返回演出名本体。"""
        return cls._normalize_text(cls._PERFORMANCE_SUFFIX_RE.sub("", str(value or "")))

    @classmethod
    def _strip_artist_prefix(cls, title: Optional[str], artists: Optional[list[str]]) -> str:
        """剥离曲名开头的艺术家署名前缀（「许茹芸的爱情电影主题曲」）。

        资源命名习惯把署名放在曲名前，条目不含该前缀；署名身份由
        候选挑选阶段的艺术家要求保证，不会产生错误归属。前缀剥离后
        无剩余文本时保留原标题（「合集 - 花开」类短标题保护）。
        """
        text = str(title or "").strip()
        for artist in artists or []:
            artist = str(artist or "").strip()
            if len(artist) < 2:
                continue
            if text.startswith(artist):
                remainder = re.sub(r"^[的之]\s*", "", text[len(artist):]).strip()
                if remainder:
                    return remainder
        return text

    def _search_albums(self, meta: MetaMusic, limit: int) -> list[MusicInfo]:
        """按标题和可选艺术家搜索 Release Group 专辑候选，检索式同样逐级放宽。"""
        for query in self._album_queries(meta):
            payload = self._request_json(
                "/release-group",
                params={
                    "query": query,
                    "limit": max(1, min(limit, 100)),
                    "fmt": "json",
                },
            )
            results = self._project_album_search(payload)
            if results:
                return results
        return []

    async def _async_search_albums(
            self,
            meta: MetaMusic,
            limit: int,
    ) -> list[MusicInfo]:
        """异步按标题和可选艺术家搜索 Release Group 专辑候选。"""
        for query in self._album_queries(meta):
            payload = await self._async_request_json(
                "/release-group",
                params={
                    "query": query,
                    "limit": max(1, min(limit, 100)),
                    "fmt": "json",
                },
            )
            results = self._project_album_search(payload)
            if results:
                return results
        return []

    @classmethod
    def _project_album_search(
            cls, payload: Optional[dict[str, Any]]
    ) -> list[MusicInfo]:
        """把 Release Group 搜索响应投影为统一音乐候选。"""
        return [
            album.to_music_info()
            for item in (payload or {}).get("release-groups") or []
            if (album := cls._release_group_to_album(item))
        ]

    @classmethod
    def _album_queries(cls, meta: MetaMusic) -> list[str]:
        """构造专辑检索式阶梯：专辑名+艺术家 → 仅专辑名 → 去括号/卷号变体。"""
        title = cls._search_title(meta.album or meta.title)
        if not title:
            return []
        artist = meta.artists[0] if meta.artists else None
        # 括号注释与 Vol. 卷号在条目中常以 disambiguation 形式存在，变体名兜底检索
        bare_title = cls._strip_parenthetical(title)
        bare_title = cls._strip_volume_suffix(bare_title)
        # 专辑名开头的艺术家署名前缀同样是命名习惯，用主体名检索
        title = cls._strip_artist_prefix(title, meta.artists)
        bare_title = cls._strip_artist_prefix(bare_title, meta.artists)
        # 原声带标题的通用描述词（Original Motion Picture Soundtrack）在条目中常省略，
        # 用电影名本体补充一级检索（The Hateful Eight / Pulp Fiction）；
        # 本体过短（The Score 类）时不作为独立检索目标避免噪声
        soundtrack_body = cls._soundtrack_body(bare_title)
        if len(cls._match_text(soundtrack_body)) < 4:
            soundtrack_body = ""
        queries: list[str] = []
        for query in [
            f'releasegroup:{cls._query_phrase(title)} AND artist:"{cls._escape_query(artist)}"'
            if artist else None,
            f"releasegroup:{cls._query_phrase(title)}" if title else None,
            f'releasegroup:{cls._query_phrase(bare_title)} AND artist:"{cls._escape_query(artist)}"'
            if artist and bare_title and bare_title != title else None,
            f'releasegroup:{cls._query_phrase(soundtrack_body)} AND artist:"{cls._escape_query(artist)}"'
            if artist and soundtrack_body else None,
            f"releasegroup:{cls._query_phrase(soundtrack_body)}" if soundtrack_body else None,
            # 署名变体兜底：仅按去注释专辑名检索，挑选阶段要求艺术家同时命中
            f"releasegroup:{cls._query_phrase(bare_title)}" if bare_title else None,
        ]:
            if query and query not in queries:
                queries.append(query)
        return queries

    def _search_artists(self, meta: MetaMusic, limit: int) -> list[MusicInfo]:
        """按用户输入中的艺术家部分搜索 Artist 浏览候选。"""
        artist_name = meta.artists[0] if meta.artists else meta.title
        phrase = self._query_phrase(artist_name)
        if not phrase:
            return []
        payload = self._request_json(
            "/artist",
            params={"query": f"artist:{phrase}", "limit": max(1, min(limit, 100)), "fmt": "json"},
        )
        return [
            artist.to_music_info()
            for item in (payload or {}).get("artists") or []
            if (artist := self._artist_to_info(item, include_raw=True))
        ]

    @staticmethod
    def _interleave_results(*groups: list[MusicInfo], limit: int) -> list[MusicInfo]:
        """按实体轮询合并结果，避免单曲数量占满全局搜索页。"""
        results: list[MusicInfo] = []
        index = 0
        while len(results) < limit and any(index < len(group) for group in groups):
            for group in groups:
                if index < len(group):
                    results.append(group[index])
                    if len(results) >= limit:
                        break
            index += 1
        return results

    def match_music_album(
            self,
            meta: MetaMusic,
            tracks: list[MetaMusic],
            limit: int = 5,
    ) -> Optional[MusicAlbumInfo]:
        """按目录线索和曲目特征把本地音频集合对位到 MusicBrainz 发行版本。

        适用于无标签整专目录：用专辑名、歌手搜索候选发行版本，再用曲目数、
        总时长和逐曲时长相似度打分，选出最可信的版本并返回其曲目表。
        """
        if not tracks:
            return None
        details: list[dict[str, Any]] = []
        releases = self._search_release_candidates(meta, tracks, limit=limit)
        for request in self._release_detail_requests(releases):
            detail = self._request_json(request.path, params=request.params)
            if not detail:
                continue
            details.append(detail)
        return self._select_release_match(meta, tracks, details)

    async def async_match_music_album(
            self,
            meta: MetaMusic,
            tracks: list[MetaMusic],
            limit: int = 5,
    ) -> Optional[MusicAlbumInfo]:
        """异步按目录线索和曲目特征匹配 MusicBrainz 发行版本。"""
        if not tracks:
            return None
        details: list[dict[str, Any]] = []
        releases = await self._async_search_release_candidates(
            meta,
            tracks,
            limit=limit,
        )
        for request in self._release_detail_requests(releases):
            detail = await self._async_request_json(
                request.path, params=request.params
            )
            if not detail:
                continue
            details.append(detail)
        return self._select_release_match(meta, tracks, details)

    @classmethod
    def _select_release_match(
            cls,
            meta: MetaMusic,
            tracks: list[MetaMusic],
            details: Iterable[dict[str, Any]],
    ) -> Optional[MusicAlbumInfo]:
        """对已获取的发行详情统一打分并投影最佳专辑。"""
        best_album: Optional[MusicAlbumInfo] = None
        best_score = 0.0
        for detail in details:
            summary = cls._release_track_summary(detail)
            score = cls._score_release(meta, tracks, detail, summary)
            if score > best_score:
                best_score = score
                best_album = cls._release_to_album(detail)
        # 得分低于阈值时宁可不匹配，避免把曲目写到错误的专辑上
        return best_album if best_score >= cls._album_match_threshold else None

    _album_match_threshold = 60.0

    def _search_release_candidates(
            self,
            meta: MetaMusic,
            tracks: list[MetaMusic],
            limit: int,
    ) -> list[dict[str, Any]]:
        """按专辑名和曲名线索搜索候选发行版本，多个查询按命中顺序去重。"""
        releases: list[dict[str, Any]] = []
        seen: set[str] = set()
        for request in self._release_search_requests(meta, tracks, limit):
            payload = self._request_json(
                request.path, params=request.params
            )
            self._merge_release_candidates(releases, seen, payload)
            if len(releases) >= limit:
                break
        return releases[:limit]

    async def _async_search_release_candidates(
            self,
            meta: MetaMusic,
            tracks: list[MetaMusic],
            limit: int,
    ) -> list[dict[str, Any]]:
        """异步按专辑名和曲名线索搜索并去重候选发行版本。"""
        releases: list[dict[str, Any]] = []
        seen: set[str] = set()
        for request in self._release_search_requests(meta, tracks, limit):
            payload = await self._async_request_json(
                request.path, params=request.params
            )
            self._merge_release_candidates(releases, seen, payload)
            if len(releases) >= limit:
                break
        return releases[:limit]

    @staticmethod
    def _merge_release_candidates(
            releases: list[dict[str, Any]],
            seen: set[str],
            payload: Optional[dict[str, Any]],
    ) -> None:
        """按首次命中顺序合并并去重 Release 搜索响应。"""
        for item in (payload or {}).get("releases") or []:
            release_id = item.get("id")
            if release_id and release_id not in seen:
                seen.add(release_id)
                releases.append(item)

    @classmethod
    def _release_search_requests(
            cls,
            meta: MetaMusic,
            tracks: list[MetaMusic],
            limit: int,
    ) -> list[_MusicBrainzRequestPlan]:
        """构造发行候选查询计划，统一同步与异步的限额和参数。"""
        normalized_limit = max(1, min(limit, 25))
        return [
            _MusicBrainzRequestPlan(
                path="/release",
                params={
                    "query": query,
                    "limit": normalized_limit,
                    "fmt": "json",
                },
            )
            for query in cls._release_queries(meta, tracks)
        ]

    @staticmethod
    def _release_detail_requests(
            releases: Iterable[dict[str, Any]],
    ) -> list[_MusicBrainzRequestPlan]:
        """按候选顺序构造发行详情请求计划并跳过无 ID 条目。"""
        return [
            _MusicBrainzRequestPlan(
                path=f"/release/{release_id}",
                params={
                    "inc": "recordings+media+artist-credits",
                    "fmt": "json",
                },
            )
            for release in releases
            if (release_id := release.get("id"))
        ]

    @classmethod
    def _release_queries(cls, meta: MetaMusic, tracks: list[MetaMusic]) -> list[str]:
        """构造专辑搜索表达式：优先专辑名+歌手，无专辑线索时用曲名兜底。"""
        queries: list[str] = []
        album_title = meta.album or meta.title
        artist = meta.artists[0] if meta.artists else meta.album_artist
        if album_title:
            if artist:
                queries.append(
                    f'release:"{cls._escape_query(album_title)}" AND artist:"{cls._escape_query(artist)}"'
                )
            queries.append(f'release:"{cls._escape_query(album_title)}"')
        # 目录名无意义时（如 Various Artists 合集），用代表性曲名反查所属发行版本
        titles = cls._unique_texts(
            [track.title for track in tracks if track.title and not track.title.strip().isdigit()]
        )[:3]
        if titles:
            recording_clause = " OR ".join(
                f'recording:"{cls._escape_query(title)}"' for title in titles
            )
            query = f"({recording_clause})"
            if artist:
                query += f' AND artist:"{cls._escape_query(artist)}"'
            queries.append(query)
        return queries

    @classmethod
    def _release_track_summary(cls, detail: dict[str, Any]) -> list[dict[str, Any]]:
        """提取发行版本的曲目概要（碟号、曲序、时长、标题）供打分使用。"""
        summary: list[dict[str, Any]] = []
        for medium in detail.get("media") or []:
            disc = cls._optional_int(medium.get("position")) or 1
            for track in medium.get("tracks") or []:
                recording = track.get("recording") or {}
                summary.append({
                    "disc": disc,
                    "position": cls._optional_int(track.get("position")),
                    "length": cls._duration_seconds(
                        track.get("length") or recording.get("length")
                    ),
                    "title": track.get("title") or recording.get("title"),
                })
        return summary

    @classmethod
    def _score_release(
            cls,
            meta: MetaMusic,
            tracks: list[MetaMusic],
            detail: dict[str, Any],
            summary: list[dict[str, Any]],
    ) -> float:
        """给候选发行版本打分（0-100），综合标题、歌手、曲目数和时长相似度。"""
        local_count = len(tracks)
        release_count = len(summary)
        if not release_count:
            return 0.0
        # 曲目数差异过大直接排除，避免单曲误命中整专或反之
        diff = abs(local_count - release_count)
        if diff > max(4, int(local_count * 0.5)):
            return 0.0
        # 本地文件比发行版本多出的曲目无法被覆盖，超出容忍范围视为错误候选
        if local_count > release_count and diff > max(1, int(release_count * 0.25)):
            return 0.0
        score = 0.0
        # 标题相似度：专辑目录名或文件标签中的专辑名/曲名
        title_hints = cls._unique_texts([meta.album, meta.title])
        title_sim = max(
            (cls._text_similarity(hint, detail.get("title")) for hint in title_hints),
            default=0.0,
        )
        artist_names = cls._artist_credits(detail.get("artist-credit"))[0]
        if meta.artists and artist_names:
            artist_sim = max(
                cls._text_similarity(meta.artists[0], name) for name in artist_names
            )
            score += 40 * title_sim + 15 * artist_sim
        else:
            # 缺少歌手线索时把权重让给标题
            score += 50 * title_sim
        # 曲目数：完全一致是最强信号
        if diff == 0:
            score += 15
        elif diff == 1:
            score += 8
        elif diff <= max(2, int(local_count * 0.15)):
            score += 2
        # 曲名重合度：部分曲目目录（只下载了整专的一部分）依靠曲名对位确认
        release_titles = {cls._match_text(item["title"]) for item in summary}
        named_tracks = [track for track in tracks if track.title and not track.title.strip().isdigit()]
        if named_tracks and release_titles:
            overlap = sum(
                1 for track in named_tracks if cls._match_text(track.title) in release_titles
            )
            score += 15 * overlap / len(named_tracks)
        # 总时长：无损整专 rip 的总时长与 MusicBrainz 记录高度接近
        local_total = sum(track.duration or 0 for track in tracks)
        release_total = sum(item["length"] or 0 for item in summary)
        local_durations = [track.duration for track in tracks if track.duration]
        if local_durations and release_total:
            delta = abs(local_total - release_total) / max(local_total, release_total)
            if delta <= 0.02:
                score += 15
            elif delta <= 0.05:
                score += 10
            elif delta <= 0.10:
                score += 5
        # 逐曲时长对位：曲目数一致时逐首比较
        if diff == 0 and len(local_durations) == local_count:
            similarities = []
            for track, item in zip(
                sorted(tracks, key=lambda item: (item.disc_number or 1, item.track_number or 0)),
                summary,
            ):
                if track.duration and item["length"]:
                    similarities.append(cls._duration_similarity(track.duration, item["length"]))
            if similarities:
                score += 15 * sum(similarities) / len(similarities)
        return score

    @staticmethod
    def _duration_similarity(left: int, right: int) -> float:
        """比较两个时长的接近程度，完全一致为 1，差异越大越接近 0。"""
        if not left or not right:
            return 0.0
        return max(0.0, 1 - abs(left - right) / max(left, right))

    @classmethod
    def _text_similarity(cls, left: Optional[str], right: Optional[str]) -> float:
        """忽略大小写和标点后比较两段音乐文本的相似度。"""
        normalized_left = cls._match_text(left)
        normalized_right = cls._match_text(right)
        if not normalized_left or not normalized_right:
            return 0.0
        return SequenceMatcher(None, normalized_left, normalized_right).ratio()

    @staticmethod
    def _match_text(value: Optional[str]) -> str:
        """移除大小写、空白、标点和繁简差异，生成相似度比较使用的紧凑文本。"""
        text = str(value or "").casefold()
        try:
            # 候选比对统一简体，避免条目繁体写法造成失配
            text = zhconv_convert(text, "zh-hans")
        except Exception:  # pylint: disable=broad-except
            pass
        return re.sub(r"[\W_]+", "", text, flags=re.UNICODE)

    @classmethod
    def _unique_texts(cls, values: Iterable[Optional[str]]) -> list[str]:
        """按规范化文本去重并保留原始顺序。"""
        results: list[str] = []
        seen: set[str] = set()
        for value in values:
            normalized = cls._normalize_text(value)
            identity = normalized.casefold()
            if not normalized or identity in seen:
                continue
            seen.add(identity)
            results.append(normalized)
        return results

    @classmethod
    def _release_to_album(cls, detail: dict[str, Any]) -> Optional[MusicAlbumInfo]:
        """将 MusicBrainz Release 详情转换为带曲目表的标准化专辑信息。"""
        release_id = detail.get("id")
        title = detail.get("title")
        if not release_id or not title:
            return None
        release_group = detail.get("release-group") or {}
        group_id = release_group.get("id")
        artists, artist_ids = cls._artist_credits(detail.get("artist-credit"))
        album = MusicAlbumInfo(
            media_source=cls._source,
            # 优先使用 Release Group ID，与专辑详情和封面入口保持一致
            media_id=str(group_id or release_id),
            title=str(title),
            artists=artists,
            artist_ids=artist_ids,
            album_type=cls._stripped(release_group.get("primary-type")),
            secondary_types=[cls._stripped(item) for item in release_group.get("secondary-types") or [] if cls._stripped(item)],
            release_date=detail.get("date") or None,
            cover_url=cls._build_cover_url(group_id),
            genres=cls._names_of(detail.get("genres")),
            detail_link=f"https://musicbrainz.org/release/{release_id}",
            raw_data={"release_id": str(release_id)},
        )
        album.tracks = [
            info
            for medium in detail.get("media") or []
            for track in medium.get("tracks") or []
            if (info := cls._track_to_info(album, medium, track))
        ]
        return album

    def recognize_media(
            self,
            meta: MetaBase = None,
            mtype: MediaType = None,
            media_source: Optional[MediaSource] = None,
            media_id: Optional[str] = None,
            **kwargs,
    ) -> Optional[MusicInfo]:
        """跟随统一媒体识别分发，仅在音乐类型请求下返回 MusicBrainz 识别结果。"""
        plan = self._recognition_plan(
            meta=meta,
            mtype=mtype,
            media_source=media_source,
            media_id=media_id,
            music_type=kwargs.get("music_type"),
            cache_enabled=bool(kwargs.get("cache", True)),
        )
        if not plan:
            return None
        if plan.media_id:
            info = self.recognize_music(
                plan.media_source,
                plan.require_media_id(),
                **plan.detail_kwargs(),
            )
            return self._finalize_detail_recognition(plan, info)
        return self._recognize_from_candidates_sync(plan)

    def _update_recognize_cache(self, meta: MetaMusic, info: Optional[MusicInfo]) -> None:
        """识别完成后把结果写入本地识别缓存，未挂载缓存时静默跳过。"""
        if self.cache:
            self.cache.update(meta, info)

    def update_recognize_cache(
            self,
            meta: MetaBase,
            mediainfo: MusicInfo,
    ) -> Optional[bool]:
        """回填音乐本地识别缓存，共享识别成功后避免重复回查。"""
        if not meta or not mediainfo:
            return None
        if not isinstance(meta, MetaMusic) or not isinstance(mediainfo, MusicInfo):
            return None
        if mediainfo.media_source != self._source:
            return None
        self._update_recognize_cache(meta, mediainfo)
        return True

    async def async_update_recognize_cache(
            self,
            meta: MetaBase,
            mediainfo: MusicInfo,
    ) -> Optional[bool]:
        """异步回填音乐本地识别缓存。"""
        return self.update_recognize_cache(meta=meta, mediainfo=mediainfo)

    async def async_recognize_media(
            self,
            meta: MetaBase = None,
            mtype: MediaType = None,
            media_source: Optional[MediaSource] = None,
            media_id: Optional[str] = None,
            **kwargs,
    ) -> Optional[MusicInfo]:
        """异步识别 MusicBrainz 音乐详情或按元数据匹配单曲。"""
        plan = self._recognition_plan(
            meta=meta,
            mtype=mtype,
            media_source=media_source,
            media_id=media_id,
            music_type=kwargs.get("music_type"),
            cache_enabled=bool(kwargs.get("cache", True)),
        )
        if not plan:
            return None
        if plan.media_id:
            info = await self.async_recognize_music(
                plan.media_source,
                plan.require_media_id(),
                **plan.detail_kwargs(),
            )
            return self._finalize_detail_recognition(plan, info)
        return await self._recognize_from_candidates_async(plan)

    @classmethod
    def _recognition_plan(
            cls,
            meta: Optional[MetaBase],
            mtype: Optional[MediaType],
            media_source: Optional[MediaSource],
            media_id: Optional[str],
            music_type: Optional[str],
            cache_enabled: bool,
    ) -> Optional[_MusicBrainzRecognitionPlan]:
        """统一完成来源、音乐类型、显式身份和缓存准入。"""
        if media_source and media_source != cls._source:
            return None
        if not isinstance(meta, MetaMusic):
            if mtype != MediaType.MUSIC and media_source != cls._source:
                return None
            if media_source != cls._source or not media_id:
                return None
            return _MusicBrainzRecognitionPlan(
                meta=None,
                media_source=cls._source,
                media_id=str(media_id),
                music_type=music_type,
                cache_enabled=False,
            )
        resolved_source = media_source or meta.media_source
        resolved_media_id = media_id or meta.media_id
        return _MusicBrainzRecognitionPlan(
            meta=meta,
            media_source=resolved_source or cls._source,
            media_id=(
                str(resolved_media_id)
                if resolved_source and resolved_media_id
                else None
            ),
            music_type=music_type,
            cache_enabled=cache_enabled,
        )

    def _cached_recognition(
            self, plan: _MusicBrainzRecognitionPlan
    ) -> Optional[MusicInfo]:
        """读取并标记一次候选识别缓存命中。"""
        meta = plan.require_meta()
        if not plan.cache_enabled or not self.cache:
            return None
        cached_info = self.cache.get(meta)
        if not cached_info:
            return None
        if cached_info.media_id:
            logger.info(f"{meta.title} 使用音乐识别缓存：{cached_info.title}")
        else:
            logger.info(f"{meta.title} 使用音乐识别缓存：无法识别")
        cached_info.recognize_cache_hit = True
        return cached_info

    def _finalize_detail_recognition(
            self,
            plan: _MusicBrainzRecognitionPlan,
            info: Optional[MusicInfo],
    ) -> Optional[MusicInfo]:
        """统一完成显式详情识别后的缓存回填。"""
        if info and plan.meta:
            self._update_recognize_cache(plan.meta, info)
        return info

    @classmethod
    def _select_recognition_candidate(
            cls,
            plan: _MusicBrainzRecognitionPlan,
            recordings: Iterable[MusicInfo],
            albums: Iterable[MusicInfo] = (),
    ) -> Optional[MusicInfo]:
        """按同一来源和实体规则选择 Recording 或专辑候选。"""
        meta = plan.require_meta()
        if plan.music_type == MUSIC_ENTITY_ALBUM:
            return cls._select_album_candidate(meta, albums)
        matched = cls._select_candidate(
            meta, recordings, media_source=plan.media_source
        )
        if matched or not plan.search_album or not meta.artists:
            return matched
        return cls._select_album_candidate(meta, albums)

    @staticmethod
    def _should_search_albums(
            plan: _MusicBrainzRecognitionPlan,
            preliminary: Optional[MusicInfo],
    ) -> bool:
        """统一决定候选识别是否需要继续查询专辑。"""
        meta = plan.require_meta()
        return bool(
            plan.music_type == MUSIC_ENTITY_ALBUM
            or (not preliminary and plan.search_album and meta.artists)
        )

    @staticmethod
    def _should_probe_album(
            plan: _MusicBrainzRecognitionPlan,
            recording: Optional[MusicInfo],
    ) -> bool:
        """统一决定 Recording 详情未命中后是否继续探测专辑。"""
        return bool(not recording and plan.search_album)

    def _finalize_recognition(
            self,
            plan: _MusicBrainzRecognitionPlan,
            matched: Optional[MusicInfo],
    ) -> MusicInfo:
        """统一生成候选识别兜底并写入本地缓存。"""
        meta = plan.require_meta()
        result = matched or self._info_from_meta(meta)
        self._update_recognize_cache(meta, result)
        return result

    def _recognize_from_candidates_sync(
            self, plan: _MusicBrainzRecognitionPlan
    ) -> Optional[MusicInfo]:
        """同步获取候选，所有准入与选择由共享决策方法完成。"""
        meta = plan.require_meta()
        if plan.music_type != MUSIC_ENTITY_ALBUM:
            cached_info = self._cached_recognition(plan)
            if cached_info:
                return cached_info
        recordings = (
            self._search_recordings(meta, limit=10)
            if plan.search_recording else []
        )
        preliminary = self._select_recognition_candidate(plan, recordings)
        albums = (
            self._search_albums(meta, limit=10)
            if self._should_search_albums(plan, preliminary)
            else []
        )
        matched = self._select_recognition_candidate(plan, recordings, albums)
        if plan.music_type == MUSIC_ENTITY_ALBUM:
            return matched
        return self._finalize_recognition(plan, matched)

    async def _recognize_from_candidates_async(
            self, plan: _MusicBrainzRecognitionPlan
    ) -> Optional[MusicInfo]:
        """异步获取候选，所有准入与选择由共享决策方法完成。"""
        meta = plan.require_meta()
        if plan.music_type != MUSIC_ENTITY_ALBUM:
            cached_info = self._cached_recognition(plan)
            if cached_info:
                return cached_info
        recordings = (
            await self._async_search_recordings(meta, limit=10)
            if plan.search_recording else []
        )
        preliminary = self._select_recognition_candidate(plan, recordings)
        albums = (
            await self._async_search_albums(meta, limit=10)
            if self._should_search_albums(plan, preliminary)
            else []
        )
        matched = self._select_recognition_candidate(plan, recordings, albums)
        if plan.music_type == MUSIC_ENTITY_ALBUM:
            return matched
        return self._finalize_recognition(plan, matched)

    @classmethod
    def _select_candidate(
            cls,
            meta: MetaMusic,
            candidates: Iterable[MusicInfo],
            media_source: MediaSource,
    ) -> Optional[MusicInfo]:
        """按标题、艺术家和专辑匹配度选择最可信的搜索候选。"""
        normalized_source = cls._normalize_text(media_source).casefold()
        # 资源标题携带的音质标记先剥离，再与候选曲名比对；
        # 曲名开头的艺术家署名前缀是命名习惯，用主体名比对
        clean_title = cls._strip_artist_prefix(cls._search_title(meta.title), meta.artists)
        # 条目的影视 tie-in 注释多为全角括号，与资源半角注释无法精确相等，
        # 去括号后的主体曲名一致视为弱匹配，且需艺术家同时命中才采信；
        # 卷号后缀（Vol. 3）是发行分卷标记，条目本体不含卷号
        bare_title = cls._strip_volume_suffix(cls._strip_parenthetical(clean_title))
        ranked: list[tuple[int, MusicInfo]] = []
        for candidate in candidates:
            if normalized_source and str(candidate.media_source or "").casefold() != normalized_source:
                continue
            score = 0
            title_match = False
            # 多艺术家资源任一命中即可，联名候选不会因主艺术家顺序失配
            artist_match = bool(meta.artists) and any(
                cls._same_text(artist_name, candidate_artist)
                for artist_name in meta.artists
                for candidate_artist in candidate.artists
            )
            if clean_title and cls._same_text(clean_title, candidate.title):
                score += 4
                title_match = True
            elif (
                bare_title
                and artist_match
                and (
                    cls._same_text(bare_title, cls._strip_parenthetical(candidate.title))
                    # 条目「天國的情人：鄧麗君逝世十周年…」这类冒号副标题，主标题一致视为弱匹配
                    or cls._same_text(bare_title, cls._main_title(candidate.title))
                    # 条目「为你盛开-许巍《无尽光芒》…」这类连字符前置命名，首段曲名一致视为弱匹配
                    or cls._same_text(bare_title, cls._head_title(candidate.title))
                    # 条目「愛情電影主題曲 雲且留住」这类「主体名 补充说明」结构，首段一致视为弱匹配
                    or (
                        len(cls._match_text(bare_title)) >= 3
                        and cls._same_text(bare_title, cls._lead_token(candidate.title))
                    )
                    # 条目带额外前缀/后缀完整包含资源主体名（好莱坞原声带类），长文本包含视为弱匹配
                    or (
                        len(cls._match_text(bare_title)) >= 6
                        and cls._match_text(bare_title) in cls._match_text(candidate.title)
                    )
                    # 资源标题带演出后缀（S.H.E十七音乐会），条目本体一致视为弱匹配
                    or (
                        cls._performance_title(bare_title)
                        and cls._same_text(cls._performance_title(bare_title), candidate.title)
                    )
                )
            ):
                score += 2
                title_match = True
            if artist_match:
                score += 3
            if meta.album and cls._same_text(meta.album, candidate.album):
                score += 2
            isrc_match = bool(meta.isrc) and cls._same_text(meta.isrc, candidate.isrc)
            if isrc_match:
                score += 5
            # 同名多版本（如不同年份的重发单曲）靠发行年份消歧
            if meta.year and candidate.year and int(meta.year) == int(candidate.year):
                score += 1
            # 已知艺术家时，艺术家未命中的候选不能采信（ISRC 精确身份除外），
            # 兜住宽检索阶梯下同名异曲的误配；CJK 逐字 OR 检索召回宽，
            # 标题未命中的候选同样不能仅凭艺术家署名得分（ISRC 除外）
            if (meta.artists and not artist_match and not isrc_match) or (
                not title_match and not isrc_match
            ):
                score = 0
            ranked.append((score, candidate))
        if not ranked:
            return None
        ranked.sort(key=lambda item: item[0], reverse=True)
        return ranked[0][1] if ranked[0][0] > 0 else None

    @classmethod
    def _select_album_candidate(cls, meta: MetaMusic, albums: Iterable[MusicInfo]) -> Optional[MusicInfo]:
        """单曲检索未命中时，从专辑候选中挑选高置信目标。

        专辑重名多，要求标题（含去括号弱匹配）与艺术家同时命中才返回，
        避免把音轨身份安到错误专辑上。
        """
        clean_title = cls._strip_artist_prefix(
            cls._search_title(meta.album or meta.title), meta.artists)
        if not clean_title:
            return None
        # 去括号与卷号后缀后的本体名用于弱匹配（好歌茹芸, Vol. 3 -> 好歌茹芸）；
        # 资源带卷号时弱匹配要求候选卷号一致，避免 Ibiza Vol.1 误配 Vol.3
        bare_title = cls._strip_volume_suffix(cls._strip_parenthetical(clean_title))
        meta_volume = cls._volume_number(clean_title)
        ranked: list[tuple[int, MusicInfo]] = []
        for album in albums:
            score = 0
            album_title = album.title or album.album
            artist_match = bool(meta.artists) and any(
                cls._same_text(artist_name, candidate_artist)
                for artist_name in meta.artists
                for candidate_artist in album.artists
            )
            title_match = False
            # 资源带卷号时候选卷号不一致（含其他分卷）直接排除，避免 Vol.1 误配 Vol.3
            if meta_volume and cls._volume_number(album_title) not in (None, meta_volume):
                pass
            elif cls._same_text(clean_title, album_title):
                score += 4
                title_match = True
            elif (
                artist_match
                and bare_title
                and (
                    cls._same_text(bare_title, cls._strip_parenthetical(album_title))
                    # 条目「天國的情人：鄧麗君逝世十周年…」这类冒号副标题，主标题一致视为弱匹配
                    or cls._same_text(bare_title, cls._main_title(album_title))
                    # 条目「为你盛开-许巍《无尽光芒》…」这类连字符前置命名，首段曲名一致视为弱匹配
                    or cls._same_text(bare_title, cls._head_title(album_title))
                    # 条目「愛情電影主題曲 雲且留住」这类「主体名 补充说明」结构，首段一致视为弱匹配
                    or (
                        len(cls._match_text(bare_title)) >= 3
                        and cls._same_text(bare_title, cls._lead_token(album_title))
                    )
                    # 条目带额外前缀/后缀完整包含资源主体名（好莱坞原声带类），长文本包含视为弱匹配
                    or (
                        len(cls._match_text(bare_title)) >= 6
                        and cls._match_text(bare_title) in cls._match_text(album_title)
                    )
                    # 资源标题带演出后缀（S.H.E十七音乐会），条目本体一致视为弱匹配
                    or (
                        cls._performance_title(bare_title)
                        and cls._same_text(cls._performance_title(bare_title), album_title)
                    )
                    # 原声带资源的描述词在条目中常省略（Pulp Fiction: Music From the…），
                    # 电影名本体与条目标题或主标题一致视为弱匹配
                    or (
                        len(cls._match_text(cls._soundtrack_body(bare_title))) >= 4
                        and (
                            cls._same_text(cls._soundtrack_body(bare_title), album_title)
                            or cls._same_text(cls._soundtrack_body(bare_title), cls._main_title(album_title))
                        )
                    )
                )
            ):
                # 「我爱夜 (新歌+精选)」对位条目「我爱夜」这类注释差异
                score += 2
                title_match = True
            if artist_match:
                score += 3
            if meta.year and album.year and int(meta.year) == int(album.year):
                score += 1
            # 标题与艺术家缺一不可，仅有标题相似不能采信
            ranked.append((score if title_match and artist_match else 0, album))
        if not ranked:
            return None
        ranked.sort(key=lambda item: item[0], reverse=True)
        return ranked[0][1] if ranked[0][0] > 0 else None

    @classmethod
    def _info_from_meta(cls, meta: MetaMusic) -> MusicInfo:
        """音乐识别无候选时，把元数据转换为可展示的最小信息。"""
        return MusicInfo.from_meta(meta)

    @classmethod
    def _same_text(cls, left: Optional[str], right: Optional[str]) -> bool:
        """忽略大小写、空白与标点差异比较两个音乐文本字段。"""
        compact_left = cls._match_text(left)
        compact_right = cls._match_text(right)
        if compact_left and compact_left == compact_right:
            return True
        # 条目与资源标题的汉字数字写法不一致（十三首/13首），归一后再比对
        return bool(compact_left) and cls._convert_cjk_numerals(
            compact_left) == cls._convert_cjk_numerals(compact_right)

    # 常见汉字数字归一为阿拉伯数字：十三首/13首、二十周年/20周年 在条目与资源标题间混用
    _CJK_DIGIT_MAP = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
                      "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    _CJK_NUMERAL_RUN_RE = re.compile(r"[零一二两三四五六七八九十百]+")

    @classmethod
    def _convert_cjk_numerals(cls, value: str) -> str:
        """将文本中连续的汉字数字转换为阿拉伯数字，支持十位与百位组合。"""

        def _convert(run: str) -> str:
            total, current = 0, 0
            for char in run:
                if char in cls._CJK_DIGIT_MAP:
                    current = cls._CJK_DIGIT_MAP[char]
                elif char == "十":
                    total += (current or 1) * 10
                    current = 0
                elif char == "百":
                    total += (current or 1) * 100
                    current = 0
            return str(total + current)

        return cls._CJK_NUMERAL_RUN_RE.sub(lambda m: _convert(m.group()), str(value or ""))

    @classmethod
    def _normalize_text(cls, value: Optional[str]) -> str:
        """清理音乐检索文本中的多余空白，并统一繁简写法避免候选比对失误。"""
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if not text:
            return text
        # MusicBrainz 中文条目以简体为主，资源标题可能是繁体，比对前统一转简体
        try:
            return zhconv_convert(text, "zh-hans")
        except Exception:  # pylint: disable=broad-except
            return text

    # 资源标题中的音质规格（格式、位深采样参数、年份后缀、发行实体标记）会污染检索式，检索前统一剥离
    _quality_token_pattern = re.compile(
        r"\[[^\]]*\]|\((?:19|20)\d{2}\)|"
        r"\b(?:DSD(?:64|128|256|512)?|DSF|DFF|FLAC|ALAC|APE|WAV|WAVE|AIFF?|PCM|"
        r"MP3|AAC|M4A|OGG|VORBIS|OPUS|WMA|WEB-?DL|WEBRip|WEB)\b|"
        r"\b\d{1,3}\s*-?\s*bits?\b|\b\d{2,4}(?:\.\d)?\s*k(?:hz|bps?)\b|"
        # 流媒体发行实体标记（- Single / - EP），不是曲名的一部分
        r"\b(?:single|ep|album)\b",
        re.IGNORECASE,
    )

    @classmethod
    def _search_title(cls, value: Optional[str]) -> str:
        """剥离资源标题中的音频格式、规格参数与年份后缀，只保留曲名用于检索比对。"""
        text = cls._quality_token_pattern.sub(" ", str(value or ""))
        # 流媒体文件名消毒产生的下划线转空格，避免破坏检索短语
        text = text.replace("_", " ")
        # 规格剥离后可能残留悬空分隔符，统一修剪
        text = re.sub(r"^[\s\-–—/]+|[\s\-–—/]+$", "", cls._normalize_text(text))
        # 格式标记后紧跟的场景发布组标签（如 ALAC-HHWEB），整体剔除
        text = re.sub(r"[-–—]\s*[A-Z0-9]{3,}\s*$", "", text)
        # 曲名尾部独立年份是发行线索不是曲名一部分（解析阶段通常已提取），
        # 反复剥离尾部年份：场景命名可能重复携带（Live At Montreux 2011 2011）
        text = cls._normalize_text(text)
        while True:
            # 仅剔除空白分隔的尾部年份，纯年份标题（1999）无前导空白不受影响
            stripped = re.sub(r"\s+(?:19|20)\d{2}$", "", text)
            if stripped == text:
                break
            text = stripped
        return cls._normalize_text(text)

    def recognize_music(
            self,
            media_source: MediaSource,
            media_id: str,
            music_type: Optional[str] = None,
    ) -> Optional[MusicInfo]:
        """按 MusicBrainz 标准 ID 和实体类型获取详情；空类型保留旧版探测顺序。"""
        plan = self._detail_plan(media_source, media_id, music_type)
        if not plan:
            return None
        result: Optional[MusicInfo] = None
        if plan.search_recording:
            payload = self._request_json(
                f"/recording/{plan.require_media_id()}",
                params={
                    "inc": "artists+releases+release-groups+isrcs+genres",
                    "fmt": "json",
                },
            )
            result = self._project_recording_detail(payload)
            if result:
                return result
        if not self._should_probe_album(plan, result):
            return None
        # MusicBrainz 各实体共用 UUID 形式，统一详情入口在 Recording 未命中后继续探测专辑。
        album = self.music_album(self._source, plan.require_media_id())
        return self._project_album_result(album)

    async def async_recognize_music(
            self,
            media_source: MediaSource,
            media_id: str,
            music_type: Optional[str] = None,
    ) -> Optional[MusicInfo]:
        """异步按 MusicBrainz 标准 ID 和实体类型获取详情。"""
        plan = self._detail_plan(media_source, media_id, music_type)
        if not plan:
            return None
        result: Optional[MusicInfo] = None
        if plan.search_recording:
            payload = await self._async_request_json(
                f"/recording/{plan.require_media_id()}",
                params={
                    "inc": "artists+releases+release-groups+isrcs+genres",
                    "fmt": "json",
                },
            )
            result = self._project_recording_detail(payload)
            if result:
                return result
        if not self._should_probe_album(plan, result):
            return None
        album = await self._async_music_album(
            self._source, plan.require_media_id()
        )
        return self._project_album_result(album)

    @classmethod
    def _detail_plan(
            cls,
            media_source: MediaSource,
            media_id: str,
            music_type: Optional[str],
    ) -> Optional[_MusicBrainzRecognitionPlan]:
        """统一校验详情来源并冻结 Recording 到专辑的探测顺序。"""
        if media_source != cls._source or not media_id:
            return None
        return _MusicBrainzRecognitionPlan(
            meta=None,
            media_source=cls._source,
            media_id=str(media_id),
            music_type=music_type,
            cache_enabled=False,
        )

    @classmethod
    def _project_recording_detail(
            cls, payload: Optional[dict[str, Any]]
    ) -> Optional[MusicInfo]:
        """把 Recording 详情响应投影为统一音乐信息。"""
        return cls._recording_to_info(payload) if payload else None

    @staticmethod
    def _project_album_result(
            album: Optional[MusicAlbumInfo],
    ) -> Optional[MusicInfo]:
        """把专辑详情统一投影到音乐识别返回类型。"""
        return album.to_music_info() if album else None

    async def _async_music_album(
            self,
            media_source: MediaSource,
            media_id: str,
    ) -> Optional[MusicAlbumInfo]:
        """异步按 MusicBrainz Release Group ID 获取专辑详情及曲目。"""
        if not self._detail_plan(media_source, media_id, MUSIC_ENTITY_ALBUM):
            return None
        payload = await self._async_request_json(
            f"/release-group/{media_id}",
            params={
                "inc": "artists+releases+media+genres+tags+ratings",
                "fmt": "json",
            },
        )
        album = self._project_album_detail(payload)
        if not album:
            return None
        tracks_payload = await self._async_album_tracks_payload(
            payload.get("releases") or []
        )
        album.tracks = self._project_album_tracks(album, tracks_payload)
        return album

    def music_album(
            self,
            media_source: MediaSource,
            media_id: str,
    ) -> Optional[MusicAlbumInfo]:
        """按 MusicBrainz Release Group ID 获取标准化专辑详情及曲目。"""
        if not self._detail_plan(media_source, media_id, MUSIC_ENTITY_ALBUM):
            return None
        payload = self._request_json(
            f"/release-group/{media_id}",
            params={
                "inc": "artists+releases+media+genres+tags+ratings",
                "fmt": "json",
            },
        )
        album = self._project_album_detail(payload)
        if not album:
            return None
        tracks_payload = self._album_tracks_payload(payload.get("releases") or [])
        album.tracks = self._project_album_tracks(album, tracks_payload)
        return album

    @classmethod
    def _project_album_detail(
            cls, payload: Optional[dict[str, Any]]
    ) -> Optional[MusicAlbumInfo]:
        """把 Release Group 详情投影为尚未装载曲目的专辑。"""
        if not payload:
            return None
        album = cls._release_group_to_album(payload)
        if album:
            album.releases = cls._release_variants(payload.get("releases") or [])
        return album

    def music_artist(
            self,
            media_source: MediaSource,
            media_id: str,
    ) -> Optional[MusicArtistInfo]:
        """按 MusicBrainz Artist ID 获取标准化艺术家详情。"""
        if media_source != self._source or not media_id:
            return None
        payload = self._request_json(
            f"/artist/{media_id}",
            params={"inc": "url-rels+genres+tags+aliases", "fmt": "json"},
        )
        return self._artist_to_info(payload) if payload else None

    def music_artist_albums(
            self,
            media_source: MediaSource,
            media_id: str,
            page: int = 1,
            count: int = 30,
            album_type: Optional[str] = None,
    ) -> list[MusicInfo]:
        """按 MusicBrainz Artist ID 分页浏览该艺术家的专辑、EP 和单曲。"""
        if media_source != self._source or not media_id:
            return []
        limit = max(1, min(count, 100))
        params: dict[str, Any] = {
            "artist": media_id,
            "inc": "artist-credits",
            "limit": limit,
            "offset": max(page - 1, 0) * limit,
            "fmt": "json",
        }
        if album_type:
            params["type"] = album_type
        payload = self._request_json("/release-group", params=params)
        albums = [
            album
            for item in (payload or {}).get("release-groups") or []
            if (album := self._release_group_to_album(item))
        ]
        # MusicBrainz 浏览接口不支持排序，只能在当前页内按发行日期倒序，保证首页是最新作品
        albums.sort(key=lambda item: item.release_date or "", reverse=True)
        return [album.to_music_info() for album in albums]

    def music_artist_related(
            self,
            media_source: MediaSource,
            media_id: str,
            count: int = 24,
    ) -> list[MusicArtistInfo]:
        """按 MusicBrainz 艺术家关系返回可继续浏览的关联艺术家。"""
        if media_source != self._source or not media_id:
            return []
        payload = self._request_json(
            f"/artist/{media_id}",
            params={"inc": "artist-rels", "fmt": "json"},
        )
        if not payload:
            return []
        return self._related_artists(payload.get("relations") or [], count=count)

    @classmethod
    def _build_query(cls, meta: MetaMusic) -> str:
        """构造 MusicBrainz Recording 搜索表达式。"""
        clauses = []
        # 资源标题先剥离音质标记，避免规格文本污染检索式导致零命中
        title = cls._search_title(meta.title)
        if title:
            clauses.append(f"recording:{cls._query_phrase(title)}")
        if meta.artists:
            clauses.append(f'artist:"{cls._escape_query(meta.artists[0])}"')
        if meta.album:
            clauses.append(f'release:"{cls._escape_query(meta.album)}"')
        if meta.isrc:
            clauses.append(f'isrc:"{cls._escape_query(meta.isrc)}"')
        return " AND ".join(clauses)

    @staticmethod
    def _escape_query(value: str) -> str:
        """转义 MusicBrainz 查询中的引号和反斜线。"""
        return value.replace("\\", "\\\\").replace('"', '\\"').strip()

    # 中日韩字符：Lucene 标准分词器不会切分连续 CJK，短语检索对中文标题永远零命中
    _QUERY_CJK_RE = re.compile(
        r"[\u3040-\u30FF\u3400-\u4DBF\u4E00-\u9FFF\uAC00-\uD7AF]")
    # 检索词元切分：按空白、标点与括号拆分，保留 CJK 串与拉丁词（括号对逐字检索无意义）
    _QUERY_TOKEN_SPLIT_RE = re.compile(
        r"[\s\-–—−－。，、；：！？·．…()（）「」『』【】\[\]《》,;]+")

    @classmethod
    def _query_phrase(cls, value: Optional[str]) -> Optional[str]:
        """构造适配 Lucene 分词的检索表达式。

        无 CJK 的普通文本返回带引号短语；含 CJK 的文本拆为词元后用 OR 交集检索，
        MusicBrainz 索引中连续 CJK 是单一词元，逐字 OR 才能命中（「茹此精彩十三首」）；
        过宽的召回由候选挑选阶段的标题与艺术家比对收紧。
        """
        text = str(value or "").strip()
        if not text:
            return None
        if not cls._QUERY_CJK_RE.search(text):
            return f'"{cls._escape_query(text)}"'
        tokens = [
            token for token in cls._QUERY_TOKEN_SPLIT_RE.split(text) if token.strip()
        ]
        if not tokens:
            return None
        parts = [
            f'"{cls._escape_query(token)}"'
            if not cls._QUERY_CJK_RE.search(token) else cls._or_group(
                [f'"{cls._escape_query(char)}"' for char in token]
            )
            for token in tokens
        ]
        return cls._or_group(parts)

    @staticmethod
    def _or_group(parts: list[str]) -> str:
        """拼接 OR 检索表达式，多项时用括号包裹避免与外层 AND 产生优先级歧义。"""
        if len(parts) == 1:
            return parts[0]
        return "(" + " OR ".join(parts) + ")"

    @classmethod
    def _recording_to_info(cls, recording: dict[str, Any]) -> Optional[MusicInfo]:
        """将 MusicBrainz Recording 响应转换为标准音乐信息。"""
        media_id = recording.get("id")
        title = recording.get("title")
        if not media_id or not title:
            return None
        releases = recording.get("releases") or []
        release = cls._select_release(releases)
        release_group = (release or {}).get("release-group") or {}
        release_date = cls._release_date(recording, release)
        album = (release or {}).get("title")
        artists, artist_ids = cls._artist_credits(recording.get("artist-credit"))
        album_artists, _ = cls._artist_credits((release or {}).get("artist-credit"))
        secondary_types = [
            value
            for item in release_group.get("secondary-types") or []
            if (value := cls._stripped(item))
        ]
        category_parts = [cls._stripped(release_group.get("primary-type")), *secondary_types]
        return MusicInfo(
            media_source=cls._source,
            media_id=str(media_id),
            title=str(title),
            artists=artists,
            artist_ids=artist_ids,
            album=album,
            album_artist=" / ".join(album_artists) if album_artists else None,
            album_id=str(release_group["id"]) if release_group.get("id") else None,
            album_type=cls._stripped(release_group.get("primary-type")),
            secondary_types=secondary_types,
            year=cls._year(release_date),
            release_date=release_date,
            duration=cls._duration_seconds(recording.get("length")),
            isrc=next(iter(recording.get("isrcs") or []), None),
            cover_url=cls._build_cover_url(release_group.get("id")),
            version=recording.get("disambiguation") or None,
            metadata_category=" / ".join(str(part) for part in category_parts if part),
            genres=cls._names_of(recording.get("genres")),
            release_status=cls._stripped((release or {}).get("status")),
            names=[name for name in (title, album) if name],
            detail_link=f"{cls._detail_url}/{media_id}",
            raw_data=recording,
        )

    @classmethod
    def _release_group_to_album(cls, release_group: dict[str, Any]) -> Optional[MusicAlbumInfo]:
        """将 MusicBrainz Release Group 响应转换为标准专辑信息。"""
        media_id = release_group.get("id")
        title = release_group.get("title")
        if not media_id or not title:
            return None
        artists, artist_ids = cls._artist_credits(release_group.get("artist-credit"))
        rating = release_group.get("rating") or {}
        return MusicAlbumInfo(
            media_source=cls._source,
            media_id=str(media_id),
            title=str(title),
            artists=artists,
            artist_ids=artist_ids,
            album_type=cls._stripped(release_group.get("primary-type")),
            secondary_types=[cls._stripped(item) for item in release_group.get("secondary-types") or [] if cls._stripped(item)],
            release_date=release_group.get("first-release-date") or None,
            cover_url=cls._build_cover_url(media_id),
            genres=cls._names_of(release_group.get("genres")),
            tags=cls._names_of(release_group.get("tags")),
            # MusicBrainz 评分是 5 分制，统一放大到与影视一致的 10 分制展示
            rating=round(float(rating["value"]) * 2, 1) if rating.get("value") else 0.0,
            rating_votes=rating.get("votes-count"),
            detail_link=f"{cls._album_detail_url}/{media_id}",
            raw_data=release_group,
        )

    @classmethod
    def _release_variants(cls, releases: list[dict[str, Any]]) -> list[MusicRelease]:
        """整理同一专辑下的发行版本，供详情页对比介质和地区。"""
        variants = []
        for release in releases:
            if not release.get("id"):
                continue
            media = release.get("media") or []
            variants.append(
                MusicRelease(
                    media_id=str(release["id"]),
                    title=release.get("title"),
                    date=release.get("date") or None,
                    country=release.get("country") or None,
                    status=release.get("status") or None,
                    packaging=release.get("packaging") or None,
                    formats=[str(item["format"]) for item in media if item.get("format")],
                    track_count=sum(int(item.get("track-count") or 0) for item in media) or None,
                )
            )
        variants.sort(key=lambda item: (cls._date_sort_key(item.date), item.title or ""))
        return variants

    @staticmethod
    def _artist_credits(
            artist_credit: Optional[list[dict[str, Any]]],
    ) -> Tuple[list[str], list[str]]:
        """从 MusicBrainz artist-credit 提取有序艺术家名称和按位置对齐的标准 ID。"""
        names: list[str] = []
        ids: list[str] = []
        for credit in artist_credit or []:
            artist = credit.get("artist") or {}
            name = artist.get("name") or credit.get("name")
            if not name or str(name) in names:
                continue
            names.append(str(name))
            # 名称与 ID 按下标一一对应，缺少 ID 时补空串，前端据此决定是否可跳转
            ids.append(str(artist.get("id") or ""))
        return names, ids

    @staticmethod
    def _names_of(items: Optional[list[dict[str, Any]]]) -> list[str]:
        """提取 MusicBrainz 风格、标签或别名列表的名称，热度高的排在前面。"""
        entries = [item for item in items or [] if item.get("name")]
        entries.sort(key=lambda item: (-int(item.get("count") or 0), str(item["name"])))
        return [str(item["name"]) for item in entries]

    @classmethod
    def _select_track_release(cls, releases: list[dict[str, Any]]) -> dict[str, Any]:
        """选择曲目最完整且发行最早的正式版本，作为专辑曲目来源。"""
        candidates = [
            release
            for release in releases
            if release.get("id")
            and sum(int(item.get("track-count") or 0) for item in release.get("media") or [])
        ]
        if not candidates:
            return next((release for release in releases if release.get("id")), {})
        official = [release for release in candidates if release.get("status") == "Official"]
        return min(
            official or candidates,
            key=lambda release: cls._date_sort_key(release.get("date")),
        )

    @classmethod
    def _album_tracks_payload(
            cls, releases: list[dict[str, Any]]
    ) -> Optional[dict[str, Any]]:
        """同步读取专辑代表性发行版本的原始曲目响应。"""
        release = cls._select_track_release(releases)
        if not release.get("id"):
            return None
        payload = cls._request_json(
            f"/release/{release['id']}",
            params={"inc": "recordings+artist-credits", "fmt": "json"},
        )
        return payload if isinstance(payload, dict) else None

    @classmethod
    async def _async_album_tracks_payload(
            cls, releases: list[dict[str, Any]]
    ) -> Optional[dict[str, Any]]:
        """异步读取专辑代表性发行版本的原始曲目响应。"""
        release = cls._select_track_release(releases)
        if not release.get("id"):
            return None
        payload = await cls._async_request_json(
            f"/release/{release['id']}",
            params={"inc": "recordings+artist-credits", "fmt": "json"},
        )
        return payload if isinstance(payload, dict) else None

    @classmethod
    def _project_album_tracks(
            cls,
            album: MusicAlbumInfo,
            payload: Optional[dict[str, Any]],
    ) -> list[MusicInfo]:
        """把代表性发行版本响应投影为专辑曲目列表。"""
        tracks: list[MusicInfo] = []
        for medium in (payload or {}).get("media") or []:
            for track in medium.get("tracks") or []:
                info = cls._track_to_info(album, medium, track)
                if info:
                    tracks.append(info)
        return tracks

    @classmethod
    def _track_to_info(
            cls,
            album: MusicAlbumInfo,
            medium: dict[str, Any],
            track: dict[str, Any],
    ) -> Optional[MusicInfo]:
        """将发行版本中的单条曲目转换为可继续浏览的音乐信息。"""
        recording = track.get("recording") or {}
        media_id = recording.get("id")
        title = track.get("title") or recording.get("title")
        if not media_id or not title:
            return None
        artists, artist_ids = cls._artist_credits(
            track.get("artist-credit") or recording.get("artist-credit")
        )
        return MusicInfo(
            media_source=cls._source,
            media_id=str(media_id),
            title=str(title),
            artists=artists or list(album.artists),
            artist_ids=artist_ids or list(album.artist_ids),
            album=album.title,
            album_artist=album.artist or None,
            album_id=album.media_id,
            album_type=album.album_type,
            year=album.year,
            release_date=recording.get("first-release-date") or album.release_date,
            disc_number=cls._optional_int(medium.get("position")),
            track_number=cls._optional_int(track.get("position")),
            total_tracks=cls._optional_int(medium.get("track-count")),
            duration=cls._duration_seconds(track.get("length") or recording.get("length")),
            cover_url=album.cover_url,
            version=recording.get("disambiguation") or None,
            secondary_types=list(album.secondary_types),
            metadata_category=album.metadata_category,
            genres=list(album.genres),
            tags=list(album.tags),
            artist_country=album.artist_country,
            release_status=album.release_status,
            names=[str(title)],
            detail_link=f"{cls._detail_url}/{media_id}",
        )

    @classmethod
    def _select_release(cls, releases: list[dict[str, Any]]) -> dict[str, Any]:
        """优先选择正式且日期最早的发行记录。"""
        if not releases:
            return {}
        official = [release for release in releases if release.get("status") == "Official"]
        candidates = official or releases
        return min(
            candidates,
            key=lambda release: cls._date_sort_key(release.get("date")),
        )

    @staticmethod
    def _date_sort_key(value: Optional[str]) -> tuple[int, str]:
        """将完整或不完整发行日期转换为稳定排序键。"""
        return (0, value) if value else (1, "")

    @staticmethod
    def _release_date(recording: dict[str, Any], release: dict[str, Any]) -> Optional[str]:
        """从录音和发行信息中选择最可靠的发行日期。"""
        return recording.get("first-release-date") or release.get("date")

    @staticmethod
    def _year(release_date: Optional[str]) -> Optional[int]:
        """从 MusicBrainz 的可变精度日期提取年份。"""
        if not release_date:
            return None
        try:
            return int(release_date[:4])
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _duration_seconds(value: Any) -> Optional[int]:
        """将 MusicBrainz 毫秒时长转换为整数秒。"""
        try:
            return round(int(value) / 1000) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _stripped(value: Any) -> Optional[str]:
        """去除 MusicBrainz 响应字段两端的空白，空值返回 None。"""
        text = str(value).strip() if value is not None else ""
        return text or None

    @staticmethod
    def _optional_int(value: Any) -> Optional[int]:
        """将 MusicBrainz 的碟号、音轨号等计数字段转换为可选整数。"""
        try:
            return int(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None

    @classmethod
    def _artist_to_info(
            cls,
            artist: dict[str, Any],
            relation: Optional[str] = None,
            include_raw: bool = False,
    ) -> Optional[MusicArtistInfo]:
        """将 MusicBrainz Artist 响应转换为标准艺术家信息。"""
        media_id = artist.get("id")
        name = artist.get("name")
        if not media_id or not name:
            return None
        life_span = artist.get("life-span") or {}
        area = artist.get("area") or {}
        begin_area = artist.get("begin-area") or {}
        relations = artist.get("relations") or []
        return MusicArtistInfo(
            media_source=cls._source,
            media_id=str(media_id),
            name=str(name),
            sort_name=artist.get("sort-name") or None,
            disambiguation=artist.get("disambiguation") or None,
            artist_type=artist.get("type") or None,
            gender=artist.get("gender") or None,
            country=artist.get("country") or None,
            area=area.get("name") or begin_area.get("name") or None,
            begin_date=life_span.get("begin") or None,
            end_date=life_span.get("end") or None,
            ended=bool(life_span.get("ended")),
            genres=cls._names_of(artist.get("genres")),
            tags=cls._names_of(artist.get("tags")),
            aliases=cls._names_of(artist.get("aliases")),
            relation=relation,
            image_url=cls._artist_image(relations),
            detail_link=f"{cls._artist_detail_url}/{media_id}",
            external_links=cls._artist_links(relations),
            raw_data=artist if include_raw else {},
        )

    @classmethod
    def _artist_image(cls, relations: list[dict[str, Any]]) -> Optional[str]:
        """从艺术家 image 关系解析可直接展示的图片地址。"""
        for relation in relations:
            if relation.get("type") != "image":
                continue
            resource = (relation.get("url") or {}).get("resource") or ""
            # MusicBrainz 记录的是维基共享资源页地址，需要转成文件直链才能展示
            if "commons.wikimedia.org/wiki/File:" in resource:
                file_name = resource.rsplit("File:", 1)[-1]
                return (
                    "https://commons.wikimedia.org/wiki/Special:FilePath/"
                    f"{file_name}?width=500"
                )
            if resource:
                return resource
        return None

    @classmethod
    def _artist_links(cls, relations: list[dict[str, Any]]) -> dict[str, str]:
        """整理艺术家可对外跳转的官方与流媒体链接。"""
        links: dict[str, str] = {}
        for relation in relations:
            relation_type = str(relation.get("type") or "")
            resource = (relation.get("url") or {}).get("resource")
            if relation_type in cls._artist_link_types and resource:
                links.setdefault(relation_type, str(resource))
        return links

    @classmethod
    def _related_artists(
            cls,
            relations: list[dict[str, Any]],
            count: int,
    ) -> list[MusicArtistInfo]:
        """按关系类型优先级整理关联艺术家，并按来源去重。"""
        ranked: list[tuple[int, MusicArtistInfo]] = []
        seen: set[str] = set()
        fallback_priority = len(cls._artist_relation_priority)
        for relation in relations:
            if relation.get("target-type") != "artist":
                continue
            artist = relation.get("artist") or {}
            artist_id = str(artist.get("id") or "")
            if not artist_id or artist_id in seen:
                continue
            relation_type = str(relation.get("type") or "")
            info = cls._artist_to_info(artist, relation=relation_type or None)
            if not info:
                continue
            seen.add(artist_id)
            priority = (
                cls._artist_relation_priority.index(relation_type)
                if relation_type in cls._artist_relation_priority
                else fallback_priority
            )
            ranked.append((priority, info))
        ranked.sort(key=lambda item: (item[0], item[1].name or ""))
        return [info for _, info in ranked[: max(1, count)]]

    @classmethod
    def _build_cover_url(cls, release_group_id: Optional[str]) -> Optional[str]:
        """根据 Release Group ID 构造 Cover Art Archive 封面地址。"""
        if not release_group_id:
            return None
        # 支持配置音乐封面代理地址，解决 coverartarchive.org 无法访问的问题
        base = (get_runtime_setting('MUSIC_COVER_PROXY') or "https://coverartarchive.org").rstrip("/")
        return f"{base}/release-group/{release_group_id}/front-500"

    @classmethod
    def _get_request(cls) -> RequestUtils:
        """懒创建并复用统一 HTTP 客户端，批量识别时避免重复建连。"""
        if cls._request is None:
            with cls._client_lock:
                if cls._request is None:
                    cls._request = RequestUtils(
                        headers={
                            "User-Agent": (
                                f"{get_runtime_setting('USER_AGENT')} "
                                "(https://github.com/jxxghp/MoviePilot)"
                            ),
                            "Accept": "application/json",
                        },
                        proxies=get_runtime_setting('PROXY'),
                        use_session=True,
                        timeout=20,
                    )
        return cls._request

    @classmethod
    def _reserve_request_delay(cls) -> float:
        """为同步和异步 MusicBrainz 请求统一预留发送时间。"""
        with cls._request_lock:
            now = time.monotonic()
            request_at = max(now, cls._last_request_at + cls._request_interval)
            cls._last_request_at = request_at
            return max(0.0, request_at - now)

    @classmethod
    def _wait_for_rate_limit(cls) -> None:
        """同步等待 MusicBrainz 公共接口的已预留请求时间。"""
        if delay := cls._reserve_request_delay():
            time.sleep(delay)

    @classmethod
    async def _async_wait_for_rate_limit(cls) -> None:
        """异步等待 MusicBrainz 公共接口的已预留请求时间。"""
        if delay := cls._reserve_request_delay():
            await asyncio.sleep(delay)

    @classmethod
    def _response_decision(
            cls,
            response: Any,
            path: str,
            attempt: int,
            attempts: int,
    ) -> _MusicBrainzResponseDecision:
        """统一分类响应、解析 JSON，并决定是否执行下一次退避重试。"""
        status_code = response.status_code
        if status_code == 404:
            logger.debug(f"MusicBrainz 资源不存在：{path}")
            return _MusicBrainzResponseDecision(payload={})
        if status_code == 429 or status_code >= 500:
            logger.warning(
                f"MusicBrainz 服务繁忙：{status_code} {response.text[:200]}"
            )
            if attempt < attempts - 1:
                return _MusicBrainzResponseDecision(
                    retry_delay=cls._busy_backoff * (2 ** attempt)
                )
            return _MusicBrainzResponseDecision()
        if status_code != 200:
            logger.warning(
                f"MusicBrainz 请求失败：{status_code} {response.text[:200]}"
            )
            return _MusicBrainzResponseDecision()
        try:
            payload = response.json()
        except (TypeError, ValueError) as err:
            logger.warning(f"MusicBrainz 响应解析失败：{err}")
            return _MusicBrainzResponseDecision()
        return _MusicBrainzResponseDecision(
            payload=payload if isinstance(payload, dict) else None
        )

    @classmethod
    @cached(maxsize=get_runtime_setting('CONF').musicbrainz, ttl=get_runtime_setting('CONF').meta, skip_none=True)
    def _request_json(
            cls,
            path: str,
            params: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        """请求 MusicBrainz JSON 接口并统一处理网络和响应错误。

        服务端繁忙（429/5xx）属于瞬时错误，退避重试后再失败才放弃，
        避免批量识别场景下把限流误判为检索零命中。
        """
        attempts = cls._busy_retries + 1
        for attempt in range(attempts):
            cls._wait_for_rate_limit()
            response = cls._get_request().get_res(
                f"{cls._base_url}{path}", params=params
            )
            if response is None:
                return None
            try:
                decision = cls._response_decision(
                    response, path, attempt, attempts
                )
            finally:
                response.close()
            if decision.retry_delay is not None:
                time.sleep(decision.retry_delay)
                continue
            return decision.payload
        return None

    @classmethod
    @cached(
        maxsize=get_runtime_setting('CONF').musicbrainz,
        ttl=get_runtime_setting('CONF').meta,
        skip_none=True,
        shared_key="_request_json",
    )
    async def _async_request_json(
            cls,
            path: str,
            params: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        """异步请求 MusicBrainz JSON 接口并统一处理限流与响应错误。"""
        attempts = cls._busy_retries + 1
        for attempt in range(attempts):
            await cls._async_wait_for_rate_limit()
            response = await AsyncRequestUtils(
                headers={
                    "User-Agent": f"{get_runtime_setting('USER_AGENT')} (https://github.com/jxxghp/MoviePilot)",
                    "Accept": "application/json",
                },
                proxies=get_runtime_setting('PROXY'),
                timeout=20,
            ).get_res(f"{cls._base_url}{path}", params=params)
            if response is None:
                return None
            try:
                decision = cls._response_decision(
                    response, path, attempt, attempts
                )
            finally:
                await response.aclose()
            if decision.retry_delay is not None:
                await asyncio.sleep(decision.retry_delay)
                continue
            return decision.payload
        return None
