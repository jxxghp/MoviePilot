# Hermes CJK FTS5 tokenizer

Vendored unchanged from NousResearch/hermes-agent at
`f42f579cf8bac4918ac9599bece71618afadd846`, `native/fts5_cjk/fts5_cjk.c`.
Author: Soju06 (upstream PR #65544). MIT license: LICENSE.hermes.
SQLite headers in vendor/ are public domain, as stated in those files.
Trailing whitespace in sqlite3ext.h is normalized; tokenizer code is unchanged.

The tokenizer wraps unicode61 and emits overlapping CJK bigrams. It is
optional exactly as in Hermes; without it, queries use trigram or a timed
LIKE fallback. Compile for the host architecture (never copy binaries
across architectures) and install under the Agent runtime lib directory:

```bash
cc -shared -fPIC -O2 -Wall -Wextra -Inative/fts5_cjk/vendor \
  native/fts5_cjk/fts5_cjk.c -o /path/to/config/agent/runtime/lib/libfts5_cjk.so
```

Only this fixed local path is loaded; tools cannot provide extension paths.
No compiler is invoked by an Agent request.
