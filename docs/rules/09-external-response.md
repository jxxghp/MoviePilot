# 09 — External APIs, Protocols, and Responses

## HTTP Client Conventions

**Rule:** Host-authored ordinary outbound HTTP must go through `RequestUtils` from
`app/adapters/network/http.py`. This transport rule does not authorize Application or Chain to
import the concrete Adapter; they own/use a Port and receive its implementation from startup.
Plugins import the curated facade from `app.sdk.network`.

Direct `requests`/`httpx` clients, SDK transports, product streaming protocols, contained vendor
code, diagnostics and control-plane access are governed by the exact `direct_egress` facts and
manual policy. An approved fingerprint is containment for that exact source/binding/use identity
only; `owner: "$source"` names that source module and does not authorize another caller or
operation. New SDK roots must be added to the registry and receive explicit policy review.
Ordinary direct HTTP, RequestUtils Session bridges and Application-owned DNS I/O remain
`temporary_debt` with an empty target state. Runtime wildcard imports from registered egress roots
are forbidden.

`RequestUtils` handles:
- Proxy configuration (from `settings.PROXY_*`)
- Timeouts
- SSL verification settings
- User-Agent headers
- Retry logic

The shared asynchronous transport tracks consecutive `PoolTimeout` failures per
event loop and configuration. After three failures it replaces that pool for
subsequent requests and logs the recovery. Responses and other request errors
reset the count. It does not replay the failed request. The retired pool closes
after its existing client contexts exit, including streaming reads and
cancellation. While a retired pool remains unclosed, the same configuration
cannot rotate again, bounding the active generations to two. Caller-owned
clients retain their own transport lifecycle.

```python
# 此处展示适配器实现；应用服务和业务链通过注入的接口访问网络能力
from app.adapters.network.http import RequestUtils

res = RequestUtils(
    ua=settings.USER_AGENT,
    proxies=settings.PROXY,
    timeout=30,
).get_res(url="https://api.example.com/data")

if res and res.status_code == 200:
    data = res.json()
```

---

## Response Format — REST API

All REST API responses use Pydantic schema models from `app/schemas/`. Do not return raw `dict` objects from endpoints.

### Standard Response Patterns

```python
# 成功响应携带业务结果
from app.schemas.response import Response

return Response(success=True, message="", data=result)

# 无业务结果时只返回成功状态和说明
return Response(success=True, message="操作成功")

# 失败响应保留可向调用方展示的原因
return Response(success=False, message="错误原因描述")
```

### List Responses

For paginated lists, follow the pattern of existing endpoint files. Check `app/api/endpoints/` for examples matching the resource domain.

### Error Responses (Endpoint Layer Only)

In endpoints, raise `HTTPException` for request-level errors:

```python
from fastapi import HTTPException

raise HTTPException(status_code=404, detail="Resource not found")
raise HTTPException(status_code=403, detail="Permission denied")
```

Do not raise `HTTPException` in chain or module code. Chains and modules return `None` or domain-level error objects on failure; the endpoint translates that into an HTTP response.

---

## Error Handling by Layer

| Layer | On external API failure |
|---|---|
| Module | Log the error, return `None` or `(False, "error message")` tuple |
| Chain | Log the error, return `None` or an appropriate domain object with failure indication |
| Endpoint | Translate `None` or failure result into a `Response(success=False, ...)` or `HTTPException` |

```python
def test(self) -> Optional[Tuple[bool, str]]:
    """
    在模块层检查客户端连通性并归一化失败结果。

    :return: 连接状态与说明组成的二元组；客户端异常会记录日志并转换为失败结果
    """
    try:
        ok = self.client.ping()
        return (True, "连接成功") if ok else (False, "连接失败")
    except Exception as err:
        logger.error(f"测试连通性失败：{str(err)}")
        return (False, str(err))
```

---

## MCP Protocol

MoviePilot exposes an MCP (Model Context Protocol) interface for AI agent integration.

- **Transport:** HTTP, JSON-RPC 2.0
- **Base path:** `/api/v1/mcp`
- **Protocol versions supported:** `2025-11-25`, `2025-06-18`, `2024-11-05`

### Authentication

```
Header: X-API-KEY: <api_key>
Query:  ?apikey=<api_key>
```

### Supported Methods

| Method | Description |
|---|---|
| `initialize` | Initialize session, negotiate protocol version and capabilities |
| `notifications/initialized` | Client confirmation of initialization |
| `tools/list` | List all available tools |
| `tools/call` | Invoke a specific tool |
| `ping` | Connection liveness check |

### Error Codes

| Code | Message | Meaning |
|---|---|---|
| -32700 | Parse error | Malformed JSON |
| -32600 | Invalid Request | Invalid JSON-RPC request structure |
| -32601 | Method not found | Unknown method |
| -32602 | Invalid params | Parameter validation failure |
| -32002 | Session not found | Session does not exist or has expired |
| -32003 | Not initialized | Session has not completed initialization |
| -32603 | Internal error | Server-side error |

### Tool Response Format

MCP tools return structured content. Errors must use the JSON-RPC error object format, not HTTP status codes.

---

## Notification and Messaging

Internal notifications use the `Notification` schema and the event system:

```python
from app.schemas import Notification
from app.schemas.types import NotificationType, MessageChannel
from app.runtime.events import eventmanager
from app.schemas.types import EventType

eventmanager.send_event(
    EventType.NoticeMessage,
    {
        "channel": MessageChannel.Telegram,
        "type": NotificationType.Download,
        "title": "下载成功",
        "text": f"{media_name} 已添加到下载队列",
        "image": poster_url,
    }
)
```

Do not call message channel modules directly from chain code. Use the event bus to decouple senders from channels.

---

## Media Metadata API Conventions

When calling TMDB, TheTVDB, Douban, or Bangumi via the module layer:

- Always check the module return for `None` before using the result — modules return `None` when the backend is not configured or the request fails.
- Cache responses using `FileCache` / `AsyncFileCache` where the result is stable and repeated requests would be expensive.
- Return domain objects (`MediaInfo`, `TmdbEpisode`, `MediaPerson`, etc.) from modules, never raw API response dicts.

---

## Webhook Handling

Webhook payloads arrive at `app/api/endpoints/webhook.py` and are dispatched via `eventmanager.send_event(EventType.WebhookMessage, ...)`. Processing logic lives in the chain layer (`app/chain/webhook.py`).

Do not add webhook-specific business logic directly in the endpoint. The endpoint parses the payload and fires the event; the chain handles the response.

Jellyfin 的条目删除后无法通过播放接口反查路径。适配器优先使用 webhook 的 `Path`；
非删除事件查到的路径会按服务器和条目 ID 缓存在进程内（最多 10000 条、有效期 30 天），
删除事件消费缓存，不请求已删除条目的路径或图片。重启、过期或从未观察过的条目仍可能
没有路径，此时告警并保留 `item_path=None`，消费方不得据此推断同片其他副本。

插件兼容接口 `TransferHistoryOper.get_by` 及底层 `TransferHistory.list_by` 对电影和
未指定类型的查询要求非空 `dest`，空路径默认返回空列表。确实需要按身份或标题年份查询
全部版本时，调用方须显式传入 `match_all_versions=True`；路径未知的删除事件不得使用
该参数。电视剧整剧、整季及单集路径精确匹配继续沿用原有查询语义。

*Last Updated: 2026-08-14*
