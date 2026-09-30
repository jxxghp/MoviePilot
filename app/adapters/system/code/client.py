"""工具 RPC 客户端及标准辅助函数，工具存根只按宿主提供的允许集合生成。"""

import json
import keyword
from typing import Any

_HEADER = r'''
"""Auto-generated tools RPC stubs."""
import json, os, socket, shlex, threading, time

_sock = None
_UNSET = object()
# The RPC server handles a single client connection serially and has no
# request-id in the protocol, so concurrent _call() invocations from multiple
# threads (e.g. ThreadPoolExecutor) would race on the shared socket and get
# each other's responses. Serialize the entire send+recv round-trip.
_call_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Convenience helpers (avoid common scripting pitfalls)
# ---------------------------------------------------------------------------

def json_parse(text: str):
    """Parse JSON tolerant of control characters and UTF-8 BOM (strict=False).
    Use this instead of json.loads() when parsing output from terminal()
    or web_extract() that may contain raw tabs/newlines in strings,
    or from tools/files that prepend a UTF-8 BOM (salvage #57870, credit @woxinwuhen713-bit)."""
    if isinstance(text, str) and text.startswith("﻿"):
        text = text[1:]
    return json.loads(text, strict=False)


def shell_quote(s: str) -> str:
    """Shell-escape a string for safe interpolation into commands.
    Use this when inserting dynamic content into terminal() commands:
        terminal(f"echo {shell_quote(user_input)}")
    """
    return shlex.quote(s)


def retry(fn, max_attempts=3, delay=2):
    """Retry a function up to max_attempts times with exponential backoff.
    Use for transient failures (network errors, API rate limits):
        result = retry(lambda: terminal("gh issue list ..."))
    """
    last_err = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except Exception as e:
            last_err = e
            if attempt < max_attempts - 1:
                time.sleep(delay * (2 ** attempt))
    raise last_err


def _connect():
    """Connect to the parent's RPC server via the transport it picked.

    MOVIEPILOT_RPC_SOCKET can be either:
      - a filesystem path (POSIX Unix domain socket — the default on
        Linux and macOS)
      - a string of the form ``tcp://127.0.0.1:<port>`` (Windows, where
        AF_UNIX is unreliable — the parent falls back to loopback TCP)
    """
    global _sock
    if _sock is None:
        endpoint = os.environ["MOVIEPILOT_RPC_SOCKET"]
        if endpoint.startswith("tcp://"):
            # tcp://host:port  (host is always 127.0.0.1 in practice — we
            # only bind loopback server-side)
            _host_port = endpoint[len("tcp://"):]
            _host, _, _port = _host_port.rpartition(":")
            _sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            _sock.connect((_host or "127.0.0.1", int(_port)))
        else:
            _sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            _sock.connect(endpoint)
        _sock.settimeout(300)
    return _sock

def _call(tool_name, args):
    """Send a tool call to the parent process and return the parsed result."""
    request = json.dumps({
        "tool": tool_name,
        "args": args,
        "token": os.environ.get("MOVIEPILOT_RPC_TOKEN", ""),
    }) + "\n"
    # Session kernels outlive the RPC server's 300s idle window, so their
    # connection can be legitimately gone by the next cell. The server
    # re-accepts (MOVIEPILOT_RPC_PERSISTENT=1); retry once on a fresh socket.
    _attempts = 2 if os.environ.get("MOVIEPILOT_RPC_PERSISTENT") == "1" else 1
    with _call_lock:
        for _attempt in range(_attempts):
            try:
                conn = _connect()
                conn.sendall(request.encode())
                buf = b""
                while True:
                    chunk = conn.recv(65536)
                    if not chunk:
                        raise RuntimeError("Agent process disconnected")
                    buf += chunk
                    if buf.endswith(b"\n"):
                        break
                break
            except (OSError, RuntimeError):
                global _sock
                try:
                    if _sock is not None:
                        _sock.close()
                except OSError:
                    pass
                _sock = None
                if _attempt + 1 >= _attempts:
                    raise
    raw = buf.decode().strip()
    result = json.loads(raw)
    if isinstance(result, str):
        try:
            return json.loads(result)
        except (json.JSONDecodeError, TypeError):
            return result
    return result

'''


def build_client(names: tuple[str, ...], schemas: dict[str, dict[str, Any]] | None = None) -> str:
    """工具名来自宿主目录，禁止模型把任意源码注入存根定义。"""
    definitions = []
    for name in names:
        if not name.isidentifier() or name.startswith('_') or keyword.iskeyword(name):
            raise ValueError('无效的工具标识')
        schema = (schemas or {}).get(name)
        if schema is None:
            definitions.append(f'def {name}(**kwargs):\n    """Call the current session tool and return parsed JSON."""\n    return _call({name!r}, kwargs)\n')
        else:
            definitions.append(_stub(name, schema))
    return _HEADER + '\n'.join(definitions)


def _stub(name: str, schema: dict[str, Any]) -> str:
    """按实际 schema 生成可用位置参数的签名；未提供的可选字段不伪造为 null。"""
    properties = schema.get('properties', {})
    required = schema.get('required', [])
    names = sorted(properties, key=lambda field: field not in required)
    if any(not field.isidentifier() or field.startswith('_') or keyword.iskeyword(field) for field in names):
        raise ValueError('无效的工具参数标识')
    parameters = [field if field in required else f'{field}=_UNSET' for field in names]
    arguments = ', '.join(f'{field!r}: {field}' for field in names)
    description = 'Call the current session tool and return parsed JSON. Input schema: ' + json.dumps(schema, ensure_ascii=False)
    return (f'def {name}({", ".join(parameters)}):\n    {description!r}\n'
            f'    args = {{{arguments}}}\n'
            f'    return _call({name!r}, {{key: value for key, value in args.items() if value is not _UNSET}})\n')
