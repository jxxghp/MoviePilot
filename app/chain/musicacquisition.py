"""艺术家合集优先、缺失作品补齐的统一下载链。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Mapping, Optional
from uuid import uuid4

from app.application.music.acquisition import (
    ArtistAcquisitionRepository,
    ArtistAcquisitionSnapshot,
    ArtistWork,
    CollectionCoverage,
    evaluate_collection_coverage,
    get_artist_acquisition_repository,
)
from app.chain.download import DownloadChain
from app.domain.context import Context, MusicInfo, TorrentInfo
from app.domain.metainfo import MetaInfo
from app.runtime.log import logger
from app.schemas.types import (
    MUSIC_ARTIST_COLLECTION_CATEGORY,
    MUSIC_ENTITY_ARTIST,
    MediaSource,
    MediaType,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_torrent(torrent: Mapping[str, Any]) -> dict[str, Any]:
    """持久化可观测资源身份，不落盘 Cookie、UA 和下载票据。"""
    safe = {
        key: torrent.get(key)
        for key in (
            "site",
            "site_name",
            "title",
            "description",
            "page_url",
            "size",
            "seeders",
            "peers",
            "grabs",
            "pubdate",
        )
        if torrent.get(key) is not None
    }
    if safe.get("description"):
        safe["description"] = str(safe["description"])[:2000]
    return safe


def _safe_work(work: Mapping[str, Any]) -> dict[str, Any]:
    """任务快照只保存补齐与审计需要的官方作品身份。"""
    return {
        key: work.get(key)
        for key in (
            "media_source",
            "media_id",
            "title",
            "original_title",
            "year",
            "album_type",
            "artists",
            "artist_ids",
            "release_date",
        )
        if work.get(key) is not None
    }


class MusicArtistAcquisitionChain:
    """将合集和单作品补齐收口为一个可查询任务。"""

    @staticmethod
    def probe_collection(*, torrent: TorrentInfo, works: tuple[ArtistWork, ...]) -> CollectionCoverage:
        """只下载种子元数据并解析文件清单，不向下载器提交。"""
        _, folder_name, file_list = DownloadChain().download_torrent(torrent, source="ArtistAcquisitionProbe")
        return evaluate_collection_coverage(
            works=works,
            file_paths=tuple(file_list or ()),
            resource_title=torrent.title or "",
            resource_description=torrent.description or "",
            folder_name=folder_name or "",
        )

    @staticmethod
    def _plan_key(
        *,
        username: str,
        artist_source: str,
        artist_id: str,
        collection: Optional[Mapping[str, Any]],
        supplements: tuple[Mapping[str, Any], ...],
        normalize_source: Optional[bool],
        downloader: Optional[str],
        save_path: Optional[str],
    ) -> str:
        identity = {
            "username": username,
            "artist_source": artist_source,
            "artist_id": artist_id,
            "normalize_source": normalize_source,
            "downloader": downloader,
            "save_path": save_path,
            "collection": _safe_torrent(collection.get("torrent", {})) if collection else None,
            "supplements": [
                {
                    "media_id": item.get("media", {}).get("media_id"),
                    "torrent": _safe_torrent(item.get("torrent", {})),
                }
                for item in supplements
            ],
        }
        encoded = json.dumps(identity, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def submit(
        self,
        *,
        username: str,
        artist_source: str,
        artist_id: str,
        artist_name: str,
        works: tuple[Mapping[str, Any], ...],
        collection: Optional[Mapping[str, Any]],
        supplements: tuple[Mapping[str, Any], ...],
        normalize_source: Optional[bool],
        downloader: Optional[str] = None,
        save_path: Optional[str] = None,
    ) -> ArtistAcquisitionSnapshot:
        """防重创建聚合任务，再顺序提交基线合集和缺失补齐。"""
        repository = get_artist_acquisition_repository()
        plan_key = self._plan_key(
            username=username,
            artist_source=artist_source,
            artist_id=artist_id,
            collection=collection,
            supplements=supplements,
            normalize_source=normalize_source,
            downloader=downloader,
            save_path=save_path,
        )
        existing = repository.find_by_plan_key(username=username, plan_key=plan_key)
        if existing is not None:
            return existing

        initial_snapshot = self._build_initial_snapshot(
            plan_key=plan_key,
            username=username,
            artist_source=artist_source,
            artist_id=artist_id,
            artist_name=artist_name,
            works=works,
            collection=collection,
            supplements=supplements,
            normalize_source=normalize_source,
        )
        try:
            snapshot = repository.create(initial_snapshot)
        except Exception:
            concurrent = repository.find_by_plan_key(username=username, plan_key=plan_key)
            if concurrent is not None:
                return concurrent
            raise

        results: list[dict[str, Any]] = []
        if collection:
            result = self._submit_collection(
                collection=collection,
                artist_source=artist_source,
                artist_id=artist_id,
                artist_name=artist_name,
                username=username,
                normalize_source=normalize_source,
                downloader=downloader,
                save_path=save_path,
            )
            snapshot = self._record_result(repository, snapshot, results, result)

        for item in supplements:
            result = self._submit_supplement(
                item=item,
                username=username,
                normalize_source=normalize_source,
                downloader=downloader,
                save_path=save_path,
            )
            snapshot = self._record_result(repository, snapshot, results, result)

        failed = sum(result["state"] == "failed" for result in results)
        state = "failed" if results and failed == len(results) else "partial" if failed else "submitted"
        return repository.update(
            replace(
                snapshot,
                state=state,
                results=results,
                failed_count=failed,
                updated_at=_now(),
                finished_at=_now() if state == "failed" else None,
                last_error=("; ".join(str(item.get("error")) for item in results if item.get("error")) or None),
            )
        )

    @staticmethod
    def _build_initial_snapshot(
        *,
        plan_key: str,
        username: str,
        artist_source: str,
        artist_id: str,
        artist_name: str,
        works: tuple[Mapping[str, Any], ...],
        collection: Optional[Mapping[str, Any]],
        supplements: tuple[Mapping[str, Any], ...],
        normalize_source: Optional[bool],
    ) -> ArtistAcquisitionSnapshot:
        now = _now()
        coverage = (collection or {}).get("coverage") or ()
        public_collection = None
        if collection:
            public_collection = {
                "torrent": _safe_torrent(collection.get("torrent", {})),
                "coverage": list(coverage),
            }
        public_supplements = [
            {
                "media_id": item.get("media", {}).get("media_id"),
                "title": item.get("media", {}).get("title"),
                "album_type": item.get("media", {}).get("album_type"),
                "torrent": _safe_torrent(item.get("torrent", {})),
            }
            for item in supplements
        ]
        return ArtistAcquisitionSnapshot(
            job_id=uuid4().hex,
            plan_key=plan_key,
            username=username,
            artist_source=artist_source,
            artist_id=artist_id,
            artist_name=artist_name,
            state="submitting",
            scope={"works": [_safe_work(item) for item in works]},
            plan={
                "collection": public_collection,
                "supplements": public_supplements,
                "normalize_source": normalize_source,
            },
            total_count=(1 if collection else 0) + len(supplements),
            covered_count=sum(
                item.get("state") == "confirmed" and bool(item.get("media_id"))
                for item in coverage
            ),
            supplement_count=len(supplements),
            created_at=now,
            updated_at=now,
        )

    def _submit_collection(
        self,
        *,
        collection: Mapping[str, Any],
        artist_source: str,
        artist_id: str,
        artist_name: str,
        username: str,
        normalize_source: Optional[bool],
        downloader: Optional[str],
        save_path: Optional[str],
    ) -> dict[str, Any]:
        torrent = TorrentInfo()
        torrent.from_dict(dict(collection.get("torrent", {})))
        if downloader is not None:
            torrent.site_downloader = downloader
        media = MusicInfo(
            media_source=MediaSource(artist_source),
            media_id=artist_id,
            music_type=MUSIC_ENTITY_ARTIST,
            title=f"{artist_name} 艺术家合集",
            artists=[artist_name],
            album_artist=artist_name,
            album_type=MUSIC_ARTIST_COLLECTION_CATEGORY,
            library_category=MUSIC_ARTIST_COLLECTION_CATEGORY,
        )
        return self._submit_one(
            role="collection",
            media_id=artist_id,
            media=media,
            torrent=torrent,
            username=username,
            normalize_source=normalize_source,
            downloader=downloader,
            save_path=save_path,
        )

    def _submit_supplement(
        self,
        *,
        item: Mapping[str, Any],
        username: str,
        normalize_source: Optional[bool],
        downloader: Optional[str],
        save_path: Optional[str],
    ) -> dict[str, Any]:
        media = MusicInfo.from_dict(dict(item.get("media", {})))
        torrent = TorrentInfo()
        torrent.from_dict(dict(item.get("torrent", {})))
        if downloader is not None:
            torrent.site_downloader = downloader
        return self._submit_one(
            role="supplement",
            media_id=str(media.media_id or ""),
            media=media,
            torrent=torrent,
            username=username,
            normalize_source=normalize_source,
            downloader=downloader,
            save_path=save_path,
        )

    @staticmethod
    def _record_result(
        repository: ArtistAcquisitionRepository,
        snapshot: ArtistAcquisitionSnapshot,
        results: list[dict[str, Any]],
        result: dict[str, Any],
    ) -> ArtistAcquisitionSnapshot:
        results.append(result)
        return repository.update(
            replace(
                snapshot,
                results=list(results),
                failed_count=sum(item["state"] == "failed" for item in results),
                updated_at=_now(),
            )
        )

    @staticmethod
    def _submit_one(
        *,
        role: str,
        media_id: str,
        media: MusicInfo,
        torrent: TorrentInfo,
        username: str,
        normalize_source: Optional[bool],
        downloader: Optional[str],
        save_path: Optional[str],
    ) -> dict[str, Any]:
        meta = MetaInfo(
            title=torrent.title or "",
            subtitle=torrent.description,
            mtype=MediaType.MUSIC,
        )
        context = Context(meta_info=meta, media_info=media, torrent_info=torrent)
        try:
            download_id = DownloadChain().download_single(
                context=context,
                username=username,
                save_path=save_path,
                source="ArtistAcquisition",
                downloader=downloader,
                normalize_source=normalize_source,
            )
        except Exception as err:  # 外部下载器边界必须收口为子任务终态
            logger.error(f"艺术家作品资源提交失败：{torrent.title} - {err}")
            return {
                "role": role,
                "media_id": media_id,
                "state": "failed",
                "error": str(err),
            }
        if not download_id:
            return {"role": role, "media_id": media_id, "state": "failed", "error": "下载器拒绝任务"}
        return {
            "role": role,
            "media_id": media_id,
            "state": "submitted",
            "download_id": str(download_id),
        }

    @staticmethod
    def get(job_id: str) -> ArtistAcquisitionSnapshot | None:
        return get_artist_acquisition_repository().get(job_id)
