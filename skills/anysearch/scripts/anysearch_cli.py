#!/usr/bin/env python3
"""AnySearch CLI - Unified search client for AnySearch API."""

import argparse
import io
import json
import os
import sys
import requests

if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding != "utf-8":
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

ENDPOINT = "https://api.anysearch.com/mcp"


def _load_env():
    """Load API keys from .env files near the skill.

    The documented priority is:
    --api_key > .env file > environment variable > anonymous.

    Use utf-8-sig so .env files saved by Windows Notepad with a BOM are parsed
    correctly. The .env value intentionally overrides an existing environment
    variable to match the documented priority order.
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    for env_path in [os.path.join(script_dir, ".env"), os.path.join(script_dir, "..", ".env")]:
        if os.path.isfile(env_path):
            with open(env_path, "r", encoding="utf-8-sig") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    key = key.strip().lstrip(chr(0xFEFF))
                    value = value.strip().strip("\"'").strip()
                    if key and value:
                        os.environ[key] = value


_load_env()


# BEGIN GENERATED:CONSTANTS
AVAILABLE_DOMAINS = [
    "general", "resource", "social_media", "finance", "academic", "legal",
    "health", "business", "security", "ip", "code", "energy",
    "environment", "agriculture", "travel", "film", "gaming",
]
# END GENERATED:CONSTANTS


def _build_headers(api_key: str) -> dict:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _call_api(tool_name: str, arguments: dict, api_key: str) -> str:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }
    try:
        resp = requests.post(ENDPOINT, json=payload, headers=_build_headers(api_key), timeout=30)
        resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        print(f"HTTP Error: {e}", file=sys.stderr)
        try:
            detail = resp.json()
            print(f"Response: {json.dumps(detail, ensure_ascii=False)}", file=sys.stderr)
        except Exception:
            print(f"Response body: {resp.text[:500]}", file=sys.stderr)
        sys.exit(1)
    except requests.exceptions.ConnectionError:
        print("Connection Error: Unable to reach the API endpoint.", file=sys.stderr)
        sys.exit(1)
    except requests.exceptions.Timeout:
        print("Timeout: The API request timed out.", file=sys.stderr)
        sys.exit(1)

    data = resp.json()
    if "error" in data:
        error_msg = data["error"].get("message", str(data["error"]))
        print(f"API Error: {error_msg}", file=sys.stderr)
        sys.exit(1)
    result = data.get("result", {})
    content = result.get("content", [])
    for item in content:
        if item.get("type") == "text":
            return item.get("text", "")
    return json.dumps(result, indent=2, ensure_ascii=False)


def _parse_json_list(value: str) -> list:
    try:
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return parsed
        return [parsed]
    except json.JSONDecodeError:
        return [s.strip() for s in value.split(",") if s.strip()]


def cmd_search(args):
    """Execute search (general or vertical)."""
    arguments = {"query": args.query}

    if args.domain:
        arguments["domain"] = args.domain
        if args.sub_domain:
            arguments["sub_domain"] = args.sub_domain
        if args.sub_domain_params:
            try:
                arguments["sub_domain_params"] = json.loads(args.sub_domain_params)
            except json.JSONDecodeError:
                print("Error: --sub_domain_params must be valid JSON", file=sys.stderr)
                sys.exit(1)

    if args.max_results is not None:
        arguments["max_results"] = min(args.max_results, 10)

    print(_call_api("search", arguments, args.api_key))


def cmd_get_sub_domains(args):
    """List available sub_domains for given domain(s)."""
    arguments = {}
    if args.domains:
        arguments["domains"] = _parse_json_list(args.domains)
    elif args.domain:
        arguments["domain"] = args.domain
    else:
        print("Error: provide --domain or --domains", file=sys.stderr)
        sys.exit(1)

    print(_call_api("get_sub_domains", arguments, args.api_key))


def cmd_extract(args):
    """Fetch and extract full page content from a URL."""
    url = args.url or getattr(args, "url_opt", None)
    if not url:
        print("Error: url is required", file=sys.stderr)
        sys.exit(1)
    arguments = {"url": url}
    print(_call_api("extract", arguments, args.api_key))


def _repair_json(raw: str) -> list:
    raw = raw.strip()
    if raw.startswith("{") and not raw.startswith("["):
        raw = "[" + raw + "]"
    if raw.startswith("["):
        content = raw.strip("[]")
        if not content:
            return []
        items = _split_json_items(content)
        queries = []
        for item in items:
            item = item.strip().strip(",")
            if not item:
                continue
            if item.startswith("{"):
                d = _repair_json_object(item)
                queries.append(d)
            else:
                s = item.strip().strip("'\"")
                queries.append({"query": s})
        return queries
    return [{"query": raw.strip().strip("'\"")}]


def _split_json_items(s: str) -> list:
    depth = 0
    current = []
    items = []
    for ch in s:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        if ch == "," and depth == 0:
            items.append("".join(current))
            current = []
        else:
            current.append(ch)
    if current:
        tail = "".join(current).strip()
        if tail:
            items.append(tail)
    return items


def _repair_json_object(s: str) -> dict:
    inner = s.strip().strip("{}").strip()
    if not inner:
        return {}
    pairs = _split_json_items(inner)
    result = {}
    for pair in pairs:
        pair = pair.strip().strip(",")
        if not pair:
            continue
        if ":" not in pair:
            continue
        colon = pair.index(":")
        key = pair[:colon].strip().strip("'\"")
        val = pair[colon + 1:].strip()
        if val.startswith("{"):
            try:
                result[key] = json.loads(val)
            except json.JSONDecodeError:
                result[key] = _repair_json_object(val)
        elif val.startswith("["):
            try:
                result[key] = json.loads(val)
            except json.JSONDecodeError:
                result[key] = val.strip("[]").split(",")
        elif val.lower() in ("true", "false"):
            result[key] = val.lower() == "true"
        elif val.lower() == "null":
            result[key] = None
        else:
            try:
                result[key] = json.loads(val)
            except (json.JSONDecodeError, ValueError):
                result[key] = val.strip("'\"")
    return result


def cmd_batch_search(args):
    """Execute multiple search queries in parallel (2-5 queries)."""
    query_items = getattr(args, "query_items", None) or []
    raw = args.queries or getattr(args, "queries_opt", None)

    if query_items:
        queries = [{"query": q} for q in query_items]
        if len(queries) > 5:
            print("Error: batch_search supports a maximum of 5 queries", file=sys.stderr)
            sys.exit(1)
    elif raw:
        if raw.startswith("@"):
            file_path = raw[1:]
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    raw = f.read()
            except FileNotFoundError:
                print(f"Error: file not found: {file_path}", file=sys.stderr)
                sys.exit(1)
        try:
            queries = json.loads(raw)
            if not isinstance(queries, list):
                queries = [queries]
        except json.JSONDecodeError:
            queries = _repair_json(raw)
        if len(queries) < 1:
            print("Error: queries must contain at least 1 item", file=sys.stderr)
            sys.exit(1)
        if len(queries) > 5:
            print("Error: batch_search supports a maximum of 5 queries", file=sys.stderr)
            sys.exit(1)
    else:
        print("Error: provide --queries or --query", file=sys.stderr)
        sys.exit(1)

    arguments = {"queries": queries}
    print(_call_api("batch_search", arguments, args.api_key))


# BEGIN GENERATED:DOC_SPEC
def _render_doc():
    import json as _json
    _dir = os.path.dirname(os.path.abspath(__file__))
    _shared = os.path.join(_dir, "shared")
    with open(os.path.join(_shared, "doc_spec.md"), "r", encoding="utf-8") as _f:
        _tpl = _f.read()
    with open(os.path.join(_shared, "constants.json"), "r", encoding="utf-8") as _f:
        _c = _json.load(_f)
    _tpl = _tpl.replace("{{LANG_NAME}}", "Python")
    _tpl = _tpl.replace("{{LANG_CODEBLOCK}}", "")
    _tpl = _tpl.replace("{{LANG_INVOKE}}", "python scripts/anysearch_cli.py")
    _tpl = _tpl.replace("{{DOMAINS_SPACE}}", " ".join(_c["available_domains"]))
    return _tpl
# END GENERATED:DOC_SPEC


def cmd_doc(args):
    print(_render_doc())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="anysearch",
        description=(
            "AnySearch CLI - Unified real-time search client.\n\n"
            "Supports general search, vertical domain search, batch search,\n"
            "domain directory lookup, and URL content extraction via the\n"
            "AnySearch JSON-RPC API."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  anysearch search \"quantum computing\"\n"
            "  anysearch search \"AAPL\" --domain finance --sub_domain finance.us_stock\n"
            "  anysearch get_sub_domains --domain finance\n"
            "  anysearch extract --url https://example.com\n"
            "  anysearch batch_search --queries '[{\"query\":\"AAPL\"},{\"query\":\"GOOG\"}]'\n"
        ),
    )

    parser.add_argument(
        "--api_key",
        default=os.environ.get("ANYSEARCH_API_KEY", ""),
        help="API key for authentication. Read from: --api_key > .env ANYSEARCH_API_KEY > env ANYSEARCH_API_KEY. "
        "Without a key, anonymous access is used with lower rate limits.",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    search_p = subparsers.add_parser(
        "search",
        help="Search the web (general or vertical domain search)",
        description=(
            "Execute a search query.\n\n"
            "Two modes:\n"
            "  General search:   omit --domain (open-ended natural language queries)\n"
            "  Vertical search:  specify --domain and --sub_domain for structured queries\n\n"
            "For vertical search, run 'get_sub_domains' first to discover available\n"
            "sub_domains and their required query formats."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    search_p.add_argument("query", help="Search query string. For vertical search, follow the format returned by get_sub_domains.")
    search_p.add_argument(
        "--domain", "-d",
        choices=AVAILABLE_DOMAINS,
        help=(
            "Vertical domain for structured search. "
            f"Available: {', '.join(AVAILABLE_DOMAINS)}"
        ),
    )
    search_p.add_argument(
        "--sub_domain", "-s",
        help="Sub-domain routing key (e.g. finance.us_stock). Required for vertical search; obtain via get_sub_domains.",
    )
    search_p.add_argument(
        "--sub_domain_params",
        help="Additional sub_domain parameters as JSON string. Schema depends on the sub_domain (see get_sub_domains output).",
    )
    search_p.add_argument(
        "--max_results", "-m",
        type=int,
        help="Maximum number of results to return (1-10, default 10).",
    )
    search_p.set_defaults(func=cmd_search)

    ld_p = subparsers.add_parser(
        "get_sub_domains",
        help="Query domain directory for available sub_domains",
        description=(
            "List available sub_domains, query formats, and parameter schemas\n"
            "for one or more vertical domains.\n\n"
            "MUST be called before performing vertical search to obtain\n"
            "the correct sub_domain value and query_format.\n\n"
            "Results are returned as a Markdown table with columns:\n"
            "domain, sub_domain, description, query_format, params_schema, zone."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ld_p.add_argument(
        "--domain",
        choices=AVAILABLE_DOMAINS,
        help="Single domain to query.",
    )
    ld_p.add_argument(
        "--domains",
        help=(
            "Batch query up to 5 domains. Comma-separated or JSON array.\n"
            f"Available: {', '.join(AVAILABLE_DOMAINS)}\n"
            "Takes precedence over --domain."
        ),
    )
    ld_p.set_defaults(func=cmd_get_sub_domains)

    ext_p = subparsers.add_parser(
        "extract",
        help="Fetch full page content from a URL",
        description=(
            "Extract the full content of a web page and return it as Markdown.\n\n"
            "Use this when search snippets are insufficient, you need to verify\n"
            "data, or want to extract structured content (tables, code, etc.).\n\n"
            "Note: Output is truncated at 50,000 characters. Only HTML pages\n"
            "are supported (not PDFs, images, etc.)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ext_p.add_argument("url", nargs="?", help="Target URL to extract content from (http(s)://).")
    ext_p.add_argument("--url", "-u", dest="url_opt", help="Target URL to extract content from (alternative to positional arg).")
    ext_p.set_defaults(func=cmd_extract)

    batch_p = subparsers.add_parser(
        "batch_search",
        help="Execute 2-5 search queries in parallel",
        description=(
            "Run multiple independent search queries in a single API call.\n"
            "Each query follows the same parameter structure as the 'search' command.\n"
            "A single query failure does not block others; results are merged.\n\n"
            "Queries are provided as a JSON array of objects. Each object supports\n"
            "the same fields as 'search': query, domain, sub_domain, max_results."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            '  anysearch batch_search --query AAPL --query GOOG\n'
            '  anysearch batch_search --queries \'[{\"query\":\"AAPL\"},{\"query\":\"GOOG\"}]\'\n'
            '  anysearch batch_search \'[{\"query\":\"AAPL\"},{\"query\":\"GOOG\"}]\'\n'
            '  anysearch batch_search --queries @queries.json\n'
        ),
    )
    batch_p.add_argument(
        "queries",
        nargs="?",
        help=(
            'JSON array of search query objects (1-5 items). '
            'Tolerates PowerShell quote-stripping automatically.\n'
            'Each object supports: query (required), domain, sub_domain, sub_domain_params, max_results.\n'
            'Example: \'[{"query":"AAPL"},{"query":"GOOG"}]\''
        ),
    )
    batch_p.add_argument(
        "--queries", "-q", dest="queries_opt",
        help="JSON array of search query objects (alternative to positional arg). Prefix @ to read from file.",
    )
    batch_p.add_argument(
        "--query",
        action="append",
        dest="query_items",
        help="Shorthand: repeatable single-query string. Easier for PowerShell. Up to 5.",
    )
    batch_p.set_defaults(func=cmd_batch_search)

    doc_p = subparsers.add_parser(
        "doc",
        help="Print AI-facing interface specification",
    )
    doc_p.set_defaults(func=cmd_doc)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    if args.command is None:
        print(_render_doc())
        sys.exit(0)
    args.func(args)


if __name__ == "__main__":
    main()
