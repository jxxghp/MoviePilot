#!/usr/bin/env python3
"""
MoviePilot REST API CLI -- a lightweight command-line client for calling
any MoviePilot API endpoint directly.

Usage:
    python mp-api.py configure --host <HOST> --apikey <KEY>
    python mp-api.py GET /api/v1/media/search title="Avatar" type="movie"
    python mp-api.py POST /api/v1/download/add --json '{"torrent_url":"..."}'
    python mp-api.py DELETE /api/v1/subscribe/123

Authentication:
    The script sends the API key via the ``X-API-KEY`` header.
    It can also fall back to ``?token=`` for endpoints that require it.

Configuration priority:
    CLI flags > Environment variables > local MoviePilot settings > Config file

Config file location: ~/.config/moviepilot_api/config
"""

from __future__ import annotations

import json
import os
import ssl
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SCRIPT_NAME = os.path.basename(sys.argv[0]) if sys.argv else "mp-api.py"
SCRIPT_PATH = Path(__file__).resolve()
PROJECT_ROOT = SCRIPT_PATH.parents[3]
CONFIG_DIR = Path.home() / ".config" / "moviepilot_api"
CONFIG_FILE = CONFIG_DIR / "config"
LOCAL_HOSTS = {"0.0.0.0", "::", "::1", "", "localhost"}

# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------


def read_config() -> tuple[str, str]:
    """Return (host, apikey) from the config file."""
    host = ""
    apikey = ""
    if not CONFIG_FILE.exists():
        return host, apikey
    for line in CONFIG_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key == "MP_HOST":
            host = value
        elif key == "MP_API_KEY":
            apikey = value
    return host, apikey


def save_config(host: str, apikey: str) -> None:
    """Persist host and API key to the legacy config file."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(f"MP_HOST={host}\nMP_API_KEY={apikey}\n", encoding="utf-8")
    CONFIG_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)


def _ensure_project_import() -> None:
    """Add the MoviePilot project root to sys.path for local auto-configuration."""
    project_path = str(PROJECT_ROOT)
    if project_path not in sys.path:
        sys.path.insert(0, project_path)


def _client_host(host: str) -> str:
    """Return a loopback host usable by local clients."""
    host = (host or "").strip()
    if host in LOCAL_HOSTS:
        return "127.0.0.1"
    return host


def read_local_config() -> tuple[str, str]:
    """Return host and key from local MoviePilot settings when available."""
    try:
        _ensure_project_import()
        from app.runtime.config import settings  # pylint: disable=import-outside-toplevel
    except Exception:
        return "", ""

    host = str(settings.HOST or "")
    port = settings.PORT
    apikey = str(settings.API_TOKEN or "")
    if host and port:
        return f"http://{_client_host(host)}:{port}", apikey

    return "", apikey


def resolve_config(
    cli_host: str = "",
    cli_key: str = "",
) -> tuple[str, str]:
    """Resolve effective host and key without requiring prompt-visible secrets."""
    local_host, local_key = read_local_config()
    cfg_host, cfg_key = read_config()
    host = cli_host or os.environ.get("MP_HOST", "") or local_host or cfg_host
    apikey = cli_key or os.environ.get("MP_API_KEY", "") or local_key or cfg_key
    return host, apikey


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

# Allow self-signed certs (common in home-lab setups)
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


def http_request(
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: int = 120,
) -> tuple[int, str]:
    """Perform an HTTP request and return (status_code, response_body)."""
    headers = headers or {}
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        return 0, f"Connection error: {exc.reason}"


def build_url(host: str, path: str, query_params: dict[str, str] | None = None) -> str:
    """Build a full URL from host + path + optional query parameters."""
    base = host.rstrip("/")
    if not path.startswith("/"):
        path = "/" + path
    url = base + path
    if query_params:
        url += "?" + urllib.parse.urlencode(query_params)
    return url


# ---------------------------------------------------------------------------
# Core API call
# ---------------------------------------------------------------------------


def api_call(
    host: str,
    apikey: str,
    method: str,
    path: str,
    query_params: dict[str, str] | None = None,
    json_body: object | None = None,
    use_token_param: bool = False,
    timeout: int = 120,
) -> tuple[int, object]:
    """
    Call a MoviePilot REST API endpoint.

    Parameters
    ----------
    host : str
        MoviePilot base URL (e.g. ``http://localhost:3000``).
    apikey : str
        The API key (``settings.API_TOKEN`` value).
    method : str
        HTTP method: GET, POST, PUT, DELETE.
    path : str
        API path (e.g. ``/api/v1/media/search``).
    query_params : dict, optional
        Additional query-string parameters.
    json_body : object, optional
        A JSON-serialisable body for POST/PUT requests.
    use_token_param : bool
        If True, send the key as ``?token=`` instead of the header.
    timeout : int
        Request timeout in seconds.

    Returns
    -------
    (status_code, parsed_json_or_text)
    """
    headers: dict[str, str] = {}
    qp = dict(query_params or {})

    if use_token_param:
        qp["token"] = apikey
    else:
        headers["X-API-KEY"] = apikey

    body_bytes: bytes | None = None
    if json_body is not None:
        headers["Content-Type"] = "application/json"
        body_bytes = json.dumps(json_body, ensure_ascii=False).encode("utf-8")

    url = build_url(host, path, qp if qp else None)
    status, raw = http_request(method, url, headers, body_bytes, timeout)

    # Try to parse JSON
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        data = raw
    return status, data


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def print_json(obj: object) -> None:
    """Pretty-print a JSON-serialisable object to stdout."""
    if isinstance(obj, str):
        print(obj)
    else:
        print(json.dumps(obj, indent=2, ensure_ascii=False))


def print_usage() -> None:
    print(f"""Usage: python {SCRIPT_NAME} [options] <METHOD> <PATH> [key=value ...] [--json '<body>']
       python {SCRIPT_NAME} configure --host <HOST> --apikey <KEY>  # legacy fallback

Options:
    --host HOST       MoviePilot backend URL (auto-read locally when omitted)
    --apikey KEY      API key (auto-read locally when omitted)
    --token-param     Send key as ?token= query param instead of X-API-KEY header
    --timeout SECS    Request timeout (default: 120)
    --help            Show this help message

Methods: GET  POST  PUT  DELETE

Examples:
    python {SCRIPT_NAME} GET /api/v1/media/search title="Avatar" type="movie"
    python {SCRIPT_NAME} GET /api/v1/subscribe/
    python {SCRIPT_NAME} POST /api/v1/download/add --json '{{"torrent_url":"abc:1"}}'
    python {SCRIPT_NAME} DELETE /api/v1/subscribe/123
    python {SCRIPT_NAME} GET /api/v1/dashboard/statistic2 --token-param
""")


def main() -> None:
    argv = sys.argv[1:]
    if not argv or "--help" in argv or "-h" in argv:
        print_usage()
        sys.exit(0)

    # Parse options
    cli_host = ""
    cli_key = ""
    use_token_param = False
    timeout = 120
    positional: list[str] = []
    json_body_str: str | None = None

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--host":
            i += 1
            cli_host = argv[i] if i < len(argv) else ""
        elif arg == "--apikey":
            i += 1
            cli_key = argv[i] if i < len(argv) else ""
        elif arg == "--token-param":
            use_token_param = True
        elif arg == "--timeout":
            i += 1
            timeout = int(argv[i]) if i < len(argv) else 120
        elif arg == "--json":
            i += 1
            json_body_str = argv[i] if i < len(argv) else "{}"
        else:
            positional.append(arg)
        i += 1

    # Sub-command: configure
    if positional and positional[0].lower() == "configure":
        if not cli_host and not cli_key:
            print(
                "Error: --host and --apikey are required for configure", file=sys.stderr
            )
            sys.exit(1)
        cfg_host, cfg_key = read_config()
        save_config(cli_host or cfg_host, cli_key or cfg_key)
        print("Configuration saved.")
        sys.exit(0)

    # Normal API call
    if len(positional) < 2:
        print("Error: expected <METHOD> <PATH>", file=sys.stderr)
        print_usage()
        sys.exit(1)

    method = positional[0].upper()
    path = positional[1]

    # Remaining positional args are key=value query params
    query_params: dict[str, str] = {}
    for kv in positional[2:]:
        if "=" in kv:
            k, _, v = kv.partition("=")
            query_params[k] = v
        else:
            print(f"Warning: ignoring argument without '=': {kv}", file=sys.stderr)

    # Parse JSON body
    json_body = None
    if json_body_str:
        try:
            json_body = json.loads(json_body_str)
        except json.JSONDecodeError as exc:
            print(f"Error: invalid JSON body: {exc}", file=sys.stderr)
            sys.exit(1)

    # Resolve config
    host, apikey = resolve_config(cli_host, cli_key)
    if not host:
        print("Error: backend host is not configured.", file=sys.stderr)
        print("  Use: --host HOST or set MP_HOST environment variable", file=sys.stderr)
        sys.exit(1)
    if not apikey:
        print("Error: API key is not configured.", file=sys.stderr)
        print(
            "  Use: --apikey KEY or set MP_API_KEY environment variable",
            file=sys.stderr,
        )
        sys.exit(1)

    # Persist if CLI flags provided
    if cli_host or cli_key:
        save_config(host, apikey)

    status, data = api_call(
        host=host,
        apikey=apikey,
        method=method,
        path=path,
        query_params=query_params if query_params else None,
        json_body=json_body,
        use_token_param=use_token_param,
        timeout=timeout,
    )

    if status and status not in (200, 201):
        print(f"HTTP {status}", file=sys.stderr)

    print_json(data)
    if status and status >= 400:
        sys.exit(1)


if __name__ == "__main__":
    main()
