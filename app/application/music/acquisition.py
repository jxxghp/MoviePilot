"""艺术家作品获取计划与持久化端口。"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Optional, Protocol

CoverageState = Literal["confirmed", "probable", "missing"]


@dataclass(frozen=True, slots=True)
class ArtistWork:
    """MusicBrainz 官方 release-group 的最小匹配投影。"""

    media_id: str
    title: str
    year: Optional[int] = None
    album_type: str = "Album"
    title_aliases: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class WorkCoverage:
    """某官方作品在合集种子中的可解释覆盖结果。"""

    media_id: str
    state: CoverageState
    evidence: str = ""


@dataclass(frozen=True, slots=True)
class CollectionCoverage:
    """合集资源的覆盖摘要。"""

    folder_name: str
    file_count: int
    confirmed_count: int
    probable_count: int
    missing_count: int
    works: tuple[WorkCoverage, ...]


@dataclass(frozen=True, slots=True)
class ArtistAcquisitionSnapshot:
    """一次艺术家完整作品获取任务快照。"""

    job_id: str
    plan_key: str
    username: str
    artist_source: str
    artist_id: str
    artist_name: str
    state: str
    scope: dict[str, Any] = field(default_factory=dict)
    plan: dict[str, Any] = field(default_factory=dict)
    results: list[dict[str, Any]] = field(default_factory=list)
    total_count: int = 0
    covered_count: int = 0
    supplement_count: int = 0
    failed_count: int = 0
    created_at: str = ""
    updated_at: str = ""
    finished_at: Optional[str] = None
    last_error: Optional[str] = None


class ArtistAcquisitionRepository(Protocol):
    """艺术家作品获取任务持久化端口。"""

    def find_by_plan_key(self, *, username: str, plan_key: str) -> Optional[ArtistAcquisitionSnapshot]: ...

    def get(self, job_id: str) -> Optional[ArtistAcquisitionSnapshot]: ...

    def create(self, snapshot: ArtistAcquisitionSnapshot) -> ArtistAcquisitionSnapshot: ...

    def update(self, snapshot: ArtistAcquisitionSnapshot) -> ArtistAcquisitionSnapshot: ...


_repository: Optional[ArtistAcquisitionRepository] = None


def configure_artist_acquisition_repository(
    repository: Optional[ArtistAcquisitionRepository],
) -> None:
    """由组合根发布唯一任务仓储。"""
    global _repository
    _repository = repository


def get_artist_acquisition_repository() -> ArtistAcquisitionRepository:
    """返回已配置仓储，拒绝在未启动宿主中隐式连库。"""
    if _repository is None:
        raise RuntimeError("艺术家作品获取任务仓储尚未配置")
    return _repository


_COLLECTION_TERMS = re.compile(
    r"(?:discography|collection|complete|anthology|box\s*set|"
    r"大全|合集|全集|全收录|专辑合集|精选)",
    re.IGNORECASE,
)
_TOKEN_SPLIT = re.compile(r"[^0-9a-z\u3400-\u9fff]+", re.IGNORECASE)
_YEAR_RANGE = re.compile(r"((?:19|20)\d{2})\s*[-–—~至]\s*((?:19|20)\d{2})")


def normalize_music_text(value: str) -> str:
    """对跨语言专辑名做保守归一，不做简繁强制转换。"""
    text = unicodedata.normalize("NFKC", value or "").casefold()
    return " ".join(token for token in _TOKEN_SPLIT.split(text) if token)


def is_artist_collection_resource(title: str, description: str = "") -> bool:
    """判断资源是否表达了跨多张作品的艺术家合集。"""
    return bool(_COLLECTION_TERMS.search(f"{title} {description}"))


def _work_aliases(work: ArtistWork) -> tuple[str, ...]:
    aliases = {
        normalize_music_text(work.title),
        *(normalize_music_text(alias) for alias in work.title_aliases),
    }
    return tuple(alias for alias in aliases if len(alias) >= 2)


def evaluate_collection_coverage(
    *,
    works: tuple[ArtistWork, ...],
    file_paths: tuple[str, ...],
    resource_title: str,
    resource_description: str = "",
    folder_name: str = "",
) -> CollectionCoverage:
    """用种子内部路径确认作品覆盖，资源标题只能给出 probable。"""
    normalized_paths = tuple(normalize_music_text(path) for path in file_paths)
    resource_text = normalize_music_text(f"{resource_title} {resource_description} {folder_name}")
    alias_owners: dict[str, set[str]] = {}
    for candidate in works:
        for alias in _work_aliases(candidate):
            alias_owners.setdefault(alias, set()).add(candidate.media_id)
    year_ranges = tuple(
        (int(match.group(1)), int(match.group(2)))
        for match in _YEAR_RANGE.finditer(f"{resource_title} {resource_description} {folder_name}")
    )
    coverage: list[WorkCoverage] = []
    for work in works:
        aliases = _work_aliases(work)
        matched_path = next(
            (
                original
                for original, normalized in zip(file_paths, normalized_paths)
                if any(
                    alias in normalized
                    and (not work.year or str(work.year) in normalized or len(alias_owners.get(alias, ())) == 1)
                    for alias in aliases
                )
            ),
            None,
        )
        if matched_path:
            coverage.append(WorkCoverage(work.media_id, "confirmed", f"种子路径：{matched_path}"))
            continue
        title_match = any(alias in resource_text for alias in aliases)
        year_match = not work.year or str(work.year) in resource_text
        range_match = bool(work.year and any(start <= work.year <= end for start, end in year_ranges))
        if (title_match and year_match) or range_match:
            coverage.append(
                WorkCoverage(
                    work.media_id,
                    "probable",
                    "资源标题或描述命中" if title_match else "资源年份范围命中",
                )
            )
        else:
            coverage.append(WorkCoverage(work.media_id, "missing"))
    return CollectionCoverage(
        folder_name=folder_name,
        file_count=len(file_paths),
        confirmed_count=sum(item.state == "confirmed" for item in coverage),
        probable_count=sum(item.state == "probable" for item in coverage),
        missing_count=sum(item.state == "missing" for item in coverage),
        works=tuple(coverage),
    )


def artist_work_from_mapping(item: Mapping[str, Any]) -> ArtistWork:
    """从 API 或持久化计划安全投影匹配事实。"""
    year_value = item.get("year")
    year = int(str(year_value)) if year_value not in (None, "") else None
    return ArtistWork(
        media_id=str(item.get("media_id") or ""),
        title=str(item.get("title") or item.get("album") or ""),
        year=year,
        album_type=str(item.get("album_type") or "Album"),
        title_aliases=tuple(str(value) for value in item.get("title_aliases") or ()),
    )
