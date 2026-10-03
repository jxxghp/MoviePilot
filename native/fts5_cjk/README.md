# Hermes CJK FTS5 tokenizer

Vendored unchanged from NousResearch/hermes-agent at
`f42f579cf8bac4918ac9599bece71618afadd846`, `native/fts5_cjk/fts5_cjk.c`.
Author: Soju06 (upstream PR #65544). MIT license: LICENSE.hermes.
SQLite headers in vendor/ are public domain, as stated in those files.
Trailing whitespace in sqlite3ext.h is normalized; tokenizer code is unchanged.

The tokenizer wraps unicode61 and emits overlapping CJK bigrams. Without
it, queries use trigram or a timed LIKE fallback. The C source is unchanged;
`install.py` is MoviePilot's installation and verification entrypoint.

## Automatic installation

- Docker builds compile for the image's target architecture in `prepare_cjk`.
  The selected runtime Python (standard or free-threaded) verifies the extension
  before it is included at `/opt/venv/lib/moviepilot/libfts5_cjk.so`. No compiler
  is added to the final image, and `/config` mounts do not hide the library.
- CLI `setup`, `install deps`, and backend updates compile and verify it using
  the selected virtual environment. An unchanged, working build is reused.
  Bootstrap installs the Linux C toolchain when necessary. Missing compilers or
  incompatible SQLite builds produce an explicit warning and retain search fallback.

To install or check an existing CLI environment:

```bash
moviepilot install cjk --venv /path/to/venv
moviepilot install cjk --venv /path/to/venv --check
```

Debian/Ubuntu needs `build-essential`; macOS needs Xcode Command Line Tools
(`xcode-select --install`); Windows needs a matching MSVC developer terminal or
MinGW compiler. `CC` can select the compiler. Python's SQLite must support both
FTS5 and extension loading. Successful verification actually indexes
`电影订阅成功` and matches `订阅` in a temporary in-memory database.

To check a newly built Docker image/container:

```bash
docker exec <container> /opt/venv/bin/python /app/native/fts5_cjk/install.py --check
```

Existing images need to be rebuilt/replaced to receive the packaged extension;
updating Python source alone does not install a native library in an old image.

## Runtime loading

The loader first tries `<sys.prefix>/lib/moviepilot/libfts5_cjk.so`, then the
legacy `<config>/agent/runtime/lib/libfts5_cjk.so` for manual installations.
Only these fixed local paths are loaded; tools cannot provide extension paths.
No compiler is invoked by an Agent request. Never copy binaries across operating
systems or architectures. Configuration backups do not need to contain the library.

Older messages are indexed in background batches after installation. A completed
history query reports `index_status.messages_fts_cjk=true`; eligible two-character
queries report `search_path="cjk"`. Installation verification does not claim that
every user's historical index has already finished building.
