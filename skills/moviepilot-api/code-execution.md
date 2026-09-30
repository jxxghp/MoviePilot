# Read-only Python aggregation

When `execute_code` is available, use it for three or more read-only calls with
paging, joins, filtering, or aggregation. Load this domain skill first. Import
`moviepilot_api` from `moviepilot_tools` and call it with the same operation ID
and argument buckets shown above; helpers return parsed JSON. Inspect each
response's success and pagination structure before aggregating and print only
the useful result. The host checks every call automatically; no additional user
approval is needed for these read-only calls. Writes and sensitive reads are not
available through Python RPC. Only currently enabled built-in helpers exist.

Python variables survive consecutive cells. Check `kernel.reused` and
`execution_count`; reset, timeout, cancellation, explicit exit, or an evicted
kernel loses that state. Each cell has 300 seconds and 50 tool calls. Inspect
`tool_errors` even when `exit_code` is 0. Read an oversized stdout spill in line
slices with `read_file(file_path=..., start_line=..., end_line=...)`; do not rerun
the program solely to recover output. This administrator capability is host
Python, not an OS sandbox, and is absent from external MCP/HTTP tool catalogs.
