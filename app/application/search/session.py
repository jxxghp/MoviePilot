"""搜索会话的持久检查点端口；页面候选和游标必须一次保存。"""

import json
import re
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Optional, Protocol
from urllib.parse import parse_qsl, urlsplit

from app.domain.search import SearchCollection, SearchSourceCursor

_TORRENT_FIELDS = (
    "site", "site_name", "title", "description", "media_source", "media_id",
    "size", "seeders", "peers", "grabs", "pubdate", "freedate", "uploadvolumefactor",
    "downloadvolumefactor", "hit_and_run", "labels", "pri_order", "category", "site_order",
)
_PUBLIC_QUERY_FIELDS = {"id", "tid", "torrentid", "torrent_id", "https", "type"}
# 路径中含字母的长十六进制串或字母数字混合长串可能是 passkey/RSS key；纯数字 ID 和带连字符的 slug 不受影响。
_SECRET_PATH_SEGMENT = re.compile(r"^(?:(?=[0-9]*[A-Fa-f])[0-9A-Fa-f]{16,}|(?=.*[0-9])(?=.*[A-Za-z])[A-Za-z0-9]{24,})$")
# 检查点超过该时长未更新时由保底清理删除；正常情况下任务进入终态即删除。
CHECKPOINT_RETENTION_SECONDS = 14 * 24 * 3600


def public_resource_url(value: Optional[str]) -> Optional[str]:
    """只保存公开定位 URL，携带票据或内嵌请求凭据的下载地址交给当前站点重新生成。"""
    if not value:
        return None
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or parts.username or parts.password:
        return None
    if any(key.lower() not in _PUBLIC_QUERY_FIELDS for key, _ in parse_qsl(parts.query)):
        return None
    if any(_SECRET_PATH_SEGMENT.match(segment.split(".", 1)[0]) for segment in parts.path.split("/")):
        return None
    return parts._replace(fragment="").geturl()


def torrent_snapshot(torrent: Any) -> dict[str, Any]:
    """白名单保存候选事实；Cookie/API Key 和间接下载配方不得进入检查点。"""
    values = {name: getattr(torrent, name, None) for name in _TORRENT_FIELDS}
    values["page_url"] = public_resource_url(getattr(torrent, "page_url", None))
    enclosure = public_resource_url(getattr(torrent, "enclosure", None))
    # 未验证的路径型票据也可能藏有 passkey，只有显式公共 ID 查询可直接保存。
    values["enclosure"] = enclosure if enclosure and any(
        key.lower() in {"id", "tid", "torrentid", "torrent_id"} for key, _ in parse_qsl(urlsplit(enclosure).query)
    ) else None
    return values


def encode_search_state(value: dict[str, Any]) -> str:
    """以确定性 JSON 编码页面事务，枚举和集合只保存值。"""
    def normalize(item: Any) -> Any:
        """保留确定性枚举和集合值，不对未知对象做隐式字符串化。"""
        if isinstance(item, Enum):
            return item.value
        if isinstance(item, (set, frozenset)):
            return sorted(item)
        raise TypeError(f"不支持的搜索快照类型：{type(item).__name__}")
    return json.dumps(value, default=normalize, ensure_ascii=False, sort_keys=True)


def collection_snapshot(collection: SearchCollection) -> dict[str, Any]:
    """把纯收集状态投影为持久值；资源本身由候选白名单单独保存。"""
    return asdict(collection)


def restore_collection(values: dict[str, Any]) -> SearchCollection:
    """恢复每来源的分页进度及关闭证据。"""
    sources = {}
    for key, data in values["sources"].items():
        params = dict(data)
        # 旧检查点未记录作品匹配前史；已有进度时不能断言此前从未命中，只应用页数上限。
        params.setdefault("matched_work", bool(params.get("next_page")))
        for name in ("seen", "closed"):
            params[name] = set(params.get(name, []))
        sources[key] = SearchSourceCursor(**params)
    return SearchCollection(set(values["targets"]), sources, set(values["settled"]), set(values["fallback"]),
                            dict(values.get("released") or {}))


@dataclass(frozen=True, slots=True)
class SearchSessionSnapshot:
    """脱离数据库会话的不可变分页快照，不包含运行期站点凭据。"""

    task_id: str
    version: int
    payload: str


class SearchSessionRepository(Protocol):
    """订阅搜索任务的检查点；所有权由队列任务租约决定，版本 CAS 排除迟到 worker。"""

    def get(self, *, task_id: str) -> Optional[SearchSessionSnapshot]:
        """读取任务的最新检查点。"""
        ...

    def create(self, *, task_id: str, payload: str, task_lease: Optional[str]) -> Optional[SearchSessionSnapshot]:
        """仍持有任务租约时创建初始检查点。"""
        ...

    def save(self, *, snapshot: SearchSessionSnapshot, payload: str,
             task_lease: Optional[str]) -> Optional[SearchSessionSnapshot]:
        """一次提交本页候选、收集状态和下一页；失去租约或版本冲突时返回空。"""
        ...

    def delete(self, *, snapshot: SearchSessionSnapshot, task_lease: Optional[str]) -> None:
        """本轮搜索结束后删除检查点；仅仍持有任务租约且版本未变时生效。"""
        ...
