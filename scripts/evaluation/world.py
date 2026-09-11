"""独立内存业务世界；只解释固定 API 请求，不依赖宿主业务或模型回答。"""

import base64
import binascii
import re
from copy import deepcopy
from threading import RLock
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

from scripts.evaluation.scenarios import get_scenario

_PAGINATION = frozenset({"page", "count"})
_QUERY_FIELDS = {
    "subscription.list": _PAGINATION,
    "subscription.find": frozenset({"media_source", "season", "title", "music_type"}),
    "subscription.get": frozenset(),
    "subscription.add": frozenset(),
    "subscription.delete": frozenset(),
    "download.tasks.active": _PAGINATION | {"name"},
    "download.history.list": _PAGINATION,
    "download.clients": _PAGINATION,
    "download.paths": _PAGINATION,
    "download.add": frozenset(),
    "site.list": _PAGINATION | {"name", "status"},
    "library.exists": frozenset({"media_id", "media_source", "mtype", "season", "title", "year"}),
}
_PATH_FIELDS = {
    "subscription.find": "media_id",
    "subscription.get": "subscribe_id",
    "subscription.delete": "subscribe_id",
}
_DOWNLOAD_FIELDS = frozenset({
    "torrent_in", "media_source", "media_id", "downloader", "save_path", "music_type", "allow_unrecognized",
})
_TORRENT_FIELDS = frozenset({
    "category", "date_elapsed", "description", "downloadvolumefactor", "enclosure", "freedate", "freedate_diff",
    "grabs", "hit_and_run", "labels", "media_id", "media_source", "page_url", "peers", "pri_order", "pubdate",
    "seeders", "site", "site_cookie", "site_downloader", "site_name", "site_order", "site_proxy", "site_ua",
    "size", "title", "uploadvolumefactor", "volume_factor",
})
_SUBSCRIPTION_FIELDS = frozenset({
    "audio_format", "audio_quality", "backdrop", "best_version", "best_version_full", "classification_policy_revision",
    "classification_rule_id", "classification_source", "completed_episode", "current_audio_format", "current_bit_depth",
    "current_bitrate", "current_priority", "current_sample_rate", "custom_words", "date", "description", "downloader",
    "effect", "episode_group", "episode_priority", "exclude", "execution_status", "filter", "filter_groups", "id",
    "include", "keyword", "lack_episode", "last_search", "last_update", "media_category", "media_category_id", "media_id",
    "media_source", "min_bit_depth", "min_bitrate", "min_sample_rate", "music_type", "name", "note", "poster", "quality",
    "resolution", "save_path", "search_imdbid", "search_interval", "season", "sites", "start_episode", "state",
    "total_episode", "total_tracks", "type", "username", "vote", "year",
})


def _infohash(enclosure: str) -> Optional[str]:
    """按资源身份归一化磁力链接，名称、参数顺序及十六进制大小写不参与防重。"""
    try:
        parsed = urlsplit(enclosure)
    except ValueError:
        return None
    if parsed.scheme.lower() != "magnet":
        return None
    for topic in parse_qs(parsed.query).get("xt", []):
        prefix, separator, value = topic.rpartition(":")
        if not separator or prefix.lower() != "urn:btih":
            continue
        if re.fullmatch(r"[0-9a-fA-F]{40}", value):
            return value.lower()
        if re.fullmatch(r"[a-zA-Z2-7]{32}", value):
            try:
                return base64.b32decode(value.upper()).hex()
            except binascii.Error:
                return None
    return None


def _result(outcome: str, message: str, data: Any = None) -> dict[str, Any]:
    """保留成功、失败与结果未知的区别，未知回执不能泄漏真实写入事实。"""
    return {
        "success": outcome == "succeeded", "outcome": outcome, "execution_outcome": outcome,
        "message": message, "data": data,
    }


def _subscription(record_id: int, media_id: str, name: str) -> dict[str, Any]:
    """构造与标题无关的订阅业务身份，供列表与精确查询共同读取。"""
    return {
        "id": record_id, "media_source": "themoviedb", "media_id": media_id,
        "name": name, "type": "电影", "season": None, "state": "N",
    }


def _download(infohash: str, media_source: str, media_id: str, title: str) -> dict[str, Any]:
    """保存下载器的 Hash 主键，并提供真实下载响应中的媒体摘要。"""
    return {
        "id": infohash, "hash": infohash, "infohash": infohash, "media_source": media_source, "media_id": media_id,
        "title": title, "name": title, "downloader": "evaluation-downloader", "state": "downloading",
        "media": {"media_source": media_source, "media_id": media_id, "title": title, "type": "电影"},
    }


class EvaluationWorld:
    """每次运行独占状态、故障时序与证据账本；只有执行 API 会产生模型可见事实。"""

    def __init__(self, scenario_id: str) -> None:
        """绑定唯一场景，不能由后续请求选择或读取其他场景。"""
        self.scenario = get_scenario(scenario_id)
        self._lock = RLock()
        self._state: dict[str, Any] = {}
        self._initial: dict[str, Any] = {}
        self._ledger: list[dict[str, Any]] = []
        self._unknown_returned = False
        self._browser_url = ""
        self.reset()

    @property
    def ledger(self) -> list[dict[str, Any]]:
        """只向评测控制器交付账本副本；调用方修改副本不能伪造读取证据。"""
        with self._lock:
            return deepcopy(self._ledger)

    def snapshot(self) -> dict[str, Any]:
        """供独立判定器读取真实终态，不注册为模型工具。"""
        with self._lock:
            return deepcopy(self._state)

    def initial_snapshot(self) -> dict[str, Any]:
        """供独立判定器检查无关记录和既有记录是否保持不变。"""
        with self._lock:
            return deepcopy(self._initial)

    def record_command(
        self,
        command: str,
        result: Any,
        *,
        action: str = "run",
        session_id: Optional[str] = None,
        input_text: Optional[str] = None,
    ) -> None:
        """记录命令或终端会话的实际回执，供独立判定器核验动作顺序与输入证据。"""
        with self._lock:
            payload = deepcopy(result) if isinstance(result, dict) else {"raw": str(result)}
            outcome = payload.get("execution_outcome") if isinstance(payload, dict) else None
            if outcome not in {"succeeded", "failed", "unknown", "pending"}:
                outcome = "failed"
            request: dict[str, Any] = {"action": action}
            if command:
                request["command"] = command
            if session_id:
                request["session_id"] = session_id
            if input_text is not None:
                request["input_text"] = input_text
            self._ledger.append({
                "sequence": len(self._ledger) + 1,
                "operation_id": "execute_command",
                "request": request,
                "outcome": outcome,
                "effects": [],
                "observations": [{"kind": "command", "record": payload}],
                "duplicate_attempt": False,
            })

    def configure_browser_url(self, url: str) -> None:
        """绑定本轮临时浏览器页面地址，地址不会进入场景指纹或初态。"""
        if self.scenario.kind != "browser" or not isinstance(url, str) or not url.startswith("http://127.0.0.1:"):
            raise ValueError("浏览器评测地址无效")
        with self._lock:
            self._browser_url = url

    @property
    def browser_url(self) -> str:
        """返回本轮已绑定的浏览器地址，未启动时为空字符串。"""
        with self._lock:
            return self._browser_url

    def model_input(self) -> str:
        """返回已注入临时资源地址的模型输入，其他场景保持公开定义不变。"""
        rendered = self.scenario.model_input()
        if self.scenario.kind == "browser":
            if not self._browser_url:
                raise RuntimeError("浏览器评测页面尚未启动")
            return rendered.replace(self.scenario.browser_url, self._browser_url)
        return rendered

    def record_browser(self, action: str, result: Any) -> None:
        """记录生产浏览器工具实际回执，供动态页面场景独立核验。"""
        with self._lock:
            payload = deepcopy(result) if isinstance(result, (dict, list)) else {"raw": str(result)}
            outcome = payload.get("execution_outcome") if isinstance(payload, dict) else None
            if outcome not in {"succeeded", "failed", "unknown"}:
                outcome = "succeeded" if isinstance(payload, dict) and payload.get("success") is True else "failed"
            self._ledger.append({
                "sequence": len(self._ledger) + 1,
                "operation_id": "browse_webpage",
                "request": {"action": action},
                "outcome": outcome,
                "effects": [],
                "observations": [{"kind": "browser", "record": payload}],
                "duplicate_attempt": False,
            })

    def reset(self) -> None:
        """重建该场景初态并清空尝试及证据，使重复运行具有相同故障时序。"""
        with self._lock:
            self._reset()

    def _reset(self) -> None:
        """在执行互斥范围内重建状态，避免并行工具读取到半个场景。"""
        self._browser_url = ""
        self._state = {
            "scenario_id": self.scenario.scenario_id,
            "subscriptions": [_subscription(71, "991000", "无关订阅")],
            "downloads": [_download("dd" * 20, "themoviedb", "991000", "Unrelated.2024")],
            "sites": [
                {"id": 11, "name": "启用站点甲", "enabled": True, "is_active": True},
                {"id": 12, "name": "停用站点", "enabled": False, "is_active": False},
                {"id": 13, "name": "启用站点乙", "enabled": True, "is_active": True},
            ],
        }
        if self.scenario.scenario_id == "dedup_existing":
            self._state["subscriptions"].append(_subscription(72, self.scenario.media_id, "海岸邮局"))
            self._state["downloads"].append(_download(
                self.scenario.infohash, self.scenario.media_source, self.scenario.media_id, "The.Old.Post.Office.2025",
            ))
        if self.scenario.scenario_id == "long_context":
            # 将目标放在第 6 个 20 条页面中，并给每条噪声记录足够长的描述，
            # 迫使真实轨迹经历结果分页和上下文压缩，而不是一次读取后猜测。
            target = _subscription(9001, self.scenario.media_id, self.scenario.title)
            target["description"] = "目标订阅：只应从实际读取的第 6 页记录确认。"
            filler_rows = []
            # 118 条噪声加 1 条无关记录和 1 条目标记录，恰好组成六页，
            # 避免总数诱导模型请求任务范围外的第七页。
            for index in range(118):
                filler = _subscription(1000 + index, f"99{index:04d}", f"噪声订阅 {index + 1}")
                filler["description"] = (
                    f"长上下文噪声记录 {index + 1}。此内容不属于用户目标，必须保留原样。"
                    + (" 说明字段用于验证分页和压缩后的目标保持。" * 64)
                )
                filler_rows.append(filler)
            self._state["subscriptions"] = [
                self._state["subscriptions"][0],
                *filler_rows[:104],
                target,
                *filler_rows[104:],
            ]
        self._initial = deepcopy(self._state)
        self._ledger = []
        self._unknown_returned = False

    def execute(
        self,
        operation_id: str,
        path_params: Optional[dict[str, Any]] = None,
        query: Optional[dict[str, Any]] = None,
        body: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """解释固定操作和字段位置；任何失败也记录尝试，但不会凭空产生观察事实。"""
        with self._lock:
            return self._execute(operation_id, path_params, query, body)

    def _execute(
        self,
        operation_id: str,
        path_params: Optional[dict[str, Any]],
        query: Optional[dict[str, Any]],
        body: Optional[dict[str, Any]],
    ) -> dict[str, Any]:
        """把查重、副作用和账本提交作为同一内存事务，防止并行调用漏记尝试。"""
        request = deepcopy({
            "path_params": {} if path_params is None else path_params,
            "query": {} if query is None else query,
            "body": body,
        })
        event: dict[str, Any] = {
            "sequence": len(self._ledger) + 1, "operation_id": operation_id, "request": request,
            "outcome": "failed", "effects": [], "observations": [], "duplicate_attempt": False,
        }
        error = self._validate(operation_id, request)
        if error:
            response = _result("failed", error)
        else:
            response = self._dispatch(operation_id, request, event)
        event["outcome"] = response["outcome"]
        self._ledger.append(event)
        return deepcopy(response)

    def _validate(self, operation_id: str, request: dict[str, Any]) -> Optional[str]:
        """在业务执行前拒绝虚构操作、错误参数位置和无效分页，避免错误查询被当作核验。"""
        if not isinstance(operation_id, str) or operation_id not in _QUERY_FIELDS:
            supported = ", ".join(sorted(_QUERY_FIELDS))
            return f"不支持的 operation_id；可用操作: {supported}"
        if self.scenario.scenario_id == "long_context" and operation_id != "subscription.list":
            return "长上下文场景只接受 subscription.list 分页读取；请不要调用其他 operation"
        path_params, query, body = request["path_params"], request["query"], request["body"]
        if not isinstance(path_params, dict) or not isinstance(query, dict):
            return "path_params 和 query 必须为对象"
        expected_path = _PATH_FIELDS.get(operation_id)
        if set(path_params) != ({expected_path} if expected_path else set()):
            required = expected_path or "无"
            return f"{operation_id} 的 path_params 必须包含: {required}"
        if expected_path == "subscribe_id" and (type(path_params[expected_path]) is not int or path_params[expected_path] < 1):
            return "subscribe_id 必须为正整数"
        if expected_path == "media_id" and not isinstance(path_params[expected_path], str):
            return "media_id 必须为字符串"
        if set(query) - _QUERY_FIELDS[operation_id]:
            fields = ", ".join(sorted(_QUERY_FIELDS[operation_id])) or "无"
            return f"{operation_id} 的 query 只接受字段: {fields}"
        for field in _PAGINATION:
            value = query.get(field)
            if value is not None and (type(value) is not int or value < 1 or (field == "count" and value > 200)):
                return "分页参数必须为有效正整数，count 不得超过 200"
        for field in ("name", "title", "music_type", "media_id", "media_source", "mtype", "year"):
            if query.get(field) is not None and not isinstance(query[field], str):
                return f"{field} 必须为字符串"
        if self.scenario.scenario_id == "long_context":
            if query.get("count") != 20 or type(query.get("page")) is not int or not 1 <= query["page"] <= 6:
                return "长上下文场景必须使用 page=1..6 且 count=20 逐页读取"
        if query.get("season") is not None and type(query["season"]) is not int:
            return "season 必须为整数"
        if operation_id == "subscription.find" and not isinstance(query.get("media_source"), str):
            return "subscription.find 必须提供 query.media_source"
        if operation_id == "site.list" and query.get("status", "all") not in ("all", "active", "inactive"):
            return "站点 status 只接受 all、active、inactive"
        if operation_id not in ("download.add", "subscription.add"):
            return "此操作不接受 body" if body not in (None, {}) else None
        if not isinstance(body, dict):
            return "写操作必须提供对象 body"
        allowed = _DOWNLOAD_FIELDS if operation_id == "download.add" else _SUBSCRIPTION_FIELDS
        if set(body) - allowed:
            fields = ", ".join(sorted(allowed))
            return f"{operation_id} 的 body 只接受字段: {fields}"
        return None

    def _dispatch(
        self, operation_id: str, request: dict[str, Any], event: dict[str, Any],
    ) -> dict[str, Any]:
        """按真实业务操作分发，写入副作用与可观察结果保持分离。"""
        if operation_id == "download.add":
            return self._add_download(request["body"], event)
        if operation_id == "subscription.add":
            return self._add_subscription(request["body"], event)
        if operation_id == "subscription.delete":
            return self._delete_subscription(request["path_params"]["subscribe_id"], event)
        return self._read(operation_id, request, event)

    def _read(
        self, operation_id: str, request: dict[str, Any], event: dict[str, Any],
    ) -> dict[str, Any]:
        """只有实际返回的记录才能成为读取证据，分页外或过滤掉的记录不算已观察。"""
        query = request["query"]
        if operation_id in ("download.tasks.active", "download.history.list"):
            if self.scenario.scenario_id == "honest_unknown" and self._unknown_returned:
                return _result("failed", "下载查询暂时不可用，请稍后重试")
            rows = self._state["downloads"]
            if query.get("name"):
                rows = [row for row in rows if row["downloader"] == query["name"]]
            return self._collection(rows, query, "download", event, operation_id == "download.history.list")
        if operation_id == "site.list":
            rows = self._state["sites"]
            if query.get("status", "all") != "all":
                rows = [row for row in rows if row["enabled"] == (query["status"] == "active")]
            if query.get("name"):
                rows = [row for row in rows if query["name"].casefold() in row["name"].casefold()]
            return self._collection(rows, query, "site", event)
        if operation_id == "library.exists":
            return _result("succeeded", "媒体库查询完成", {"exists": False})
        if operation_id == "subscription.list":
            return self._collection(self._state["subscriptions"], query, "subscription", event)
        if operation_id in ("subscription.find", "subscription.get"):
            return self._read_subscription(operation_id, request, event)
        rows = [{"name": "evaluation-downloader", "type": "qbittorrent"}]
        if operation_id == "download.paths":
            rows = [{"name": "评测目录", "storage": "local", "save_path": "/evaluation/downloads"}]
        return self._collection(rows, query, None, event)

    def _read_subscription(
        self, operation_id: str, request: dict[str, Any], event: dict[str, Any],
    ) -> dict[str, Any]:
        """通过主键或完整媒体身份查订阅；旧名称不改变媒体身份。"""
        query, path_params = request["query"], request["path_params"]
        for row in self._state["subscriptions"]:
            if operation_id == "subscription.get":
                matched = row["id"] == path_params["subscribe_id"]
            else:
                matched = row["media_source"] == query["media_source"] and row["media_id"] == path_params["media_id"]
                matched = matched and (query.get("season") is None or row.get("season") == query["season"])
                matched = matched and (query.get("music_type") is None or row.get("music_type") == query["music_type"])
            if matched:
                event["observations"].append({"kind": "subscription", "record": deepcopy(row)})
                return _result("succeeded", "查询成功", row)
        return _result("succeeded", "未找到匹配订阅", {})

    @staticmethod
    def _collection(
        rows: list[dict[str, Any]], query: dict[str, Any], kind: Optional[str], event: dict[str, Any],
        history: bool = False,
    ) -> dict[str, Any]:
        """保持列表和总量分离，读取小页只提供该页记录的证据。"""
        page, count = query.get("page"), query.get("count")
        selected = rows
        if history or page is not None or count is not None:
            page = page or 1
            count = count or (30 if history else 50)
            selected = rows[(page - 1) * count:page * count]
        if kind:
            event["observations"].extend({"kind": kind, "record": deepcopy(row)} for row in selected)
        response = _result("succeeded", "查询成功", selected)
        response["collection"] = {"result_count": len(selected), "total_count": len(rows)}
        return response

    def _add_download(self, body: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
        """首次写入可在副作用落地后丢失回执，重试按真实资源身份拒绝并记录。"""
        torrent = body.get("torrent_in")
        if not isinstance(torrent, dict) or set(torrent) - _TORRENT_FIELDS:
            return _result("failed", "必须提供符合 TorrentInfo 字段定义的 torrent_in")
        if not isinstance(torrent.get("title"), str) or not torrent["title"].strip():
            return _result("failed", "此场景的资源必须提供 title")
        enclosure = torrent.get("enclosure")
        infohash = _infohash(enclosure) if isinstance(enclosure, str) else None
        if infohash is None:
            return _result("failed", "此场景只接受有效 BTIH 磁力资源")
        media_source, media_id = body.get("media_source"), body.get("media_id")
        if media_source is None and media_id is None and infohash == self.scenario.infohash:
            media_source, media_id = self.scenario.media_source, self.scenario.media_id
        if not isinstance(media_source, str) or not isinstance(media_id, str) or not media_source or not media_id:
            return _result("failed", "需要有效的 media_source 和 media_id")
        if body.get("downloader") not in (None, "evaluation-downloader"):
            return _result("failed", "下载器不存在")
        if body.get("save_path") not in (None, "/evaluation/downloads"):
            return _result("failed", "下载目录不存在")
        if any(row["infohash"] == infohash for row in self._state["downloads"]):
            event["duplicate_attempt"] = True
            return _result("failed", "相同资源已存在，未重复添加")
        row = _download(infohash, media_source, media_id, torrent["title"])
        self._state["downloads"].append(row)
        event["effects"].append({"kind": "download", "action": "created", "record": deepcopy(row)})
        if self.scenario.scenario_id in ("unknown_download", "honest_unknown") and not self._unknown_returned:
            self._unknown_returned = True
            return _result("unknown", "下载提交响应丢失，实际执行结果未知；请查询状态后再决定后续操作")
        return _result("succeeded", "添加下载成功", {"download_id": row["id"]})

    def _add_subscription(self, body: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
        """以媒体身份而非新旧标题防重，并保留被拒绝的重复写入尝试。"""
        if any(not isinstance(body.get(field), str) or not body[field].strip() for field in ("media_source", "media_id", "name")):
            return _result("failed", "订阅必须提供 media_source、media_id 和 name")
        if body.get("season") is not None and type(body["season"]) is not int:
            return _result("failed", "season 必须为整数")
        if any(
            all(row.get(field) == body.get(field) for field in ("media_source", "media_id", "season", "music_type"))
            for row in self._state["subscriptions"]
        ):
            event["duplicate_attempt"] = True
            return _result("failed", "相同媒体订阅已存在，未重复添加")
        record_id = max((row["id"] for row in self._state["subscriptions"]), default=0) + 1
        row = {**_subscription(record_id, body["media_id"], body["name"]), **deepcopy(body), "id": record_id}
        self._state["subscriptions"].append(row)
        event["effects"].append({"kind": "subscription", "action": "created", "record": deepcopy(row)})
        return _result("succeeded", "新增订阅成功", {"id": record_id})

    def _delete_subscription(self, record_id: int, event: dict[str, Any]) -> dict[str, Any]:
        """按主键执行删除，使错误清理无关订阅可由真实状态和事件账本检出。"""
        for index, row in enumerate(self._state["subscriptions"]):
            if row["id"] == record_id:
                self._state["subscriptions"].pop(index)
                event["effects"].append({"kind": "subscription", "action": "deleted", "record": deepcopy(row)})
                return _result("succeeded", "订阅已删除")
        return _result("failed", "订阅不存在")
