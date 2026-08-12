import re
from pathlib import Path
from typing import Any, Iterable, Optional, Union

from fastapi.concurrency import run_in_threadpool

from app.chain import ChainBase
from app.core.cache import async_fresh, fresh
from app.core.config import settings
from app.core.context import (
    MusicAlbumInfo,
    MusicArtistInfo,
    MusicInfo,
    MusicLyrics,
)
from app.core.meta import MetaMusic
from app.helper.audio import AudioMetadataHelper
from app.log import logger
from app.schemas.types import MUSIC_ENTITY_ALBUM, MUSIC_ENTITY_RECORDING, MediaType
from app.utils.media import (
    is_music_media_source,
    normalize_media_source,
    normalize_music_type,
)


class MusicChain(ChainBase):
    """音乐元数据搜索、探索与站点搜索参数编排链；媒体识别统一入口见 MediaChain。"""

    # 专辑目录匹配结果缓存：目录内相对路径变化时失效，标签写回不触发重复远端匹配。
    _album_dir_cache: dict[
        str,
        tuple[tuple[str, ...], dict[str, MusicInfo]],
    ] = {}
    _album_dir_cache_max = 128
    # 目录级匹配至少需要两个音频文件，单文件由单曲搜索链路处理
    _album_match_min_files = 2
    # 自动识别只使用 MusicBrainz；其它来源仅响应显式来源请求。
    _primary_recognize_source = "musicbrainz"

    @classmethod
    def parse_query(cls, query: str) -> MetaMusic:
        """将用户输入的搜索关键词解析为音乐元数据，解析核心在 MetaMusic.apply_title。"""
        return MetaMusic(org_string=query, title=query, parse_title=True)

    @classmethod
    def build_site_keywords(cls, music: MetaMusic | MusicInfo) -> list[str]:
        """按单曲或专辑实体生成站点关键词，避免单曲订阅优先搜到所属整专。"""
        artists = music.artists or []
        artist = artists[0] if artists else music.album_artist
        keywords = []
        if getattr(music, "music_type", None) == MUSIC_ENTITY_ALBUM:
            album = music.album or music.title
            if artist and album:
                keywords.append(f"{artist} {album}")
            if album:
                keywords.append(album)
        else:
            if artist and music.title:
                keywords.append(f"{artist} {music.title}")
            if music.title:
                keywords.append(music.title)
        return cls._unique_texts(keywords)

    @classmethod
    def matches_site_resource(
            cls,
            music: MusicInfo,
            resource_title: str,
            resource_description: Optional[str] = None,
    ) -> bool:
        """判断站点资源标题与副标题是否包含订阅目标，避免串专辑或串单曲。"""
        resource_text = f"{resource_title or ''} {resource_description or ''}"
        normalized_resource = cls._normalize_match_text(resource_text)
        if not normalized_resource:
            return False
        if music.music_type == MUSIC_ENTITY_ALBUM:
            candidates = cls._unique_texts([
                music.album or music.title,
                *(music.names or []),
            ])
        else:
            # Recording 的 names 兼容字段会包含所属专辑名；单曲匹配只能使用曲名，
            # 否则整专资源会被当成单曲下载并在首个任务后误销订阅。
            candidates = cls._unique_texts([music.title])
        title_matches = any(
            normalized_target and normalized_target in normalized_resource
            for normalized_target in (
                cls._normalize_match_text(candidate) for candidate in candidates
            )
        )
        if not title_matches:
            return False
        artists = cls._unique_texts([
            music.artist,
            music.album_artist,
            *(music.artists or []),
        ])
        if not artists:
            return True
        # 同名歌曲和专辑十分常见，已知艺术家时必须同时出现在资源标题中。
        return any(
            normalized_artist and normalized_artist in normalized_resource
            for normalized_artist in (
                cls._normalize_match_text(artist) for artist in artists
            )
        )

    @classmethod
    def normalize_candidates(
            cls,
            candidates: Optional[Iterable[MusicInfo | dict[str, Any]]],
            limit: Optional[int] = None,
    ) -> list[MusicInfo]:
        """标准化并去重来自一个或多个音乐元数据模块的候选。"""
        results: list[MusicInfo] = []
        identities: set[tuple[str, ...]] = set()
        for candidate in candidates or []:
            info = candidate if isinstance(candidate, MusicInfo) else MusicInfo.from_dict(candidate)
            identity = cls._candidate_identity(info)
            if identity in identities:
                continue
            identities.add(identity)
            results.append(info)
            if limit and len(results) >= limit:
                break
        return results

    def search(
            self,
            query: str,
            limit: int = 20,
            media_source: Optional[str] = None,
    ) -> list[MusicInfo]:
        """按请求来源调用音乐元数据模块搜索候选，未指定时默认使用 MusicBrainz。"""
        meta = self.parse_query(query)
        candidates = self.run_module(
            "search_music",
            meta=meta,
            limit=limit,
            media_source=media_source or "musicbrainz",
        )
        return self.normalize_candidates(candidates, limit=limit)

    def recognize_best(
            self,
            meta: MetaMusic,
            cache: bool = True,
    ) -> Optional[MusicInfo]:
        """执行自动音乐识别，仅调用 MusicBrainz 主数据源。"""
        with fresh(not cache):
            return self.recognize_from_source(
                media_source=self._primary_recognize_source,
                meta=meta,
                cache=cache,
                music_type=MUSIC_ENTITY_RECORDING,
            )

    async def async_recognize_best(
            self,
            meta: MetaMusic,
            cache: bool = True,
    ) -> Optional[MusicInfo]:
        """异步执行自动音乐识别，仅调用 MusicBrainz 主数据源。"""
        async with async_fresh(not cache):
            return await self.async_recognize_from_source(
                media_source=self._primary_recognize_source,
                meta=meta,
                cache=cache,
                music_type=MUSIC_ENTITY_RECORDING,
            )

    def recognize_from_source(
            self,
            media_source: str,
            meta: Optional[MetaMusic] = None,
            media_id: Optional[str] = None,
            cache: bool = True,
            music_type: Optional[str] = None,
    ) -> Optional[MusicInfo]:
        """只调用指定音乐数据源识别指定实体，拒绝影视、未知来源和跨实体结果。"""
        normalized_source = normalize_media_source(media_source)
        if not is_music_media_source(normalized_source):
            return None
        normalized_music_type = normalize_music_type(
            music_type, allow_artist=False
        )
        if music_type is not None and not normalized_music_type:
            return None
        result = self._recognize_from_source(
            meta=meta,
            media_source=normalized_source,
            cache=cache,
            media_id=media_id,
            music_type=normalized_music_type,
        )
        return self._validate_source_recognize_result(
            result=result,
            media_source=normalized_source,
            media_id=media_id,
            music_type=normalized_music_type,
        )

    async def async_recognize_from_source(
            self,
            media_source: str,
            meta: Optional[MetaMusic] = None,
            media_id: Optional[str] = None,
            cache: bool = True,
            music_type: Optional[str] = None,
    ) -> Optional[MusicInfo]:
        """异步只调用指定音乐数据源识别指定实体，拒绝影视、未知来源和跨实体结果。"""
        normalized_source = normalize_media_source(media_source)
        if not is_music_media_source(normalized_source):
            return None
        normalized_music_type = normalize_music_type(
            music_type, allow_artist=False
        )
        if music_type is not None and not normalized_music_type:
            return None
        result = await self._async_recognize_from_source(
            meta=meta,
            media_source=normalized_source,
            cache=cache,
            media_id=media_id,
            music_type=normalized_music_type,
        )
        return self._validate_source_recognize_result(
            result=result,
            media_source=normalized_source,
            media_id=media_id,
            music_type=normalized_music_type,
        )

    async def async_search(
            self,
            query: str,
            limit: int = 20,
            media_source: Optional[str] = None,
    ) -> list[MusicInfo]:
        """异步按请求来源搜索音乐候选，未指定时默认使用 MusicBrainz。"""
        meta = self.parse_query(query)
        candidates = await self.async_run_module(
            "search_music",
            meta=meta,
            limit=limit,
            media_source=media_source or "musicbrainz",
        )
        return self.normalize_candidates(candidates, limit=limit)

    def chart(self, range_name: str, page: int = 1, count: int = 30) -> list[MusicInfo]:
        """读取 ListenBrainz 全站音乐榜单并标准化分页结果。"""
        candidates = self.run_module(
            "music_chart",
            range_name=range_name,
            offset=max(page - 1, 0) * count,
            count=count,
        )
        return self.normalize_candidates(candidates, limit=count)

    async def async_chart(
            self,
            range_name: str,
            page: int = 1,
            count: int = 30,
            sort_by: str = "listen_count.desc",
            min_listen_count: int = 0,
            with_cover: bool = False,
            entity: str = MUSIC_ENTITY_RECORDING,
    ) -> list[MusicInfo]:
        """异步读取 ListenBrainz 热门榜单，并应用音乐探索筛选和排序。"""
        candidates = await self.async_run_module(
            "music_chart",
            range_name=range_name,
            offset=max(page - 1, 0) * count,
            count=count,
            entity=entity,
        )
        results = self._filter_candidates(
            self.normalize_candidates(candidates),
            min_listen_count=min_listen_count,
            with_cover=with_cover,
        )
        results.sort(
            key=lambda info: info.listen_count or 0,
            reverse=sort_by != "listen_count.asc",
        )
        return results[:count]

    async def async_fresh_releases(
            self,
            days: int = 14,
            sort: str = "release_date",
            past: bool = True,
            future: bool = True,
            page: int = 1,
            count: int = 30,
            with_cover: bool = False,
    ) -> list[MusicInfo]:
        """异步读取 ListenBrainz 官方新发行专辑，排序由官方接口决定。"""
        candidates = await self.async_run_module(
            "music_fresh_releases",
            days=days,
            sort=sort,
            past=past,
            future=future,
            offset=max(page - 1, 0) * count,
            count=count,
        )
        results = self._filter_candidates(
            self.normalize_candidates(candidates),
            min_listen_count=0,
            with_cover=with_cover,
        )
        return results[:count]

    def discover(
            self,
            media_source: str,
            page: int = 1,
            count: int = 30,
            entity: str = MUSIC_ENTITY_ALBUM,
            mode: str = "chart",
            tags: str = "",
            sort: str = "U",
    ) -> list[MusicInfo]:
        """按指定音乐源读取推荐榜单，并统一分页候选结构。"""
        candidates = self.run_module(
            "music_discover",
            media_source=media_source,
            page=page,
            count=count,
            entity=entity,
            mode=mode,
            tags=tags,
            sort=sort,
        )
        return self.normalize_candidates(candidates, limit=count)

    async def async_discover(
            self,
            media_source: str,
            page: int = 1,
            count: int = 30,
            entity: str = MUSIC_ENTITY_ALBUM,
            mode: str = "chart",
            tags: str = "",
            sort: str = "U",
    ) -> list[MusicInfo]:
        """异步按指定音乐源读取推荐榜单，并统一分页候选结构。"""
        candidates = await self.async_run_module(
            "music_discover",
            media_source=media_source,
            page=page,
            count=count,
            entity=entity,
            mode=mode,
            tags=tags,
            sort=sort,
        )
        return self.normalize_candidates(candidates, limit=count)

    async def async_album(self, media_source: str, media_id: str) -> Optional[MusicAlbumInfo]:
        """异步按来源和专辑 ID 获取标准化专辑详情及曲目。"""
        result = await self.async_run_module(
            "music_album",
            media_source=media_source,
            media_id=media_id,
        )
        if isinstance(result, MusicAlbumInfo):
            return result
        if isinstance(result, dict):
            return MusicAlbumInfo.from_dict(result)
        return None

    def album(self, media_source: str, media_id: str) -> Optional[MusicAlbumInfo]:
        """同步按来源和专辑 ID 获取标准化专辑详情及曲目。"""
        result = self.run_module(
            "music_album",
            media_source=media_source,
            media_id=media_id,
        )
        if isinstance(result, MusicAlbumInfo):
            return result
        if isinstance(result, dict):
            return MusicAlbumInfo.from_dict(result)
        return None

    async def async_album_related(
            self,
            media_source: str,
            media_id: str,
            count: int = 24,
    ) -> list[MusicInfo]:
        """异步读取指定来源的关联专辑，供专辑详情继续浏览。"""
        candidates = await self.async_run_module(
            "music_album_related",
            media_source=media_source,
            media_id=media_id,
            count=count,
        )
        return self.normalize_candidates(candidates, limit=count)

    def lyrics(self, music: MetaMusic | MusicInfo) -> Optional[MusicLyrics]:
        """按单曲元数据调用已启用的歌词模块并返回标准歌词。"""
        result = self.run_module("music_lyrics", music=music)
        if isinstance(result, MusicLyrics):
            return result
        if isinstance(result, dict):
            return MusicLyrics.from_dict(result)
        return None

    async def async_artist(self, media_source: str, media_id: str) -> Optional[MusicArtistInfo]:
        """异步按来源和艺术家 ID 获取标准化艺术家详情。"""
        result = await self.async_run_module(
            "music_artist",
            media_source=media_source,
            media_id=media_id,
        )
        if isinstance(result, MusicArtistInfo):
            return result
        if isinstance(result, dict):
            return MusicArtistInfo.from_dict(result)
        return None

    async def async_artist_albums(
            self,
            media_source: str,
            media_id: str,
            page: int = 1,
            count: int = 30,
            album_type: Optional[str] = None,
    ) -> list[MusicInfo]:
        """异步分页读取艺术家名下的专辑、EP 和单曲。"""
        candidates = await self.async_run_module(
            "music_artist_albums",
            media_source=media_source,
            media_id=media_id,
            page=page,
            count=count,
            album_type=album_type,
        )
        return self.normalize_candidates(candidates, limit=count)

    async def async_artist_related(
            self,
            media_source: str,
            media_id: str,
            count: int = 24,
    ) -> list[MusicArtistInfo]:
        """异步读取关联艺术家，供详情页继续浏览。"""
        candidates = await self.async_run_module(
            "music_artist_related",
            media_source=media_source,
            media_id=media_id,
            count=count,
        )
        results: list[MusicArtistInfo] = []
        identities: set[str] = set()
        for candidate in candidates or []:
            info = (
                candidate
                if isinstance(candidate, MusicArtistInfo)
                else MusicArtistInfo.from_dict(candidate)
            )
            identity = (info.media_id or info.name or "").casefold()
            if not identity or identity in identities:
                continue
            identities.add(identity)
            results.append(info)
        return results[:count]

    @staticmethod
    def _filter_candidates(
            candidates: list[MusicInfo],
            min_listen_count: int,
            with_cover: bool,
    ) -> list[MusicInfo]:
        """按热度和封面条件过滤音乐探索候选。"""
        results = candidates
        if min_listen_count > 0:
            results = [
                info for info in results
                if (info.listen_count or 0) >= min_listen_count
            ]
        if with_cover:
            results = [info for info in results if info.cover_url]
        return list(results)

    @staticmethod
    def _normalize_match_text(value: Optional[str]) -> str:
        """移除大小写、空白和标点差异，生成站点标题匹配使用的紧凑文本。"""
        return MetaMusic.compact_text(value)

    @staticmethod
    def _validate_source_recognize_result(
            result: Optional[MusicInfo],
            media_source: str,
            media_id: Optional[str],
            music_type: Optional[str],
    ) -> Optional[MusicInfo]:
        """校验指定来源的识别结果，显式 ID 不允许被另一实体或另一身份替代。"""
        if not isinstance(result, MusicInfo):
            return None
        if result.media_source and result.media_source != media_source:
            return None
        if music_type and result.music_type != music_type:
            return None
        if media_id and (
                not result.media_source
                or not result.media_id
                or str(result.media_id) != str(media_id)
        ):
            return None
        return result

    def _recognize_from_source(
            self,
            meta: Optional[MetaMusic],
            media_source: str,
            cache: bool,
            media_id: Optional[str] = None,
            music_type: Optional[str] = None,
    ) -> Optional[MusicInfo]:
        """调用声明了指定音乐来源的系统模块，隔离单个来源的查询失败。"""
        module = self._music_recognize_module(media_source)
        if not module:
            return None
        try:
            recognize_kwargs = {
                "meta": meta,
                "mtype": MediaType.MUSIC,
                "media_source": media_source,
                "media_id": media_id,
                "cache": cache,
            }
            if music_type is not None:
                recognize_kwargs["music_type"] = music_type
            return module.recognize_media(
                **recognize_kwargs,
            )
        except Exception as err:
            logger.warning(f"{media_source} 音乐自动识别失败：{err}")
            return None

    async def _async_recognize_from_source(
            self,
            meta: Optional[MetaMusic],
            media_source: str,
            cache: bool,
            media_id: Optional[str] = None,
            music_type: Optional[str] = None,
    ) -> Optional[MusicInfo]:
        """异步调用指定音乐来源模块，单个来源失败不影响其它候选。"""
        module = self._music_recognize_module(media_source)
        if not module:
            return None
        try:
            recognize_kwargs = {
                "meta": meta,
                "mtype": MediaType.MUSIC,
                "media_source": media_source,
                "media_id": media_id,
                "cache": cache,
            }
            if music_type is not None:
                recognize_kwargs["music_type"] = music_type
            return await module.async_recognize_media(**recognize_kwargs)
        except Exception as err:
            logger.warning(f"{media_source} 音乐自动识别失败：{err}")
            return None

    def _music_recognize_module(self, media_source: str) -> Optional[Any]:
        """枚举运行中的系统模块并返回声明了指定音乐来源的实现。"""
        for module in self.modulemanager.get_running_modules("recognize_media"):
            get_music_source = getattr(module, "get_music_source", None)
            if get_music_source and get_music_source() == media_source:
                return module
        return None

    def recognize_album_directory(self, path: str | Path) -> dict[str, MusicInfo]:
        """按目录级线索批量识别整目录音频，返回 文件路径 到标准音乐信息的映射。
    
        适用于 WAV 无标签或标签不全的整专目录：先用目录名和文件标签构造专辑线索，
        再交给音乐元数据模块用曲目数、时长等特征对位到具体发行版本。
        """
        dir_path = Path(path)
        if not dir_path.is_dir():
            return {}
        files = self._directory_audio_files(dir_path)
        if len(files) < self._album_match_min_files:
            return {}
        cache_key = str(dir_path)
        signature = self._album_directory_signature(dir_path, files)
        cached = self._album_dir_cache.get(cache_key)
        # 新增、删除或重命名音频时重新匹配；标签写回不会改变相对路径签名。
        if cached and cached[0] == signature:
            return cached[1]
        matched = self._match_album_directory(dir_path, files)
        if len(self._album_dir_cache) >= self._album_dir_cache_max:
            self._album_dir_cache.clear()
        self._album_dir_cache[cache_key] = (signature, matched)
        return matched

    async def async_recognize_album_directory(self, path: str | Path) -> dict[str, MusicInfo]:
        """异步按目录级线索批量识别整目录音频。"""
        dir_path = Path(path)
        if not dir_path.is_dir():
            return {}
        files = await run_in_threadpool(self._directory_audio_files, dir_path)
        if len(files) < self._album_match_min_files:
            return {}
        cache_key = str(dir_path)
        signature = self._album_directory_signature(dir_path, files)
        cached = self._album_dir_cache.get(cache_key)
        if cached and cached[0] == signature:
            return cached[1]
        matched = await self._async_match_album_directory(dir_path, files)
        if len(self._album_dir_cache) >= self._album_dir_cache_max:
            self._album_dir_cache.clear()
        self._album_dir_cache[cache_key] = (signature, matched)
        return matched

    @classmethod
    def _directory_audio_files(cls, dir_path: Path) -> list[Path]:
        """收集目录及其一级子目录（如 CD1/CD2）内的音频文件。"""
        audio_exts = settings.RMT_AUDIOEXT
        files: list[Path] = []

        def collect(current: Path) -> None:
            try:
                entries = sorted(current.iterdir())
            except OSError:
                return
            for entry in entries:
                if entry.name.startswith("."):
                    continue
                if entry.is_file() and entry.suffix.lower() in audio_exts:
                    files.append(entry)

        collect(dir_path)
        try:
            subdirs = sorted(entry for entry in dir_path.iterdir()
                             if entry.is_dir() and not entry.name.startswith("."))
        except OSError:
            subdirs = []
        for subdir in subdirs:
            collect(subdir)
        return files

    @staticmethod
    def _album_directory_signature(dir_path: Path, files: list[Path]) -> tuple[str, ...]:
        """按相对文件路径生成专辑目录缓存签名，兼容多碟子目录。"""
        return tuple(
            str(file.relative_to(dir_path)).casefold()
            for file in files
        )

    @classmethod
    def read_path_meta(cls, path: Union[str, Path]) -> MetaMusic:
        """读取本地音频标签，标签缺失时用文件名和目录线索补齐。"""
        file_path = Path(path)
        if file_path.exists() and file_path.is_file():
            return AudioMetadataHelper.read(file_path)
        return AudioMetadataHelper.read_filename(file_path)

    @classmethod
    def read_path_evidence(
            cls,
            path: Union[str, Path],
    ) -> tuple[MetaMusic, Optional[MetaMusic], MetaMusic]:
        """分别返回合并元数据、纯标签元数据和纯文件名元数据。"""
        file_path = Path(path)
        filename_meta = AudioMetadataHelper.read_filename(file_path)
        tag_meta = None
        if file_path.exists() and file_path.is_file():
            tag_meta = AudioMetadataHelper.read_tags(file_path)
        if not tag_meta:
            return filename_meta, None, filename_meta
        merged_meta = MetaMusic.from_dict(tag_meta.to_dict()).apply_path_context(file_path)
        return merged_meta, tag_meta, filename_meta

    def identify_by_fingerprint(self, path: Union[str, Path]) -> Optional[str]:
        """调用音频指纹模块识别 MusicBrainz Recording ID。"""
        result = self.run_module(
            "identify_music_by_fingerprint",
            path=Path(path),
        )
        return str(result) if result else None

    async def async_identify_by_fingerprint(
            self,
            path: Union[str, Path],
    ) -> Optional[str]:
        """异步调用音频指纹模块识别 MusicBrainz Recording ID。"""
        result = await self.async_run_module(
            "async_identify_music_by_fingerprint",
            path=Path(path),
        )
        return str(result) if result else None

    def _match_album_directory(
            self,
            dir_path: Path,
            files: list[Path],
    ) -> dict[str, MusicInfo]:
        """执行目录级专辑匹配，并把专辑曲目对位到具体音频文件。"""
        # 标签读取属于音乐领域；MediaChain 只负责编排识别、刮削等跨领域流程。
        metas = [self.read_path_meta(file) for file in files]
        album_meta = self._album_meta_from_context(dir_path, metas)
        if not (album_meta.album or album_meta.title or album_meta.artists):
            logger.debug(f"目录缺少专辑识别线索，跳过专辑匹配：{dir_path}")
            return {}
        candidates = self.run_module("match_music_album", meta=album_meta, tracks=metas)
        candidate_items = candidates if isinstance(candidates, list) else [candidates]
        album = next(
            (item for item in candidate_items if isinstance(item, MusicAlbumInfo) and item.tracks),
            None,
        )
        if not album:
            return {}
        logger.info(f"目录 {dir_path.name} 匹配到专辑：{album.title_year}（{album.media_source}）")
        matched: dict[str, MusicInfo] = {}
        for file, info in self._align_album_tracks(files, metas, album.tracks).items():
            matched[str(file.resolve())] = info
        return matched

    async def _async_match_album_directory(
            self,
            dir_path: Path,
            files: list[Path],
    ) -> dict[str, MusicInfo]:
        """异步执行目录级专辑匹配，本地标签读取保持在线程池中。"""
        metas = await run_in_threadpool(self._read_album_path_metas, files)
        album_meta = self._album_meta_from_context(dir_path, metas)
        if not (album_meta.album or album_meta.title or album_meta.artists):
            logger.debug(f"目录缺少专辑识别线索，跳过专辑匹配：{dir_path}")
            return {}
        candidates = await self.async_run_module(
            "async_match_music_album",
            meta=album_meta,
            tracks=metas,
        )
        candidate_items = candidates if isinstance(candidates, list) else [candidates]
        album = next(
            (item for item in candidate_items if isinstance(item, MusicAlbumInfo) and item.tracks),
            None,
        )
        if not album:
            return {}
        logger.info(f"目录 {dir_path.name} 匹配到专辑：{album.title_year}（{album.media_source}）")
        matched: dict[str, MusicInfo] = {}
        for file, info in self._align_album_tracks(files, metas, album.tracks).items():
            matched[str(file.resolve())] = info
        return matched

    @classmethod
    def _read_album_path_metas(cls, files: list[Path]) -> list[MetaMusic]:
        """批量读取专辑目录中的本地音频元数据。"""
        return [cls.read_path_meta(file) for file in files]

    @classmethod
    def _album_meta_from_context(cls, dir_path: Path, metas: list[MetaMusic]) -> MetaMusic:
        """汇总目录名和文件标签中的专辑线索，作为专辑搜索条件。"""
        dir_info = MetaMusic.parse_album_dir(dir_path.name)
        # 文件标签中的专辑信息比目录名更可靠，多数文件一致时优先采用
        album_votes: dict[str, int] = {}
        artist_votes: dict[str, int] = {}
        for meta in metas:
            if meta.album:
                album_votes[meta.album] = album_votes.get(meta.album, 0) + 1
            if meta.album_artist:
                artist_votes[meta.album_artist] = artist_votes.get(meta.album_artist, 0) + 1
            elif meta.artists:
                artist_votes[meta.artists[0]] = artist_votes.get(meta.artists[0], 0) + 1
        majority_album = max(album_votes, key=album_votes.get) if album_votes else None
        majority_artist = max(artist_votes, key=artist_votes.get) if artist_votes else None
        # 多数文件共享同一专辑标签才可信，避免杂集目录的个别错误标签带偏搜索
        album = majority_album if majority_album and album_votes[majority_album] >= max(2, len(metas) // 2) else None
        artist = majority_artist if majority_artist and artist_votes[majority_artist] >= max(2,
                                                                                             len(metas) // 2) else None
        return MetaMusic(
            org_string=dir_path.name,
            title=album or dir_info.get("album") or dir_path.name,
            album=album or dir_info.get("album"),
            artists=[artist or dir_info.get("artist")] if (artist or dir_info.get("artist")) else [],
            album_artist=artist or dir_info.get("artist"),
            year=dir_info.get("year"),
        )

    @classmethod
    def _align_album_tracks(
            cls,
            files: list[Path],
            metas: list[MetaMusic],
            tracks: list[MusicInfo],
    ) -> dict[Path, MusicInfo]:
        """把专辑曲目对位到目录内的音频文件。
    
        带曲序标签的文件按（碟号, 曲序）精确对位，其余文件按排序顺序依次补齐。
        """
        matched: dict[Path, MusicInfo] = {}
        used_keys: set[tuple[int, int]] = set()
        by_position: dict[tuple[int, int], MusicInfo] = {}
        for track in tracks:
            if track.track_number:
                by_position[(track.disc_number or 1, track.track_number)] = track
        pending: list[tuple[Path, MetaMusic]] = []
        for file, meta in zip(files, metas):
            key = (meta.disc_number or 1, meta.track_number)
            track = by_position.get(key) if meta.track_number else None
            if track and key not in used_keys:
                matched[file] = track
                used_keys.add(key)
            else:
                pending.append((file, meta))
        if not pending:
            return matched
        remaining = [
            track for track in tracks
            if (track.disc_number or 1, track.track_number or 0) not in used_keys
        ]
        # 无曲序线索的文件按碟号和文件名排序，与剩余曲目顺序对位
        pending.sort(key=lambda item: (item[1].disc_number or 1, item[0].name.casefold()))
        for (file, _meta), track in zip(pending, remaining):
            matched[file] = track
        return matched

    @classmethod
    def to_meta(cls, info: MusicInfo) -> MetaMusic:
        """将用户选中的标准音乐信息转换为下载和整理上下文元数据。"""
        return MetaMusic(
            title=info.title,
            artists=list(info.artists),
            album=info.album,
            album_artist=info.album_artist,
            year=info.year,
            disc_number=info.disc_number,
            track_number=info.track_number,
            total_tracks=info.total_tracks,
            version=info.version,
            audio_format=info.audio_format,
            audio_lossless=info.audio_lossless,
            bit_depth=info.bit_depth,
            sample_rate=info.sample_rate,
            bitrate=info.bitrate,
            duration=info.duration,
            isrc=info.isrc,
            media_source=info.media_source,
            media_id=info.media_id,
        )

    @classmethod
    def _candidate_identity(cls, info: MusicInfo) -> tuple[str, ...]:
        """构造跨来源稳定的候选去重键。"""
        if info.media_source and info.media_id:
            return "id", info.media_source.casefold(), info.music_type.casefold(), info.media_id.casefold()
        return (
            "metadata",
            info.music_type.casefold(),
            cls._normalize_text(info.title).casefold(),
            cls._normalize_text(info.artist).casefold(),
            cls._normalize_text(info.album).casefold(),
        )

    @classmethod
    def _unique_texts(cls, values: Iterable[Optional[str]]) -> list[str]:
        """按规范化文本去重并保留原始顺序。"""
        results = []
        seen = set()
        for value in values:
            normalized = cls._normalize_text(value)
            identity = normalized.casefold()
            if not normalized or identity in seen:
                continue
            seen.add(identity)
            results.append(normalized)
        return results

    @classmethod
    def _normalize_text(cls, value: Optional[str]) -> str:
        """清理音乐检索文本中的多余空白。"""
        return re.sub(r"\s+", " ", str(value or "")).strip()
