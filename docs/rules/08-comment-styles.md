# 08 — Comments and Documentation Style

## Scope and Single Source of Truth

This document is the sole detailed source for comment style across production code, scripts, tests, and code examples in documentation.
New and modified comments must comply even when neighboring code uses an older format or English prose.
Existing noncompliant comments remain migration work, not an alternative accepted style. Migrate within the task scope; unrelated changes do not require rewriting an entire file.

- All human-authored comments, docstrings, field descriptions, and API descriptions must use Chinese. Preserve code identifiers, product names, protocol names, URLs, format labels, and tool directives such as `MediaInfo`, `HTTP`, `:param`, and `# type: ignore`.
  Explanations accompanying tool directives must be Chinese. Adding a Chinese word to otherwise English prose does not satisfy this requirement.
- All docstrings use triple double quotes, with each opening and closing `"""` on its own line, even for a one-sentence description. Single-line docstrings and triple-single-quoted docstrings are prohibited. Ordinary `#` comments may occupy a single line.
- Use reStructuredText / Sphinx field lists consistently: `:param name:`, `:return:`, `:raises ExceptionType:`, `:ivar name:`, and `:cvar name:`. These are not the Google-style `Args:` / `Returns:` sections; do not mix the formats.
- Triple-quoted SQL, templates, prompts, and other runtime strings are not docstrings. Do not rewrite their content or formatting as comment maintenance. Shebangs, encoding declarations, and suppression directives are machine-readable syntax and must not be translated or broken.
- The Chinese requirement governs source comments and contract descriptions. It does not require translating English rule documents such as this one; their code examples must still comply.

## Content and Layout

1. Start with a short Chinese description of the purpose. Add a separate paragraph for business rules, boundaries, and behavior callers need to understand.
2. Use familiar words and short sentences that identify the object, action, and conditions. Avoid jargon piles, vague slogans, or merely repeating a class or function name.
3. Separate the summary, detailed explanation, and field list with blank lines. Do not insert blank lines between consecutive parameter or field entries.
4. Use Chinese sentence punctuation for complete sentences. Short phrase-style parameter and field descriptions do not need a final period; keep each list consistent.
5. Wrap long explanations at meaningful boundaries, indent continuation lines consistently, and respect the project line width. Do not use decorative borders, walls of asterisks, or excessive blank lines.
6. Do not duplicate existing type annotations mechanically with `:type:` / `:rtype:`. Explain business meaning instead of translating `int` into a word for integer.
7. Explain units, allowed values, default behavior, null semantics, and failure behavior where applicable. Each declared item needs useful information, not necessarily a long paragraph or an empty checklist of irrelevant concerns.

## Required Documentation

| Object | Requirement |
|---|---|
| Python module with substantive content | A module docstring describing its responsibility and, where needed, boundaries and constraints |
| Public class, function, or method | A docstring describing purpose and calling contract; this includes `ChainBase`, `_ModuleBase`, and endpoint implementations |
| Private implementation | Document non-obvious business behavior, cross-module contracts, lifecycle, compatibility, and side effects; small self-evident private helpers may omit docstrings |
| Every dataclass | Document the class, every declared field, and every explicitly defined method, including private classes |
| Every Pydantic model | Document the class, every declared field, validators, and explicitly defined methods, including request, response, configuration, and internal models |
| Every Protocol | Document the class, every method, property, and declared member, including private interfaces |
| Enum / TypedDict | Document the overall purpose and every member or field |
| Constant | Explain non-obvious meaning, units, source, or the reason for its value |
| Tests | Apply the same language and format rules; small self-evident test helpers may omit docstrings |

Test scaffolding and local structures do not exempt dataclasses, Pydantic models, or Protocols from item-by-item documentation.
Unchanged inherited members retain their base documentation without duplication. Redeclarations, overrides, and semantic changes require corresponding documentation updates.
The rule against restating code does not waive required documentation: explain each item's business meaning.

## Module Headers and Class Descriptions

Place the module docstring before imports. Necessary shebangs and encoding declarations may precede it; `from __future__` follows it.
Describe the current responsibility rather than requiring author names, dates, or change history. Empty package marker files do not need added documentation; existing architecture requirements for docstring-only package roots still apply.

```python
"""
下载任务的重试策略与进度结构。

本模块只表达重试规则和进度数据，不负责发起下载请求。
"""
```

Ordinary class docstrings describe responsibility and collaboration boundaries, without `:return:`.
Document non-obvious initialization behavior in constructors and value, fallback, or side-effect behavior in properties.
A brief module header does not replace documentation of public contracts.

## Functions and Methods

Even a simple description without parameter or return semantics to expand uses the multiline format:

```python
def stop(self) -> None:
    """
    停止接收新的下载任务。
    """
```

Use `:param name:` for parameters that need explanation, `:return:` for results, and `:raises ExceptionType:` for exceptions callers must handle.
Protocol methods must explain each parameter and the return contract; ordinary functions expand the items with meaningful business semantics.
Do not document `self` or `cls` as parameters. Methods returning no data do not need a mechanical `:return: None` entry.
Use `:yields:` for generator items. Async methods describe the result after awaiting, not the coroutine object as a business result.

The following example illustrates a calling contract; the implementation is omitted:

```python
def find_task(self, task_id: str) -> Optional[DownloadTask]:
    """
    按任务标识查询当前下载任务。

    仅查询本地任务记录，不向下载器发起请求。

    :param task_id: 下载器分配的任务标识，不接受空字符串
    :return: 匹配的任务；记录不存在时返回 None
    :raises ValueError: 任务标识为空
    """
    ...
```

Document caller-relevant constraints involving input mutation, persistence, transaction commits, resource acquisition and release, call ordering, concurrency, idempotency, and compatibility fallbacks.
List actual exception contracts rather than every exception an implementation detail could theoretically raise.

## Dataclasses

Every dataclass must have a multiline class docstring with a centralized description of every declared field:

- Use `:param field_name:` for initialization parameters and `InitVar` inputs.
- Use `:ivar field_name:` for instance fields excluded from initialization with `init=False`. When automatic initialization is disabled or a custom constructor exists, distinguish parameters and attributes using the actual constructor signature; do not label non-input attributes as parameters.
- Use `:cvar name:` for `ClassVar` members, not initialization parameter entries.
- Keep field descriptions in the class docstring; do not repeat them in per-field `#` comments.
- Explicit methods, properties, and `__post_init__` require multiline docstrings. Do not add implementations merely to document generated methods.

```python
from dataclasses import dataclass, field


@dataclass
class DownloadProgress:
    """
    下载任务的进度信息。

    已下载字节数从零开始，由进度更新逻辑维护。

    :param total_bytes: 文件总字节数；为 0 表示尚未获取文件大小
    :ivar downloaded_bytes: 已下载字节数，初始化为 0，不接受构造参数
    """

    total_bytes: int
    downloaded_bytes: int = field(default=0, init=False)
```

## Pydantic Models and Fields

Every model must have a multiline class docstring describing its purpose and cross-field constraints.
Every declared model field must use `Field(description="中文说明")`, preserving its actual requiredness, defaults, aliases, and constraints.
Neither a `#` comment nor a class-level `:param:` entry replaces a field description: they do not populate JSON Schema / OpenAPI field documentation by default.
Do not maintain a duplicate complete field list in the class docstring.

```python
from pydantic import BaseModel, Field


class SearchRequest(BaseModel):
    """
    媒体搜索请求。
    """

    keyword: str = Field(
        ...,
        min_length=1,
        description="搜索关键词，至少包含一个字符",
    )
    limit: int = Field(
        default=20,
        ge=1,
        le=100,
        description="最多返回的结果数量，默认 20 条，范围为 1 至 100 条",
    )
```

Explain units, allowed values, defaults, and the difference between missing and null values where relevant; do not simply repeat the English field name.
A `description` value is a metadata string and may be single-line; it is not subject to the multiline docstring rule.
Existing `Annotated` declarations may provide the description in their `Field` metadata. Do not change declaration semantics as part of comment migration.
Document private attributes and class variables using `:ivar:` and `:cvar:` in the class docstring rather than turning them into model fields.
Computed fields require both a Chinese Schema description and a property docstring, serving API consumers and code callers respectively.

Every validator and explicitly defined method must have a multiline docstring.
Validators explain validation rules, normalization, cross-field relationships, and failure conditions instead of merely saying that they validate parameters.
Method parameters, results, and exceptions use the shared field-list format.

## Protocol Interfaces

Every interface class describes its responsibility and implementation boundary.
Use `:ivar:` for declared instance members and `:cvar:` for class variables.
Every method has its own multiline docstring explaining each parameter, its return contract, and applicable exceptions and side effects.
Properties describe their values with `:return:` in the property method docstring rather than duplicating the member description.

```python
from typing import Protocol


class SubscriptionRepository(Protocol):
    """
    订阅记录的持久化接口。

    实现方负责查询和暂存修改，事务由调用方提交或回滚。
    """

    def delete(self, subscription_id: int) -> bool:
        """
        暂存指定订阅的删除操作。

        记录不存在时不修改数据；本方法不提交事务。

        :param subscription_id: 订阅记录的数据库主键
        :return: 找到并暂存删除时返回 True，记录不存在时返回 False
        """
        ...
```

Make transaction ownership, resource release, concurrency limits, idempotency, and failure behavior explicit where they affect interface use.
Do not leave implementers to guess, add irrelevant empty sections, or invent new contracts merely to fill a format.

## Other Structures and API Documentation

- TypedDict classes document every field using `:ivar field_name:` in the class docstring, distinguishing missing keys from null values where needed.
- Enum classes describe their purpose in the class docstring and each member's business meaning in a `#` comment above its declaration.
- Non-obvious constants have preceding comments explaining units, source, or value rationale. Ordinary local variables do not each need a comment.
- API `summary` and `description` values must be Chinese. Endpoint docstrings use the shared multiline and field-list format. Summaries describe the operation; details explain permissions, limitations, and special behavior.
- SDK capabilities, compatibility paths, and lifecycle behavior require documentation of their calling contracts and necessary compatibility rationale.

## Ordinary Comments and Prohibited Patterns

Place explanatory `#` comments above the related code, not at the end of the line.
Directives that must attach to a specific line, such as `# type: ignore[...]`, `# noqa`, and `# pylint: disable=...`, may remain inline; explain their necessity in Chinese immediately above.
Do not add suppressions merely to clear diagnostics; the [quality rules](11-quality-and-security.md) still apply.

Explain external limitations, business invariants, third-party bugs, initialization order, and compatibility reasons. For example:

```python
# 下载器刚接受任务时可能尚未完成索引，此时空结果不代表任务不存在
result = client.get_torrent(task_id)
```

Prohibited patterns:

- Translating straightforward assignments, branches, or calls into comments, or adding empty statements of importance.
- Editorial markers describing where a change starts or announcing a fix; change history belongs in version control.
- Commented-out dead code.
- Single-line docstrings, English explanations, Google / NumPy sections, or appeals to an older file style to bypass these rules.
- Context-free TODO / FIXME markers. If deferral is genuinely unavoidable, explain the reason, the condition for follow-up, and a traceable issue or owner.

Update or remove stale comments when implementation changes.
Do not promise validation, thread safety, idempotency, or exception behavior that the code does not provide.

## Submission and Review

Check the objects added or modified by the task before submission:

- Chinese explanations, multiline triple double quotes, field lists, punctuation, and placement comply with this document.
- Required module, class, function, and method documentation is present.
- Every declared dataclass, Pydantic, and Protocol item has a matching explanation, with parameter and field names matching the code.
- Pydantic field descriptions reach the Schema; comment changes have not altered defaults, constraints, aliases, or serialization behavior.
- Documentation reflects actual contracts without vague placeholders, duplicate maintenance, or stale explanations.

These are submission and code-review requirements. The current Pylint / Ruff configuration does not automatically enforce every comment rule.
Passing static checks does not replace this review, and updating the rules does not establish that all existing code has been migrated.

*Last Updated: 2026-10-17*
