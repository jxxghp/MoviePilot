# 09 — External APIs, Protocols, and Responses

## HTTP Client Conventions

**Rule:** All outbound HTTP requests must go through `RequestUtils` from `app/utils/http.py`. Do not use `requests`, `httpx`, or `aiohttp` directly.

`RequestUtils` handles:
- Proxy configuration (from `settings.PROXY_*`)
- Timeouts
- SSL verification settings
- User-Agent headers
- Retry logic

```python
from app.utils.http import RequestUtils

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
# Success with data
from app.schemas.response import Response

return Response(success=True, message="", data=result)

# Success without data
return Response(success=True, message="操作成功")

# Error
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
# Module layer
def test(self) -> Optional[Tuple[bool, str]]:
    """测试模块连通性"""
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
from app.core.event import eventmanager
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

*Last Updated: 2026-05-25*
