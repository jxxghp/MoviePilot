import re
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Iterable, Optional

from app import schemas
from app.chain import ChainBase
from app.chain.storage import StorageChain
from app.core.config import settings
from app.core.music import MusicInfo, MusicMeta
from app.helper.audio import AudioMetadataHelper
from app.log import logger
from app.utils.http import RequestUtils


class MusicChain(ChainBase):
    """音乐元数据搜索、识别与站点搜索参数编排链。"""

    _artist_title_pattern = re.compile(r"^\s*(?P<artist>.+?)\s+[-–—]\s+(?P<title>.+?)\s*$")
    _spaces_pattern = re.compile(r"\s+")

    @classmethod
    def parse_query(cls, query: str) -> MusicMeta:
        """将用户输入解析为最小可用的音乐搜索元数据。"""
        normalized = cls._normalize_text(query)
        meta = MusicMeta(org_string=query, title=normalized)
        match = cls._artist_title_pattern.match(normalized)
        if match:
            meta.artists = [match.group("artist").strip()]
            meta.title = match.group("title").strip()
        return meta

    @classmethod
    def build_site_keywords(cls, music: MusicMeta | MusicInfo) -> list[str]:
        """根据音乐元数据生成按精确度递减的站点搜索关键词。"""
        artists = music.artists or []
        artist = artists[0] if artists else music.album_artist
        keywords = []
        if artist and music.album:
            keywords.append(f"{artist} {music.album}")
        if artist and music.title:
            keywords.append(f"{artist} {music.title}")
        if music.album:
            keywords.append(music.album)
        if music.title:
            keywords.append(music.title)
        return cls._unique_texts(keywords)

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

    def search(self, query: str, limit: int = 20) -> list[MusicInfo]:
        """调用已启用的音乐元数据模块搜索候选。"""
        meta = self.parse_query(query)
        candidates = self.run_module("search_music", meta=meta, limit=limit)
        return self.normalize_candidates(candidates, limit=limit)

    async def async_search(self, query: str, limit: int = 20) -> list[MusicInfo]:
        """异步调用已启用的音乐元数据模块搜索候选。"""
        meta = self.parse_query(query)
        candidates = await self.async_run_module("search_music", meta=meta, limit=limit)
        return self.normalize_candidates(candidates, limit=limit)

    def recognize(
            self,
            source: str,
            media_id: str,
    ) -> Optional[MusicInfo]:
        """按音乐元数据源和媒体 ID 获取标准化详情。"""
        result = self.run_module(
            "recognize_music",
            source=source,
            media_id=media_id,
        )
        if isinstance(result, MusicInfo):
            return result
        if isinstance(result, dict):
            return MusicInfo.from_dict(result)
        return None

    async def async_recognize(
            self,
            source: str,
            media_id: str,
    ) -> Optional[MusicInfo]:
        """异步按音乐元数据源和媒体 ID 获取标准化详情。"""
        result = await self.async_run_module(
            "recognize_music",
            source=source,
            media_id=media_id,
        )
        if isinstance(result, MusicInfo):
            return result
        if isinstance(result, dict):
            return MusicInfo.from_dict(result)
        return None

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
    ) -> list[MusicInfo]:
        """异步读取 ListenBrainz 榜单，并应用音乐探索筛选和排序。"""
        candidates = await self.async_run_module(
            "music_chart",
            range_name=range_name,
            offset=max(page - 1, 0) * count,
            count=count,
        )
        results = self.normalize_candidates(candidates)
        if min_listen_count > 0:
            results = [
                info for info in results
                if (info.listen_count or 0) >= min_listen_count
            ]
        if with_cover:
            results = [info for info in results if info.cover_url]
        results.sort(
            key=lambda info: info.listen_count or 0,
            reverse=sort_by != "listen_count.asc",
        )
        return results[:count]

    @classmethod
    def is_audio_path(cls, path: str | Path) -> bool:
        """判断路径是否指向系统支持的音频文件。"""
        return Path(path).suffix.lower() in settings.RMT_AUDIOEXT

    @classmethod
    def read_path_meta(cls, path: str | Path) -> MusicMeta:
        """读取本地音频标签，不可访问时按文件名构造最小音乐元数据。"""
        file_path = Path(path)
        if file_path.exists() and file_path.is_file():
            return AudioMetadataHelper.read(file_path)
        return cls.parse_query(file_path.stem)

    async def async_recognize_by_path(
            self,
            path: str | Path,
            source: str = "musicbrainz",
    ) -> tuple[MusicMeta, MusicInfo]:
        """根据音频标签和文件名识别音乐，远端不可用时仍返回最小音乐信息。"""
        meta = self.read_path_meta(path)
        candidates = await self.async_run_module(
            "search_music",
            meta=meta,
            limit=10,
        )
        results = self.normalize_candidates(candidates, limit=10)
        matched = self._select_path_candidate(meta, results, source=source)
        if matched:
            return meta, matched
        return meta, self._info_from_meta(meta)

    def recognize_by_path(
            self,
            path: str | Path,
            source: str = "musicbrainz",
    ) -> tuple[MusicMeta, MusicInfo]:
        """同步根据音频标签和文件名识别音乐，并保留离线最小结果。"""
        meta = self.read_path_meta(path)
        candidates = self.run_module("search_music", meta=meta, limit=10)
        results = self.normalize_candidates(candidates, limit=10)
        matched = self._select_path_candidate(meta, results, source=source)
        return meta, matched or self._info_from_meta(meta)

    def scrape_metadata(
            self,
            fileitem: schemas.FileItem,
            mediainfo: Optional[MusicInfo] = None,
            overwrite: bool = True,
    ) -> tuple[bool, str]:
        """为音频文件或目录写入音乐标签和封面，复用现有存储下载上传能力。"""
        files = self._audio_fileitems(fileitem)
        if not files:
            return False, "刮削路径中没有支持的音频文件"
        if mediainfo and len(files) > 1:
            return False, "指定 MusicBrainz ID 时仅支持刮削单个音频文件"

        failures: list[str] = []
        for audio_item in files:
            info = mediainfo
            if not info:
                _, info = self.recognize_by_path(audio_item.path)
            if not info or not info.title:
                failures.append(f"{audio_item.name or audio_item.path} 无法识别音乐信息")
                continue
            if not self._scrape_audio_file(audio_item, info, overwrite=overwrite):
                failures.append(f"{audio_item.name or audio_item.path} 标签写入失败")
        if failures:
            return False, "；".join(failures[:3])
        return True, f"已刮削 {len(files)} 个音频文件"

    @staticmethod
    def _download_cover(url: Optional[str]) -> tuple[Optional[bytes], str]:
        """通过统一请求封装下载封面，并返回图片内容与 MIME 类型。"""
        if not url:
            return None, "image/jpeg"
        response = RequestUtils(
            proxies=settings.PROXY,
            ua=settings.NORMAL_USER_AGENT,
            timeout=20,
        ).get_res(url)
        if not response:
            return None, "image/jpeg"
        try:
            if response.status_code != 200:
                logger.warning(f"音乐封面下载失败：{response.status_code} {url}")
                return None, "image/jpeg"
            mime = (response.headers.get("Content-Type") or "image/jpeg").split(";", 1)[0]
            return response.content, mime
        finally:
            response.close()

    def _audio_fileitems(self, fileitem: schemas.FileItem) -> list[schemas.FileItem]:
        """展开待刮削目录并过滤系统支持的音频文件。"""
        if fileitem.type != "dir":
            return [fileitem] if self.is_audio_path(fileitem.path or "") else []
        return [
            item
            for item in StorageChain().list_files(fileitem, recursion=True) or []
            if item.type == "file" and self.is_audio_path(item.path or "")
        ]

    def _scrape_audio_file(
            self,
            fileitem: schemas.FileItem,
            mediainfo: MusicInfo,
            overwrite: bool,
    ) -> bool:
        """下载单个音频文件、写入标签，并在远端存储场景上传覆盖原文件。"""
        cover_data, cover_mime = self._download_cover(mediainfo.cover_url)
        storage = StorageChain()
        if fileitem.storage == "local":
            local_path = storage.download_file(fileitem)
            return bool(
                local_path
                and AudioMetadataHelper.write(
                    local_path,
                    mediainfo,
                    cover_data=cover_data,
                    cover_mime=cover_mime,
                    overwrite=overwrite,
                )
            )

        with TemporaryDirectory(prefix="moviepilot-music-scrape-") as temp_dir:
            local_path = storage.download_file(fileitem, path=Path(temp_dir))
            if not local_path or not AudioMetadataHelper.write(
                    local_path,
                    mediainfo,
                    cover_data=cover_data,
                    cover_mime=cover_mime,
                    overwrite=overwrite,
            ):
                return False
            parent = storage.get_parent_item(fileitem)
            if not parent:
                logger.warning(f"无法获取远端音频父目录：{fileitem.path}")
                return False
            return bool(
                storage.upload_file(
                    parent,
                    local_path,
                    new_name=fileitem.name or local_path.name,
                )
            )

    @classmethod
    def to_meta(cls, info: MusicInfo) -> MusicMeta:
        """将用户选中的标准音乐信息转换为下载和整理上下文元数据。"""
        return MusicMeta(
            title=info.title,
            artists=list(info.artists),
            album=info.album,
            album_artist=info.album_artist,
            year=info.year,
            disc_number=info.disc_number,
            track_number=info.track_number,
            total_tracks=info.total_tracks,
            version=info.version,
            duration=info.duration,
            isrc=info.isrc,
            media_source=info.source,
            media_id=info.media_id,
        )

    @classmethod
    def _select_path_candidate(
            cls,
            meta: MusicMeta,
            candidates: Iterable[MusicInfo],
            source: str,
    ) -> Optional[MusicInfo]:
        """按标题、艺术家和专辑匹配度选择最可信的文件识别候选。"""
        normalized_source = cls._normalize_text(source).casefold()
        ranked: list[tuple[int, MusicInfo]] = []
        for candidate in candidates:
            if normalized_source and (candidate.source or "").casefold() != normalized_source:
                continue
            score = 0
            if cls._same_text(meta.title, candidate.title):
                score += 4
            if meta.artists and any(
                    cls._same_text(meta.artists[0], artist)
                    for artist in candidate.artists
            ):
                score += 3
            if meta.album and cls._same_text(meta.album, candidate.album):
                score += 2
            if meta.isrc and cls._same_text(meta.isrc, candidate.isrc):
                score += 5
            ranked.append((score, candidate))
        if not ranked:
            return None
        ranked.sort(key=lambda item: item[0], reverse=True)
        return ranked[0][1] if ranked[0][0] > 0 else None

    @classmethod
    def _info_from_meta(cls, meta: MusicMeta) -> MusicInfo:
        """把音频标签转换为文件管理可展示的最小音乐信息。"""
        return MusicInfo(
            source=meta.media_source,
            media_id=meta.media_id,
            title=meta.title,
            artists=list(meta.artists),
            album=meta.album,
            album_artist=meta.album_artist,
            year=meta.year,
            disc_number=meta.disc_number,
            track_number=meta.track_number,
            total_tracks=meta.total_tracks,
            duration=meta.duration,
            isrc=meta.isrc,
            version=meta.version,
            names=[name for name in (meta.title, meta.album) if name],
        )

    @classmethod
    def _same_text(cls, left: Optional[str], right: Optional[str]) -> bool:
        """忽略空白和大小写比较两个音乐文本字段。"""
        return cls._normalize_text(left).casefold() == cls._normalize_text(right).casefold()

    @classmethod
    def _candidate_identity(cls, info: MusicInfo) -> tuple[str, ...]:
        """构造跨来源稳定的候选去重键。"""
        if info.source and info.media_id:
            return "id", info.source.casefold(), info.media_id.casefold()
        return (
            "metadata",
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
        return cls._spaces_pattern.sub(" ", str(value or "")).strip()
