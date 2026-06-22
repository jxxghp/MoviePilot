# 06 — Code Standards and Style

## General Principles

- Preserve the style of the surrounding file. When in doubt, read neighboring code first.
- Prefer the smallest correct change. Do not introduce a new abstraction layer without a clear payoff.
- Do not add features, refactors, or abstractions beyond what the task requires.
- Do not add error handling or validation for scenarios that cannot happen. Trust internal code and framework guarantees; only validate at system boundaries (user input, external API responses).

---

## Python Version and Typing

- Target: **Python 3.11+**. CI runs Python 3.12.
- **Type annotations are required** on all public methods and function signatures.
- Use `Optional[X]` for nullable types (do not use `X | None` — keep consistency with the existing codebase style).
- Use `Union[X, Y]` for multi-type parameters.
- Prefer `list[X]`, `dict[K, V]`, `tuple[X, Y]` built-in generics in new code (Python 3.9+); match the style of the surrounding file.
- Use `pathlib.Path` for all file path operations. Never use raw string concatenation for paths.

---

## Pydantic Models

- All request body and response models must be defined as Pydantic `BaseModel` subclasses in `app/schemas/`.
- Use `Field(...)` for required fields; use `Field(default=...)` or `Field(None)` for optional fields.
- Do not define ad-hoc `dict` return types for API responses — define a schema class.
- Settings and deployment configuration live in `ConfigModel` / `Settings` in `app/core/config.py` using `pydantic-settings`.
- Use `model_validator` for cross-field validation logic.

---

## Async and Concurrency

- Prefer `async def` for I/O-bound operations (network requests, database queries, file operations).
- Use `await` consistently; do not mix sync and async code paths in the same function without using `run_in_threadpool` from FastAPI or `asyncio.to_thread`.
- For CPU-bound work that must not block the event loop, submit to `ThreadHelper` (see `app/helper/thread.py`).
- Do not use bare `threading.Thread` in new code; use `ThreadHelper.submit()`.

---

## Imports

Order imports as follows, separated by blank lines:

1. Standard library (`import os`, `import json`, etc.)
2. Third-party packages (`from fastapi import ...`, `from pydantic import ...`)
3. Local application packages (`from app.chain import ...`, `from app.schemas import ...`)

Within each group, sort alphabetically. Do not use wildcard imports (`from module import *`) in application code.

---

## String Formatting

- Use **f-strings** for all string interpolation. Do not use `%` formatting or `.format()`.
- For log messages, use `logger.info(f"...")` — do not use lazy `%s` format in logger calls (the project does not rely on lazy evaluation here).

---

## Error Handling

- In **chain and module layers**: do not raise HTTP exceptions. Catch exceptions, log them, and return `None` or a domain-level error object so the caller can decide how to proceed.
- In **endpoint layer**: use FastAPI's `HTTPException` or the project's standard response schemas for errors.
- Never swallow exceptions silently. At minimum log the error with `logger.error(f"...: {str(err)}")`.
- Do not use bare `except:` — always catch a specific exception type or at minimum `Exception`.

```python
# Correct
try:
    result = self.do_work()
except Exception as err:
    logger.error(f"Failed to do work: {str(err)}")
    return None

# Wrong — swallowing silently
try:
    result = self.do_work()
except:
    pass
```

---

## Logging

- Use `logger` from `app/log.py`. Do not import the standard library `logging` directly in application code.
- Log levels:
  - `logger.debug(...)` — detailed diagnostic information, disabled by default.
  - `logger.info(...)` — normal operational events.
  - `logger.warning(...)` — unexpected but recoverable situations.
  - `logger.error(...)` — failures that affect functionality.
- Keep log messages in Chinese unless the surrounding file consistently uses English.

---

## Constants and Magic Values

- Do not scatter raw string keys for `SystemConfig`. Add a `SystemConfigKey` enum entry and reference it.
- Do not use magic numbers or magic strings inline. Define a named constant or enum value.

---

## File Organization

- One primary class per file is the norm for chains, modules, and helpers.
- Private helper functions in the same file are preferable to extracting a new helper for single-use logic.
- Keep files focused on one domain concern.

---

## What Not To Do

- Do not introduce new third-party libraries without placing them in the correct dependency entry: runtime packages in `requirements.in`, test/lint/build tooling in `requirements-dev.in`.
- Do not use `requests` or `httpx` directly for external HTTP calls — use `RequestUtils` from `app/utils/http.py`.
- Do not issue raw SQLAlchemy queries from chains, modules, or endpoints — use the `*_oper.py` classes.
- Do not add TODO or FIXME without context. Only keep one if it is genuinely deferred and cannot be addressed in the current task.
- Do not add noisy markers like `# change starts here`, `# important`, or `# this is a fix`.
- Do not write comments that restate what the code already clearly says.

*Last Updated: 2026-05-25*
