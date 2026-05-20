---
name: feedback-issue
version: 1
description: >-
  Use this skill when the user wants to file a bug report against the
  MoviePilot upstream backend repository `jxxghp/MoviePilot`. Triggers
  include Chinese phrases such as "反馈 issue"、"提 issue"、"报 bug"、
  "给 MP 提 issue"、"让上游修一下"、"我要反馈问题"、"提交错误报告"，
  as well as English phrasings such as "file an issue" / "report a bug" /
  "open an upstream issue". The skill collects bug context from the
  conversation, drafts an issue payload that matches the upstream
  `bug_report.yml` form, asks the user to confirm, then calls the
  `submit_feedback_issue` tool which either creates the issue directly
  via GitHub REST API (when `GITHUB_TOKEN` has write permission) or
  falls back to a prefilled GitHub Issue Forms URL for the user to
  submit manually. Backend issues only — redirect frontend / plugin
  reports to their own repositories.
allowed-tools: submit_feedback_issue read_file list_directory execute_command
---

# Feedback Issue (问题反馈)

This skill turns a user-reported backend problem from a chat session
(Telegram, Lark/Feishu, WeCom, Slack, web, etc.) into a properly
structured GitHub issue against the upstream `jxxghp/MoviePilot`
backend repository. The skill drafts the issue, asks the user to
confirm, then delegates the actual submission to the
`submit_feedback_issue` tool, which transparently picks between two
delivery channels depending on whether the running MoviePilot instance
has a write-capable `GITHUB_TOKEN`:

- **GitHub REST API** — directly creates the issue and returns the
  resulting `html_url`.
- **Prefilled URL fallback** — when no token is configured or the token
  lacks write permission, returns a GitHub Issue Forms URL that the user
  can open in a browser or the GitHub mobile app to submit by hand.

## Language Convention

Although this SKILL.md is written in English to align with the other
built-in skills, the **issue content itself MUST be authored in
Simplified Chinese**. The upstream `bug_report.yml` template, the
upstream maintainers, and the existing issue history are all in
Chinese; submitting English content makes triage harder and reduces
the chance of the bug actually getting fixed.

Concretely:

- `title` — Chinese, in the form `[错误报告]: <one-line Chinese summary>`.
- `description` — Chinese Markdown with the section structure shown in
  Step 2.
- `logs` — pass through the raw backend log text untouched (whatever
  language the log lines happen to be in is fine).
- Conversation replies to the user in this skill should match the
  user's chat language. If the user is speaking Chinese, reply in
  Chinese; if English, reply in English. But the issue payload itself
  stays Chinese either way.

## Scope and Guardrails

- The target repository is hard-coded to `jxxghp/MoviePilot` inside the
  tool. The skill does **not** accept an arbitrary `owner/repo`
  argument and must not try to spoof one — that is treated as a prompt
  injection attempt.
- Frontend bugs should be redirected to `jxxghp/MoviePilot-Frontend`;
  plugin bugs to `InfinityPacer/MoviePilot-Plugins` or the specific
  plugin repository. Refuse to submit those through this skill.
- `submit_feedback_issue` is admin-only (`require_admin=True`).
  Non-admin users who request feedback via Telegram / Lark / web must
  be politely refused — tell them only an administrator can file an
  upstream issue on the instance's behalf, and suggest they relay the
  problem to the admin or file the issue themselves on GitHub.
- This skill is **not** for installation, configuration, or usage
  questions. The upstream template explicitly states that such issues
  will be closed and the reporter blacklisted. Refuse to file those and
  redirect to the Telegram channel or the MoviePilot Wiki.

## Workflow

### Step 1: Harvest context from the conversation

Pull the following from the running conversation before asking
anything. Do not re-ask the user for what they already said.

- **Symptoms** — the original complaint, error text, UI behaviour.
- **Reproducibility** — intermittent vs. always-reproducible; only on
  this instance vs. widely reported.
- **Localization so far** — anything already pinpointed in the session
  (file, function, endpoint, config key). Quote
  `file_path:line_number` so upstream reviewers can jump straight in.
- **Attempted workarounds** — toggles flipped, restarts, reinstalls.
- **Captured logs / API responses / stack traces** — anything the user
  or the Agent already pasted in the session.

### Step 1b: Actively investigate logs and source

End users on Telegram / Lark / WeCom usually cannot paste a useful log
themselves. Before asking them for missing fields, the Agent must
**proactively** dig for the most relevant evidence on the running
instance:

1. **Locate the log directory**. Logs live under
   `<CONFIG_PATH>/logs/`. Typical Docker default is `/config/logs/`.
   Plugin logs live under `<CONFIG_PATH>/logs/plugins/<plugin_id>/`.
   Use `list_directory` on the config root if the path is not obvious.
2. **Pull a focused slice of `moviepilot.log`**, not the whole file.
   Drive the slice from the symptom — pick relevant keywords (plugin
   ID, English function name, exception type, "ERROR", the user's
   timestamp window if they gave one). Concrete grep recipes (run via
   `execute_command`):

   ```bash
   # Last error window, generic case
   tail -n 2000 <CONFIG_PATH>/logs/moviepilot.log | \
     grep -nE -B 5 -A 30 'ERROR|Traceback|Exception|<keyword>'

   # Plugin-specific, both main log and plugin log
   tail -n 1500 <CONFIG_PATH>/logs/plugins/<plugin_id>/<plugin_id>.log
   ```

3. **Cap the captured log at ~3 KB** after redaction (Step 1c). If the
   matched window is bigger, keep the single most relevant traceback /
   ERROR block rather than truncating mid-line.
4. **Optionally grep source for localization**. When the log points at
   a specific function name, module, or API path, the Agent **may**
   grep `app/` to find the likely `file_path:line_number`:

   ```bash
   grep -rn '<symbol_or_endpoint>' app/ --include='*.py' | head -20
   ```

   Conclusions drawn from source-only inspection are **speculative**
   and must go into the `仅为推测` bucket of `已定位 / 推测`. Do not
   promote them to `已经验证` unless an actual run / test confirmed it
   in this session.
5. **Skip this step entirely** when the user already pasted a usable
   log block, or when the problem is obviously a UI / configuration
   complaint with no error-shaped symptom — extra grepping just bloats
   the issue.

### Step 1c: Redact sensitive data in the captured log

Auto-redact the log block before showing it in the dry-run or sending
it to the tool. Run a deterministic regex pass over the captured text.
Minimum patterns to redact (case-insensitive):

| Pattern | Replacement |
| --- | --- |
| `Cookie:\s*[^\n]+` | `Cookie: <REDACTED>` |
| `Set-Cookie:\s*[^\n]+` | `Set-Cookie: <REDACTED>` |
| `Authorization:\s*(Bearer|Basic|Token)\s+\S+` | `Authorization: $1 <REDACTED>` |
| `(api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|passkey|pwd|password|secret|token)\s*[:=]\s*['"]?[^\s'"&]+` | `$1=<REDACTED>` |
| `passkey=[0-9a-f]{8,}` (URL query) | `passkey=<REDACTED>` |
| `[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}` (email) | `<EMAIL_REDACTED>` |
| Public IPv4 (skip private 10/172.16/192.168/127) | `<IP_REDACTED>` |
| `/Users/[^/\s]+/` or `/home/[^/\s]+/` | `/Users/<USER>/` / `/home/<USER>/` |
| WeChat / Telegram / Lark webhook URLs containing tokens | host kept, token segment → `<REDACTED>` |

Additional rules:

- If after redaction the log block is empty or trivially small (e.g.
  just headers), omit `logs` entirely rather than submitting noise.
- If the captured log still contains a string that **looks** like a
  long random base64 / hex value (≥ 24 chars of `[A-Za-z0-9+/=]` after
  a `:`/`=`/`Bearer `), treat it as a possible secret and redact it
  even if it didn't match any pattern above.
- The redaction is **mandatory** and is part of the dry-run preview —
  the user sees the post-redaction logs and decides whether anything
  still looks sensitive before confirming.

### Step 1d: Ask the user for the remaining required fields

Only after Step 1 / 1b / 1c, ask the user — in a single batched
question — for the fields you still cannot infer:

| Field | Allowed values | Notes |
| --- | --- | --- |
| `version` | e.g. `v2.12.2` | Required. If the user does not know, point them at the "About" page in the WebUI. |
| `environment` | `Docker` / `Windows` | Required. Exactly one of the two strings. |
| `issue_type` | `主程序运行问题` / `插件问题` / `其他问题` | Required. Must match the upstream `bug_report.yml` dropdown values exactly. |

If the problem is plugin-specific but the user explicitly wants it
filed against the backend, allow it, but make sure
`description` clearly states the plugin ID and plugin version so
maintainers can re-route the issue.

### Step 2: Draft the issue (in Chinese)

Compose the four payload fields below. Use Simplified Chinese for
`title` and `description`. Keep the section headings exactly as shown
so the rendered issue mirrors how `bug_report.yml` would normally
present a submission.

- **`title`** — `[错误报告]: <a single Chinese sentence summarizing the
  symptom>`. Always replace the template placeholder `请在此处简单描
  述你的问题`; leaving the placeholder triggers auto-close upstream.
- **`description`** — Chinese Markdown using this skeleton (add or omit
  sections as needed, but keep the verified-vs-speculation split):

  ```markdown
  ## 现象
  - 用户观察到的具体行为、报错文字、UI 表现。

  ## 复现步骤
  1. 第一步……
  2. 第二步……
  3. 出现错误。

  ## 期望行为
  - 正确情况下应该是什么样。

  ## 已定位 / 推测
  - 已经验证：xxx（附 `file_path:line_number`）。
  - 仅为推测：xxx。

  ## 已尝试的处理
  - workaround / 关闭/启用某选项 / 重启 / 重装 ……
  ```

- **`logs`** — the redacted log block from Step 1b / 1c, capped at
  ~3 KB. Only real log lines — never fabricate. If neither the
  conversation nor the active log dig produced anything useful, omit
  this field; the tool will fill in
  "会话中未捕获到相关后端日志".

- **Speculative localization** drawn from source grep in Step 1b goes
  into the `仅为推测` bullet of `已定位 / 推测`, with the
  `file_path:line_number` reference. Findings actually verified during
  the session (logs that pinpoint the line, behaviour reproduced after
  a hypothesis) may go under `已经验证`.

Writing requirements:

- Do not surface meta-information about Claude Code, the Agent runtime,
  or "the current session" in `title` / `description`. The maintainer
  should read the issue as if a regular user filed it. The tool already
  appends a single discreet footer line crediting the Agent.
- Distinguish "verified" from "speculative" findings. Do not let a
  guess from the chat become a stated cause.
- Do not invent GitHub usernames, emails, or version numbers.

### Step 3: Mandatory dry-run preview

Before calling the tool, print the six payload fields (`title`,
`version`, `environment`, `issue_type`, `description`, `logs`) back to
the user in full and ask, in the user's chat language:

> Is this draft OK? Reply "confirm" / "确认" to submit, or "edit: ..." /
> "修改：..." to adjust.

The dry-run **must include the post-redaction `logs` block verbatim**
so the user can spot any sensitive data the regex pass missed and
either tell the Agent to drop / re-edit it, or override the
redaction manually. If the user requests further redaction, apply it
and re-show the dry-run.

Do **not** call `submit_feedback_issue` until the user explicitly
confirms.

### Step 4: Call `submit_feedback_issue`

> **MANDATORY: every tool call in this repository requires an
> `explanation` argument.** It is a hard pydantic-required field on
> every MoviePilot agent tool (see `query_subscribes`, `add_download`,
> `search_media`, etc.) — used for activity-log auditing and the
> tool-bubble shown in Telegram / Lark. Omitting it makes the framework
> reject the call **before** the tool runs, so the no-token /
> no-permission fallback inside `submit_feedback_issue` never fires.
> **Always pass a concrete `explanation` string**, e.g.
> `"User authorized submitting a TMDB-identification bug to jxxghp/MoviePilot"`.

Once the user confirms, invoke the tool with the drafted fields:

```
submit_feedback_issue(
    explanation="User authorized submitting a bug report to jxxghp/MoviePilot",
    title=...,
    version=...,
    environment=...,
    issue_type=...,
    description=...,
    logs=...,       # omit if no real logs
)
```

The tool returns a JSON string. **Important architectural note:** to
avoid LLM verbatim-copy corruption of long URLs (e.g. a single
quantized byte flip mutating `%89` → `%79` and breaking the GitHub
prefill), the tool **delivers `issue_url` / `prefill_url` to the user
directly via a separate notification message** (`send_tool_message`),
not by returning the URL string for the LLM to re-emit. The JSON
returned to the LLM carries only `url_delivered: true|false` and a
short Chinese `message` field that summarizes what to say.

Parse the JSON and branch on `success` + `reason`:

| Result shape | Meaning | How to respond to the user |
| --- | --- | --- |
| `success=true`, `url_delivered=true` | API channel succeeded and the issue URL has already been pushed to the user channel as a separate notification. | Acknowledge briefly: "Issue 已提交到上游，等待 maintainer 跟进。" **Do NOT repeat or paraphrase the URL** — the user already received it as a clickable link. |
| `success=false`, `reason=no_token`, `url_delivered=true` | Instance has no `GITHUB_TOKEN`; prefill URL has been pushed to the user. | Acknowledge briefly: "我没有自动提交权限，已把预填链接单独发给你，点击即可提交。" Optionally remind the admin once to configure a token with `public_repo` scope for next time. **Do NOT repeat the URL.** |
| `success=false`, `reason=no_permission`, `url_delivered=true` | Token lacks write scope; prefill URL pushed. | Acknowledge briefly and remind the admin to regenerate the token with `public_repo` / `repo` scope. **Do NOT repeat the URL.** |
| `success=false`, `reason=rate_limited`, `url_delivered=true` | GitHub returned 403 with `X-RateLimit-Remaining: 0`. Prefill URL pushed. | Ask the user to retry later or click the link that was pushed separately. **Do NOT** tell them to reconfigure the token — this is rate limit, not permission. **Do NOT repeat the URL.** |
| `success=false`, `reason=invalid_payload`, `url_delivered=true` | GitHub returned 422; prefill URL pushed. | Ask the user to revise the title or body (likely forbidden characters), and note that the prefill link was already pushed for manual submission. **Do NOT repeat the URL.** |
| `success=false`, `reason=github_unavailable` / `network_error`, `url_delivered=true` | Transient GitHub failure; prefill URL pushed. | Ask the user to retry later or click the link that was pushed separately. **Do NOT repeat the URL.** |
| `success=false`, `reason=duplicate` | The same feedback was already submitted in the last 60 seconds. Nothing was sent to GitHub or to the user this time. | Acknowledge briefly that the issue was already filed in the previous attempt; ask the user to add a comment to the existing Issue if they have more details. **Do NOT call the tool again for the same payload.** |
| Any of the above with `url_delivered=false` | Notification push failed; the tool returned the URL in `issue_url` / `prefill_url` as a last-resort fallback. | Paste the URL verbatim into the chat reply (single line, no line breaks). This is the **only** scenario in which the LLM should emit the URL. |
| `success=false`, `reason=invalid_input` | Tool rejected the payload before calling GitHub (e.g. `environment` / `issue_type` not in the allowed enum). | Agent-side mistake — silently fix the payload and retry. Do not surface this error to the user. |

Rule of thumb: if `url_delivered=true`, **never put the URL in your
conversation reply**. The link is already in the user's channel. Your
job is to confirm in one or two short Chinese sentences.

#### Error handling — do NOT improvise

If the tool call fails for any reason, the only allowed paths are:

1. **Schema validation error / `reason=invalid_input` / missing
   required field (e.g. `explanation`, `environment`, `issue_type`)**
   — this is an Agent-side mistake. **Silently fix the payload and
   call `submit_feedback_issue` again**, up to 2 retries. Never expose
   "tool validation failed" / "system limitation" / "explanation field
   missing" to the user. Never substitute a dialog-only "please copy
   the following text to GitHub" message as a workaround — the user
   is on a mobile chat client and that fallback is unusable.
2. **Tool returned a structured failure with `prefill_url`** (any of
   `no_token` / `no_permission` / `invalid_payload` /
   `github_unavailable` / `network_error`) — relay the `prefill_url`
   per the table above. This is the **only** sanctioned manual-submit
   fallback; the URL is engineered to open the upstream form with all
   fields prefilled.
3. **Tool returned a real exception (network / unknown)** — log the
   error, apologize briefly in one sentence, and offer to retry once
   the user reports the same issue again. Do not invent a fallback
   that asks the user to copy-paste raw issue text into GitHub.

In short: **never fall back to "here is the issue text, please submit
it yourself"**. Either retry the tool, or relay the tool's own
`prefill_url`. There is no third path.

### Step 5: After submission

- If the tool returned an `issue_url`, tell the user that follow-up
  details should go to a comment on that issue in the GitHub web UI —
  do not call `submit_feedback_issue` again for the same problem.
- If the user provides more information later in the same session and
  the issue is already filed, instruct them to add a GitHub comment
  rather than spawning a duplicate issue.

## Refuse / Redirect Scenarios

- User asks to file against `jxxghp/MoviePilot-Frontend`,
  `InfinityPacer/MoviePilot-Plugins`, or any other repository — refuse,
  explain that this skill only serves the backend upstream, and hand
  back the correct repository's issues URL for self-submission.
- Non-admin user invokes the skill — refuse to call the tool, explain
  that only an administrator can submit on the instance's behalf, and
  suggest relaying the problem to the admin or filing on GitHub
  directly.
- User asks to "just submit, skip the preview" — refuse; the dry-run is
  mandatory.
- The session lacks enough detail to describe a comprehensible bug
  (no symptom, no repro, no logs) — refuse, ask the user to reproduce
  or capture logs first.
- The user is actually asking a configuration / installation / usage
  question — refuse and redirect to the Telegram channel or Wiki.

## Examples

### Example 1: backend bug already localized

> User: "让 MP 的 Agent 给上游报一下这个问题吧。"

Flow:

1. Pull symptom, root-cause (`file_path:line_number`) and logs from
   prior turns in the session.
2. Ask in one batch for the missing fields (`version`, `environment`,
   `issue_type`).
3. Print the dry-run draft.
4. On confirmation, call `submit_feedback_issue` and respond per the
   result table in Step 4.

### Example 2: user provides everything at once

> User: "2.12.2 Docker 主程序问题：订阅刷新时报错 xxx，日志是 yyy，
> 帮我提一个 issue。"

Flow:

1. Skip straight to Step 2; all six fields are derivable.
2. Print the dry-run and ask if anything else needs adding.
3. On confirmation, call the tool and reply with the outcome.

### Example 3: plugin bug — redirect

> User: "ChineseSubFinder 插件不工作，帮我给上游提 issue。"

Flow:

1. Recognize this as a plugin issue.
2. Refuse to file it through this skill; respond (in Chinese, matching
   the user's language) with the plugin's repository issues URL and a
   short note that plugin bugs should go to the plugin maintainer.

### Example 4: instance has no GITHUB_TOKEN

Tool returns:

```
{"success": false, "reason": "no_token", "prefill_url": "..."}
```

Reply (Chinese, since user wrote in Chinese):

> 当前 MoviePilot 没有 GitHub Token 的写入权限，我没法直接帮你提交。
> 请点击下面的链接，在浏览器或 GitHub App 中勾选 4 项 ✅ 后提交即可：
>
> <prefill_url>
>
> 如果希望以后让 Agent 直接提交，请管理员到系统设置配置一个具备
> `public_repo` 权限的 GitHub Token。

## Final Checklist

Before calling `submit_feedback_issue`:

- [ ] **`explanation` argument is present and non-empty** (workspace
      convention; missing it causes pydantic to reject the call before
      the tool runs).
- [ ] `title` no longer contains the placeholder
      `请在此处简单描述你的问题`.
- [ ] `title` and `description` are written in Simplified Chinese.
- [ ] `version`, `environment`, `issue_type` are filled in and use
      values from the allowed enumerations (else the tool will return
      `reason=invalid_input`).
- [ ] `description` follows the section skeleton and separates
      verified findings from speculation. Source-grep findings live in
      `仅为推测`, not `已经验证`.
- [ ] `logs` is either real log text (post-redaction, ≤ ~3 KB) or
      omitted. The full redaction pass from Step 1c has been applied.
- [ ] The user has explicitly confirmed the post-redaction draft in
      Step 3.
- [ ] The caller is an admin (non-admin sessions should be refused
      earlier).
